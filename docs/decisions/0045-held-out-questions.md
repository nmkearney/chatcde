# 0045: Held-out test questions: the M1 pass rate mostly holds

- Status: accepted
- Date: 2026-11-15

## Context
Decision 0043 found 96% for both agent models on the 27 tuned questions, and noted that
the same developer wrote the questions and made the fixes. To check whether the fixes
generalise, 10 new questions were written after the M1 fixes (#91–#96). They cover topics
the tuned set does not: Ethiopia, Bolivia, Switzerland, urban settings and Central Asia,
with 2 per kind. They are in `tests/questions_heldout.yaml`, committed before their first
run (fb3d5e8). Each was run once with both models (`data/eval/heldout-2026-11-15.jsonl`).

## Results
| | gpt-oss-120b | qwen3.8-27b |
|---|---|---|
| first run, as written | 8/10 (80%) | 10/10 |
| one expectation corrected (below) | **9/10 (90%)** | **10/10** |
| tuned questions (decision 0043) | 78/81 (96%) | 78/81 (96%) |

Time per answer: gpt-oss 8.7 s (3.1 tool calls), qwen 65.4 s (4.3 tool calls). No
endpoint errors.

The two gpt-oss failures:
- **`ho-lookup-cocoa-carbon`: an error in the question.** The question asks who led the
  study and in which journal. gpt-oss answered both correctly, but the expectation also
  required the year (2014), which the question does not ask for. The expectation
  `numbers: [2014]` was removed. This is the only change made after the run.
- **`ho-count-switzerland-german`: two code counts that disagree.** gpt-oss answered "255
  of the 614 Swiss records are in German", from `coverage_report(countries=[CHE])`.
  `by_language` counts the first listed BORIS language, so 255. `filters.languages: [de]`
  matches German anywhere in the list, so 259. The count comes from code (the hard rule
  holds), but the answer does not say which definition it used. This is kept as a failure
  and is #98.

Both held-out gap questions pass with both models. This includes Central Asia × SDGs,
where the area has to be read as a row of the region × SDG table rather than filtered by
(decision 0042). On the tuned set gpt-oss failed `gaps-je-regions` 2 of 3 times. Here
the gap was at the level the tool reports by default, so this does not test the
sub-area weakness.

## Decision
- The M1 fixes are taken to generalise: 19 of 20 held-out answers pass (95%, against 96% on
  the tuned set). gpt-oss's 90% is above `evaluation.min_question_pass_rate` (0.85).
  With one run per question, one answer is 10 points.
- `models.agent` stays gpt-oss-120b. qwen3.8-27b passed every held-out question but is
  7.5 times slower.
- **Rule for the held-out file:** do not change code to pass a held-out question. If a fix
  is made because of one, that question moves to `tests/questions.yaml` and a new one
  replaces it. Corrections to a wrong expectation are allowed and recorded, as above.
- M1 ("correct, traceable answers") is complete. Next is M2 (workflows: #73 trend, #71
  experts, #72 brief, #78, #74).

## Consequences
- 10 held-out answers per model is a small sample, and the same developer wrote these
  questions too, though after the tuning and on new topics.
- #98: the language counts of `coverage_report` and the languages filter should agree, or
  say which definition they use.
- Revisit:
  - run the held-out file again after M2 changes the agent or its prompts, once, and
    compare;
  - replace or extend it with questions from CDE researchers when they exist.
