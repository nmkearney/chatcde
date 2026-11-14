# 0037: Country codes are normalised in code; unknown ones are dropped

- Status: accepted
- Date: 2026-11-14

## Context
In the full tagging run (2026-11-10) 4 of 9,009 batches failed, all `how_where`
(records 96e0d3ed, 08a38880, 7c70fb4e, dc7e1ef8), so those records have no method,
data type, setting, scale or country tags (#70). The answer schema lists the ISO
3166-1 alpha-3 codes as an enum (decision 0028), but the server does not always hold
the model to it. The model wrote **sports (IOC/FIFA) codes**: the run logs show
`LIT` (Lithuania, 36 times), `KOS` (Kosovo, 27), `MOR`, `CHD` and `SDS`, plus one
method code (`DAT-secondary-quantitative`) in the country list. Most of these were
fixed by the retry; when both attempts failed, one bad code in a list of eight threw
away the whole batch.

Kosovo has no ISO 3166-1 code and is not in UN M49 (`vocab/m49.csv`). No record
names Kosovo in its title or abstract.

## Decision
- Before validation, each country `term` is looked up case-insensitively
  (`chatcde.tag.regions.country_lookup`, `chatcde.tag.answer.normalise_countries`):
  alpha-3 codes pass unchanged; then `tagging.country_aliases` (config); then alpha-2
  codes and English names from `vocab/m49.csv`. A valid alpha-3 code is never remapped.
- `tagging.country_aliases` holds the sports codes seen in the logs and common
  unambiguous IOC codes (29). Ambiguous ones (`LIB` Lebanon/Libya, `NIG` Niger/Nigeria,
  `SLO` Slovenia/Slovakia) are left out, so they are dropped rather than guessed.
- A code that still cannot be mapped is **dropped and logged** (warning), and the rest
  of the batch is kept. `KOS` is dropped: Kosovo is not added to the country list.
- The JSON schema sent to the server is unchanged, so the prompt version stays
  `tag.v5` and existing tags need no retagging.

## Consequences
- A batch no longer fails because of one country code; a dropped code loses only that
  tag. Dropped codes are visible in the log, not in `tag_state`.
- Kosovo cannot be tagged. Revisit if records about Kosovo turn up (the user-assigned
  code `XKX`, used by the EU and the World Bank, would need its own row next to M49).
- New aliases are a config change; check the log for "is not a country" after a run.
