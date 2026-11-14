# 0040: output_type is renamed audience_type, with a migration of stored tags

- Status: accepted
- Date: 2026-11-14

## Context
The dimension `output_type` holds only the audience a publication is written for
(Academic, Policy, Practitioner, Public). The publication type is not a tag: it comes
from BORIS (`dc.type`, `Record.pub_type`). In the pilot review (decision 0029) the
curator found the name misleading, since it reads as the BORIS type (#60).

The rename was meant to happen before the full tagging run (#15), which has now run:
the catalogue holds 4,348 `output_type` rows in `tags` and 5,032 in `tag_runs`
(main and the pilot runs). Its two options were a real rename or a display label
only. Doing it now, before M2 adds more prompts and tools naming the dimension,
costs one migration.

## Decision
- **Rename in code and data:** `Dimension.AUDIENCE_TYPE = "audience_type"`; the
  filter field `output_types` becomes `audience_types` (shared contract, this PR only).
  The vocabulary block `output_type:` becomes `audience_type:`; its terms and the
  vocabulary version (0.4) stay.
- **Prompt tag.v6** is tag.v5 with the heading `### audience_type`; nothing else
  changes. `tagging.prompt` points to it. Prompt files are never edited once used.
- **Migration:** `chatcde migrate` (`chatcde.catalogue.migrate`) renames old names
  in `tags` and `tag_runs` in one transaction, and is safe to rerun. The map of old
  to new names (`catalogue.RENAMED_DIMENSIONS`) is kept, so any older catalogue can
  still be brought up to date.
- Stored provenance is **not** rewritten: the tags still say `tag.v5`, which is what
  produced them. Only the dimension name changes.
- The old `output_types` filter key is not kept as an alias: unknown filter keys are
  errors (decision 0034), and the agent is told the valid keys.

## Consequences
- An unmigrated catalogue looks as if it had no audience tags (the rows are there under
  the old name). Run `chatcde migrate` after pulling this change; the bundles in
  `data/bundles/` must be rebuilt with `chatcde export` before they are shared (M4).
  `fetch-data` (#18) should run the migration on what it downloads.
- `chatcde tag` skips work only when model, prompt and vocabulary versions match, so a
  later `chatcde tag --all` would retag all 3,003 records with tag.v6 (the full run took
  most of a day). Until the next deliberate full run, tag new records with `--ids`, or
  set `tagging.prompt: tag.v5` locally to resume with the old prompt.
- The review workbooks in `data/reference/` still say `output_type`; they are a record
  of past reviews and are left as they are.
