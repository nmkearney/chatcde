# 0043: Test-question results after M1, and the pass rate required (85%)

- Status: accepted
- Date: 2026-11-15

## Context
Decision 0025 set `evaluation.min_question_pass_rate: 0.8` as a placeholder, to be
confirmed after the first real run (#5). The 27 questions of `tests/questions.yaml`
(#19: 4 lookup, 6 search, 8 count, 4 gaps, 5 synthesis) were run with both agent models,
3 runs each, before and after the M1 fixes. The fixes:
- #91: one reference normalisation step;
- #92: filter-spelling repairs (decision 0041);
- #93: filtered rows (decision 0042);
- #94: agent_system.v5, 16 tool calls, 8,192 reply tokens;
- this PR: references that cannot be records, tool calls written as text, rounding.

| run (results in `data/eval/`) | gpt-oss-120b | qwen3.8-27b |
|---|---|---|
| baseline (`baseline-2026-11-15.jsonl`) | 50/81 (62%) | 74/81 (91%) |
| baseline, rescored after #91 | 58/81 (72%) | 74/81 (91%) |
| after #91–#94 (`m1-2026-11-15.jsonl`) | 72/81 (89%) | 77/81 (95%) |
| same answers, rescored after this PR | **78/81 (96%)** | **78/81 (96%)** |

After the last rescore:

| kind | gpt-oss-120b | qwen3.8-27b |
|---|---|---|
| lookup | 12/12 | 12/12 |
| search | 18/18 | 17/18 |
| count | 24/24 | 24/24 |
| gaps | 10/12 | 11/12 |
| synthesis | 14/15 | 14/15 |

Time per answer: gpt-oss mean 14.6 s (median 10.1 s, 2.6 tool calls), qwen 63.4 s (median
51.3 s, 2.2 tool calls). GPUStack refused or timed out on 22 qwen answers when the VPN
dropped; they were rerun, so the file holds no endpoint errors.

This PR adds three changes, found in the rerun:
- **References that cannot be records** (no digit: `count_by`, `find_gaps`,
  `gap-result`, `SG-d`, and `【BORIS:[]】`) are removed without asking the model again
  (`agent.could_be_record`). `no_unknown_ids` counts only unknown IDs that could be
  records. gpt-oss kept citing the tool after a count even when asked to rewrite. Readers
  never saw these references, since the agent already stripped them; they were 6 of
  gpt-oss's 9 failures.
- **A tool call written as text** (`<tool_call> <function=…>`, once with qwen) is sent
  back once with `TOOL_CALL_NOTE`. Not yet seen in a real run.
- **Numbers:** an expected number matches a more precise one within half a unit of its
  last place (28 matches 28.1, not 28.5). `synth-mount-kenya` accepts all 19 records
  with Mount Kenya in the title, not only the 7 with full text.

## Decision
- **`evaluation.min_question_pass_rate: 0.85`.** Both models pass 96% now. The
  threshold allows for run-to-run noise (one answer is 1.2 points; gap answers vary
  between runs) and still fails a regression of about a tenth of the answers.
- **The agent model stays gpt-oss-120b** (`models.agent`; the user's choice). It
  matches qwen3.8-27b on these questions after the fixes and is four times faster.
  qwen stays the alternative and is still scored with `--models both`.
- `chatcde evaluate questions --models both --runs 3` is rerun after any change to the
  agent, its prompts, the tools or the tags, and the results are compared by kind.

## Remaining failures (known weaknesses)
- **gpt-oss stops at impact-area level on gap questions** (`gaps-je-regions`, 2 of 3
  runs). It finds no JE × region gap and does not check sub-areas, although the prompt
  says to. The gap is one level down (Eastern Africa × JE-d, 0 vs 5.4). qwen always
  checks. A gap answer that says "no gap" should be read with this in mind; `chatcde gaps`
  runs both levels.
- **An answer without citations** (gpt-oss, `synth-mount-kenya` run 1): a table of
  studies with no BORIS references.
- **qwen garbles or shortens IDs:** a UUID with a repeated tail (`search-telecoupling`),
  and the number at the end of a record's BORIS DOI (139203 for `10.7892/boris.139203`)
  in place of its record ID (`gaps-te-sdgs`). Both are caught as unknown IDs and removed
  from the answer.
- The questions were written, and the fixes made, by one developer who saw every
  failure. The pass rate on these questions overstates the rate on new ones.

## Consequences
- #5 is closed. Next for M1 is #68 (reference lists out of search).
- Revisit:
  - **held-out questions:** 8–10 new questions not used for tuning, run once; if their
    pass rate is far below 96%, the fixes did not generalise;
  - the threshold, once questions from CDE researchers exist;
  - a per-kind threshold, if gap questions keep failing more often than the others;
  - an LLM judge for synthesis quality (decision 0025), still not done.
