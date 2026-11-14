# 0038: Counts keep explicit and inferred tags (threshold 0.5, in the config)

- Status: accepted
- Date: 2026-11-14

## Context
Every count, gap and tag filter keeps only tags with `confidence >= min_confidence`
(#69). The default, 0.5, was written into `Filters` in `schemas.py`, the CLI options
and `tag.derive`, not the config. Decision 0029 found counted tags 95% ok at 0.9 but
only 68% ok at 0.6, which raised the question of counting explicit tags only.

**Confidence is discrete** (`tagging.confidence`): explicit 0.9, inferred 0.6, weak 0.3,
and 0.4 for quotes not found and for title-only records. So 0.5 and 0.6 give the same
counts, and so do 0.7, 0.8 and 0.9. There are two real choices: **explicit and inferred**
(0.5) or **explicit only** (0.7).

**Precision by confidence**, from the hand-checked review workbooks (`data/reference/`;
ok / unsure / wrong):

| review | 0.9 (explicit) | 0.6 (inferred) |
|---|---|---|
| pilot 2 (`review_main`, tag.v3) | 213 / 10 / 2 (95% ok) | 91 / 35 / 7 (68% ok) |
| pilot 3 (tag.v4) | 232 / 11 / 1 (95% ok) | 92 / 26 / 4 (75% ok) |
| pilot v5 (tag.v5, the prompt of the main run) | 237 / 9 / 1 (96% ok) | 96 / 25 / 4 (77% ok) |

At tag.v5 only 3% of inferred tags are wrong; most of the rest are "unsure". The
unsure ones cluster in research_mode (4 of 5 inferred), method and data_type.

**Coverage on the real catalogue** (`scripts/compare_confidence_thresholds.py 0.5 0.7`,
3,003 records, records with a counted tag):

| dimension | ≥ 0.5 | ≥ 0.7 |
|---|---|---|
| sub_area | 2,686 (89%) | 1,608 (54%) |
| impact_area (derived) | 2,754 (92%) | 2,139 (71%) |
| objective | 1,734 (58%) | 554 (18%) |
| research_mode | 1,210 (40%) | 417 (14%) |
| transformative_aspiration | 1,936 (64%) | 690 (23%) |
| justice_lens | 2,192 (73%) | 992 (33%) |
| sdg | 2,132 (71%) | 1,238 (41%) |
| data_type | 2,202 (73%) | 1,554 (52%) |
| method | 2,279 (76%) | 2,079 (69%) |
| country | 2,418 (81%) | 2,089 (70%) |
| system | 2,753 (92%) | 2,660 (89%) |

The strategy dimensions (sub-areas, objectives, modes, aspirations) are mostly
inferred by nature: papers rarely name CDE's strategy categories. Explicit-only would
leave half the records without a sub-area.

**Gaps** at group level are not stable between the two thresholds:

- sub_area × method: 13 gaps at 0.5, 8 at 0.7, of which 5 are among the 13;
- sub_area × data_type: 13 vs 3, of which 2 are among the 13;
- sub_area × actors: 4 vs 6, of which 3 are among the 4;
- impact_area × method: 0 vs 1 (TE × geospatial and land, 11 vs 52.6).

The strongest cells (0 records where 7–12 were expected, e.g. TE-a × geospatial
methods) appear at both thresholds.

## Decision
- **Counts keep explicit and inferred tags:** the default stays at 0.5. Explicit-only
  costs a third to two thirds of the records on the strategy dimensions and gains little,
  since only 3% of inferred tags are wrong.
- **The default lives in the config:** `tools.min_confidence: 0.5`.
  `ToolContext.expand`, which every tool calls, fills it in when a call sets no
  threshold of its own (`"min_confidence" not in filters.model_fields_set`). The CLI
  `--min-confidence` options default to that value, as do `list_vocabulary`'s record
  counts and the publish summary's unit agreement (`tag.derive.unit_agreement`).
- `Filters.min_confidence` keeps its schema default of 0.5 (shared contract, no change
  to the type). Its description now says to leave it unset for the configured default.
  Functions that take a connection rather than a context (`count_matrix`,
  `filter_record_ids`) still fall back to the schema default; the tools never call
  them with unresolved filters.

## Consequences
- Counts and gaps are unchanged today. Changing the threshold for everyone is now one
  line of config, and a user can override it locally.
- Gap answers should say that a gap rests on inferred tags too. A gap that holds at
  both thresholds is more robust; showing that is left to the gap work in #67, together
  with the evaluation questions (#19).
- Revisit with the #16 reference set, which measures recall as well:
  - a per-dimension threshold, e.g. explicit-only for research_mode, where most
    inferred tags were unsure;
  - 0.4 tags (unverified quotes, title-only records) stay out; check whether title-only
    country and setting tags should count.
- Re-run `scripts/compare_confidence_thresholds.py` after any retagging.
