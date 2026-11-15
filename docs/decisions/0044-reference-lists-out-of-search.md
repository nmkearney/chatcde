# 0044: Reference-list chunks are left out of search, scored at query time

- Status: accepted
- Date: 2026-11-15

## Context
Reference lists match almost any topic, since they name many titles, and they say nothing
about CDE's own findings. Yet they filled the top search hits (#68). In the #5 baseline,
"food security of smallholders around Mount Kenya" returned 5 reference chunks among its
8 passages, all from one record. The agent then had fewer real passages to cite, or it
cited reference pages.

The chunker (decision 0011) does not keep section headings (`Chunk.section` is always
empty). Headings alone would also miss reference lists that start mid-chunk or carry no
"References" line.

## Decision
- **A reference score per chunk** (`chatcde.store.references.reference_score`):
  reference-entry signatures per word. The signatures:
  - a year followed by a full stop (`(2021).`, `2016. Title`) or by `;` and a volume;
  - volume, issue and pages (`14(2): 379`, `87, 1–15`);
  - `pp.`, `In:`, `Eds.`, ISBN/ISSN, `accessed`;
  - links, DOIs, `CrossRef`;
  - author names with initials (`Surname, A. B.`, `A. B. Surname,`).

  In-text citations such as `(Ribot and Peluso, 2003; Mutea et al., 2019)` match few of
  them.
- **Cut-off 0.06, in the config:** `retrieval.reference_cutoff` (null keeps everything).
  On the real index (92,037 full-text chunks) the scores are bimodal. Hand checks of
  random samples (2026-11-15):
  - 0.04–0.06: 6 of 8 reference lists, 2 prose;
  - 0.06–0.12: 31 of 32 reference lists (one regression table);
  - 30 random flagged chunks: 29 reference lists. One is a boundary chunk: the last
    paragraph of a policy brief, then its references.

  A lower cut-off would start dropping prose. A higher one keeps sparse reference styles
  (APA with URLs, legal sources).
- **Scored at search time, on the candidates only.** `hybrid_search` loads the fused
  candidates (up to 2 × `top_k`), leaves out full-text chunks at or above the cut-off,
  and then cuts to `top_k` and reranks. This replaces the plan for a `chunk_flags`
  table:
  - the regex costs milliseconds on 80 chunks;
  - nothing is re-indexed;
  - a new cut-off takes effect at once;
  - the shared `Chunk` schema is unchanged.

  Abstract and metadata chunks are never left out.
- **Opt out:** `chatcde search --include-references` and `chatcde index search
  --include-references`. The agent's `search_publications` always leaves them out; its
  signature (shared contract) is unchanged. `read_section` still reads every page.

## Consequences
- 11,704 of 92,037 full-text chunks (12.7%) in 953 of 1,542 records are left out of
  search. 3 records keep no full-text chunk in search; they keep their abstract or
  metadata chunk.
- Four queries from the baseline, top 8 before → after:
  - land concessions in Laos: 5 reference chunks → 0;
  - Mount Kenya food security: 5 → 0, and 2 records found instead of 1;
  - coffee value chains: 3 → 0;
  - land acquisitions in Laos: 2 → 0.
- Boundary chunks lose their prose paragraph to search (about 1 in 30 flagged chunks).
  The 50-token overlap with the previous chunk keeps part of it.
- Questions that need the references themselves ("who does CDE cite on …") are not
  served. That would be a separate tool.
- Revisit:
  - re-score after a chunker change (`REFERENCE_SCORE_VERSION`);
  - if `Chunk.section` is ever filled from headings, combine it with the score;
  - recall was not measured: how many reference chunks stay below 0.06.
