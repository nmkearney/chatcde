# 0042: A filter on a counted dimension limits its rows; gaps never compare an area with itself

- Status: accepted
- Date: 2026-11-15

## Context
`count_by(row=sub_area, col=method, filters={sub_areas: [SG-a, SG-b, SG-c, SG-d]})`
kept the records tagged with an SG sub-area, then counted every sub-area those records
carry: 116 cells over 16 sub-areas where the question was about four (#83, found with
#81). The same held for any dimension both counted and filtered
(`count_by(row=impact_area, filters={impact_areas: [TE]})` had rows for all four areas).

`find_gaps` builds on the same matrix. In the #5 baseline (2026-11-15) gpt-oss called
`find_gaps(row=region, col=impact_area, filters={impact_areas: [JE]})`. With JE as the
only column, every expected count equals the observed count, so no cell can be a gap.
It reported "no region is rarely studied". Decision 0037 told the model not to do this,
but nothing stopped it.

## Decision
- **Rows follow the filter.** When the filters restrict the row or column dimension,
  only the filtered terms are counted on it (matching ignores case, as filters do).
  At group level this leaves the groups holding the filtered terms; a group code in a
  filter is expanded to its terms first (decision 0030), so it keeps its group. A
  sub-area filter limits impact-area rows to the parent areas (`SG-a` → `SG`, #49).
- **Denominators are unchanged:** `total_records`, `untagged_records` and the base of
  the matrix count the filtered records, as before. The kept rows have the same counts
  as without the filter (checked on the real catalogue: the #81 call now gives the four
  SG rows, 33 cells, with the counts of the unfiltered matrix).
- **`find_gaps` with a filter that leaves one row or one column is an error.** The
  message names the filter and says to drop it and read that area's cells. A filter
  keeping several terms is allowed: it compares those terms with each other (e.g. the
  four SG sub-areas among themselves).
- The `count_by` and `find_gaps` docstrings (shared contract, `tools/core.py`) state the
  rule; the signatures are unchanged.

## Consequences
- The `chatcde count` and `chatcde gaps` commands follow the same rule, since they call
  the same functions. A scope such as `chatcde gaps --dims sub_area,method --sub-area
  SL-d` is now an error instead of an empty result.
- Expected counts in a gap analysis filtered to several terms of its own dimension are
  relative to those terms only; answers should say so.
