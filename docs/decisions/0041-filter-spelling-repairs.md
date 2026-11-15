# 0041: Unambiguous filter misspellings are repaired, with a note

- Status: accepted
- Date: 2026-11-15

## Context
Decision 0034 rejects unknown filter keys, so a filter is never dropped silently, and
`run_tool` answers a misspelt key with "did you mean" (#67). In the baseline run of the
test questions (#5, 2026-11-15, 81 answers per model) gpt-oss-120b still wasted about 50
tool calls on filter keys:

- `filters.impact_area` 17 times (also `sub_area`, `region`, `country`, `sdg`), each
  time after being told "did you mean 'impact_areas'?" in an earlier call;
- a year as `year`, `years`, `pub_year` or `year_range`, as `2020`, `[2020, 2026]` or
  `{"gte": 2020}`, 16 times. No suggestion matched (`year_from` is too far from `year`),
  and in all three runs of "How many publications since 2020…" it then told the user
  that years cannot be filtered;
- `filters.record_id` in search 10 times, to look inside one record.

Every wasted call counts against the budget of `agent.max_tool_calls`.

## Decision
- **`tools.specs.repair_filters`** reads a misspelling before validation, but only where
  one reading is possible:
  - the singular of a list field (`impact_area` → `impact_areas`, `country` →
    `countries`), when the list field is not given as well;
  - a single value for a list field becomes a one-item list (`"SL"` → `["SL"]`);
  - a year spelling (`year`, `years`, `pub_year`, `publication_year`, `year_range`)
    becomes `year_from` / `year_to`: one year → that year only, two years or a
    consecutive list → that range, `{gte|ge|min|from|start, lte|le|max|to|end, gt,
    lt}` → an open or closed range. Only when neither `year_from` nor `year_to` is given.
- **Never silent:** the result starts with `[Note: filters.impact_area read as
  impact_areas. Use these names next time.]`. The repaired arguments are the ones
  recorded (`ToolCallRecord.arguments`, the Count queries list), and the repair is logged.
- **Everything else stays an error** (decision 0034): lists of years with gaps, unknown
  keys inside a year object, both spellings at once, any other key. An unknown key
  with no close match now gets the list of valid filter keys. `record_id` gets "to look
  inside one record use get_publication or read_section".
- Codes inside the filters are not repaired. Unknown codes stay errors with
  suggestions (decision 0034).

## Consequences
- The shared contract (`Filters`) is unchanged: the repairs happen in the agent's tool
  runner, not in the schema. CLI options and direct Python calls are not affected.
- The tool definition still names only the real keys, so the model is not taught the
  misspellings.
- Revisit if a model writes a misspelling with two readings. Add it to the error
  hints, not to the repairs.
