# 0046: Expertise finder: people from merged author rows, ranked by matching records

- Status: accepted
- Date: 2026-11-15

## Context
The "expertise finder" use case (planning.md) asks who at CDE has worked on a topic
(#71). The catalogue's `authors` table has one row per author per record. On the real
catalogue it holds 14,298 rows, 299 distinct CDE names and 78 distinct CDE ORCIDs.
Three facts shape the design:

- **Spellings drift.** `Liniger, Hanspeter` / `Liniger, Hans Peter`, `Breu, Thomas` /
  `Breu, Thomas Michael`, `Allan, E.` / `Allan, Eric`. 27 ORCIDs carry more than one
  spelling. The BORIS person ID (the `authority` of `dc.contributor.author`) is read
  during the harvest but not stored.
- **`is_cde` is per record.** The same person is CDE on some records and not on others
  (Liniger: 106 with, 37 without), because BORIS often lacks the affiliation on older
  records.
- **754 records have no CDE-flagged author** (mostly legacy and NCCR records, decision 0008).

## Decision
- **People = author rows merged by ORCID and name** (`chatcde.tools.expertise.build_people`):
  - rows with the same ORCID are one person;
  - rows with the same name key are one person. The key folds accents and case and joins
    the given names, ignoring spaces, hyphens and dots. A key that carries two ORCIDs is
    two people; its rows without an ORCID then stay apart;
  - a spelling with initials only joins the single person with that surname whose given
    names start with the initials. With several candidates it stays apart, and the output
    names everyone merged this way.
- **CDE is person-level:** a person with CDE affiliation on at least one record counts as
  CDE, and all their matching records count. `cde_only` (default true; `--all-authors`)
  lists only them. Recording the choice: record-level counting would undercount
  long-standing staff whose older records lack the affiliation.
- **Matching records:** the filters (tags, years, units, ...) give the set. With a topic,
  one hybrid search inside it (`experts.search_candidates` 400 chunks, reranked) keeps the
  records whose best passage scores at least `experts.min_score` (0.3), at most
  `experts.max_records` (100). Without a topic, every record passing the filters matches,
  so the counts are exact tag counts. Conference items count, as decision 0018 says for
  expertise.
- **Ranking:** matching records, then matching records from the last
  `experts.recent_years` (5) calendar years, then the latest year, then the name. Counts
  only, no weighted score, so each rank can be checked against the record list.
- **`chatcde experts`** is a fixed chain like `gaps` (decision 0024): ranking and evidence
  from code, then one structured request (`experts.v1`) for a one- or two-sentence reason
  per person, citing only that person's evidence records. Invented IDs are dropped and
  shown, and a person the model skips gets a placeholder. The prompt forbids guessing
  gender from a name. The structured-request plumbing is shared with `gaps` in
  `chatcde.workflows.structured`.
- `find_expertise` is offered to the agent (`agent.tools`).

### Tuning (real catalogue, 2026-11-15)
Rerank scores of the best passage per record, by hand:
- 0.5 and up: on topic;
- 0.3–0.5: mostly on topic (a Laos land-grab paper at 0.30, gender and land in the Mekong
  at 0.67);
- below 0.25: loosely related (water harvesting for "gender and land rights").

At 100 chunks the depth, not the score, set the matches (SLM: all 39 records above 0.5).
At 400 chunks (about 2.5 s), narrow topics level off on the threshold (pastoralism: 38
records), while broad ones (SLM: 102) reach `max_records`, as the caveats say.

### Evaluation (2026-11-15, `data/eval/71-full.jsonl`)
All 30 questions, both agent models, 3 runs each (2 qwen answers timed out on the VPN
and were rerun):
- gpt-oss-120b **86/90 (96%)**, qwen3.8-27b **90/90 (100%)**; required 0.85.
- Original 27 questions: gpt-oss 77/81 and qwen 81/81 (M1: 78/81 each). The gpt-oss
  misses are flaky, not tool choice: a count of 191 instead of 165 (count-sg-interviews),
  one Myanmar synthesis citing none of the expected records, and two Myanmar answers
  that wrote "oil‑palm" with a non-breaking hyphen, which `mentions_any` does not match.
- No question outside the experts set called `find_expertise`; every expertise answer did.

## Consequences
- Rankings for broad topics rest on the best 100 records only. A narrower topic or tag
  filters give exact counts.
- Name merging can still split a person (two unrelated spellings without an ORCID) or,
  rarely, merge two people through initials. Revisit: store the BORIS person ID
  (`Author.person_id`, a contract change plus a re-harvest) and merge on it instead.
- Units come from `authors.cde_unit`, which BORIS fills for few rows (851 of 4,223), so
  many people show no unit.
- Projects (planning.md: "people, units and projects") wait for `get_project`.
