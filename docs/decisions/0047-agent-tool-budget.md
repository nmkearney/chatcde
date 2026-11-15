# 0047: Agent tool budget: merge first, slim definitions, toolsets only if needed

- Status: accepted
- Date: 2026-11-15

## Context
The agent sees every tool in `agent.tools` on every request. With `find_expertise`
(#71, decision 0046) that is 8 tools, and planning.md lists 19. Measured 2026-11-15:

- the 8 tool definitions are about 27,000 characters (about 6,750 tokens) per request,
  plus about 1,500 tokens of system prompt;
- the `Filters` schema (3,178 characters) is repeated in 5 tools, about 60% of the total.

The context window is not the limit; both agent models have far more. The costs are
time (every turn re-reads the definitions, and qwen already takes ~63 s per answer) and
**tool choice**. Mid-sized open models choose reliably among about 10–20 distinct tools
(a rough guide, not measured here), and sooner go wrong when tools look alike. Most
planned tools overlap with existing ones: `find_similar`, `search_field` and
`find_datasets` with `search_publications`; `trend` and `spatial_footprint` with
`count_by`. Our own estimate is misrouting around 12–15 tools if they are added as they are.

## Decision
1. **Merge before adding.** A planned tool that is a variant of an existing one becomes
   an option of it where it can (e.g. `trend` as a year option of `count_by`, #73;
   `spatial_footprint` as a country/region count; `search_field` possibly as a corpus
   option of search, #74). A tool that stays separate says plainly in its docstring when
   to use it instead of its look-alike.
2. **Slim the definitions** (#102): describe `Filters` once and give each tool a short
   schema. The shared contract does not change, only the generated definitions.
3. **Fixed workflows for the core uses.** `gaps` and `experts` (later `frontier` and
   `brief`) run a set tool chain in code, so no tool has to be chosen. New core uses
   get a workflow, not only an agent tool.
4. **Named toolsets only if needed** (#103): a cheap first step picks a toolset of about
   6–8 tools per question. Built only when the tool-choice checks show misrouting that 1
   and 2 did not fix, or when the agent would be offered more than about 12 tools.
5. **No sub-agents for now.** One agent and one tool loop, so every count and citation
   traces to a tool call (CLAUDE.md), and answers do not get slower.

**How we know:** tool-choice checks in the question set (#101). `expect.tools_not` lists
tools a question must not call. Every PR that adds an agent tool also adds at least one
question that should call it and two that should not.

Order: #101 (the checks), then #102 (slimming, judged with them), then merges as #73 and
#74 come up, then #103 only if triggered.

## Consequences
- New tools cost more design work up front (fit them into an existing tool, or argue
  why not), but the agent's choice stays small.
- The question set becomes the gauge for tool choice as well as for answers. Its run time
  (about 2 hours for both models with 3 runs) is the price of each agent-tool change.
- Revisit if the agent model changes: a stronger model may handle more tools, a weaker
  one (e.g. a 14b) fewer.
