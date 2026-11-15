"""The expertise finder behind :func:`chatcde.tools.core.find_expertise`.

Issue #71; see docs/decisions/0046-expertise-finder.md. Two steps, both code:

1. **People** (:func:`build_people`). BORIS lists authors per record, with
   spellings that drift (``Liniger, Hanspeter`` / ``Liniger, Hans Peter``) and an
   ORCID only sometimes. The catalogue does not keep the BORIS person ID, so
   author rows are merged into people by ORCID and name:

   * rows with the same ORCID are one person;
   * rows with the same name key (accents, case, spaces, hyphens and dots in the
     given names ignored) are one person, unless the key carries two different
     ORCIDs: then only the rows with each ORCID merge, and rows without one stay
     apart;
   * a spelling with initials only (``Allan, E.``) joins the one person with that
     surname whose given names start with those initials, and stays apart when
     there are several.

   A person is CDE with CDE affiliation on at least one record (person-level).

2. **Matching records** (:func:`matching_records`). The filters (tags, years,
   units, ...) give a set of records. With a topic, a deep hybrid search inside
   that set keeps the records whose best passage scores at least
   ``experts.min_score`` after reranking, at most ``experts.max_records``.
   Without a topic every record passing the filters matches, so the counts are
   exact tag counts.

People are then ranked by matching records, recent matching records and latest
year. Every number comes from the catalogue; nothing here calls an LLM.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import duckdb

from chatcde.harvest.common import now
from chatcde.schemas import Expert, ExpertiseResult, Filters, Passage
from chatcde.store.filters import filter_record_ids, is_empty
from chatcde.store.search import SearchSettings
from chatcde.tools.context import ToolContext, ToolError
from chatcde.tools.publications import search_passages

log = logging.getLogger(__name__)

_SEPARATORS = re.compile(r"[\s.\-‐‑]+")

#: Search hook: (context, query, expanded filters, depth) -> (passages, reranked).
TopicSearch = Callable[[ToolContext, str, Filters, int], tuple[list[Passage], bool]]


# ---------------------------------------------------------------------------
# Names and people
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower-case ``text`` and strip accents (``Zähringer`` -> ``zahringer``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def split_name(name: str) -> tuple[str, list[str]]:
    """``"Surname, Given Names"`` -> ``(folded surname, folded given-name parts)``.

    A name without a comma is all surname (e.g. an organisation as author).
    """
    surname, _, given = name.partition(",")
    parts = [p for p in _SEPARATORS.split(fold(given)) if p]
    return " ".join(fold(surname).split()), parts


def normalise_name(name: str) -> tuple[str, str]:
    """The name key used to merge spellings: ``(surname, given names run together)``.

    ``Liniger, Hanspeter`` and ``Liniger, Hans Peter`` give the same key;
    ``Allan, E.`` and ``Allan, Eric`` do not (see :func:`build_people`).
    """
    surname, parts = split_name(name)
    return surname, "".join(parts)


def _initials_only(parts: list[str]) -> bool:
    """True for given names written as initials only (``E.``, ``M. G.``)."""
    return bool(parts) and all(len(p) == 1 for p in parts)


@dataclass
class Person:
    """One person: author rows merged by ORCID and name (see :func:`build_people`)."""

    spellings: Counter[str] = field(default_factory=Counter)
    orcid: str | None = None
    is_cde: bool = False
    units: set[str] = field(default_factory=set)
    records: set[str] = field(default_factory=set)
    first_author: set[str] = field(default_factory=set)
    by_initials: bool = False

    @property
    def name(self) -> str:
        """The most frequent spelling (ties: the longest, then alphabetical)."""
        return min(self.spellings, key=lambda s: (-self.spellings[s], -len(s), s))

    @property
    def variants(self) -> list[str]:
        """Every other spelling, alphabetical."""
        main = self.name
        return sorted(s for s in self.spellings if s != main)


@dataclass
class People:
    """Every person in the catalogue, and who wrote each record."""

    people: list[Person]
    by_record: dict[str, list[int]]  # record_id -> indexes into people


class _UnionFind:
    """Minimal union-find over hashable nodes."""

    def __init__(self) -> None:
        self.parent: dict[object, object] = {}

    def find(self, x: object) -> object:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: object, b: object) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_people(con: duckdb.DuckDBPyConnection) -> People:
    """Merge the ``authors`` rows into people (rules in the module docstring).

    Args:
        con: Open catalogue with an ``authors`` table.

    Returns:
        The people, in a stable order (by main spelling), and each record's
        authors as indexes into them.
    """
    rows = con.execute(
        "SELECT record_id, position, name, orcid, is_cde, cde_unit FROM authors"
    ).fetchall()
    uf = _UnionFind()
    orcids_of_key: dict[tuple[str, str], set[str]] = {}
    for _rid, _pos, name, orcid, _cde, _unit in rows:
        key = normalise_name(name)
        if orcid:
            orcids_of_key.setdefault(key, set()).add(orcid)

    def node(name: str, orcid: str | None) -> tuple[object, ...]:
        key = normalise_name(name)
        known = orcids_of_key.get(key, set())
        if orcid:
            return ("orcid", orcid)
        if len(known) == 1:  # the key's only ORCID: the same person
            return ("orcid", next(iter(known)))
        if len(known) > 1:  # two people share this key: keep the row apart
            return ("ambiguous", key, name)
        return ("key", key)

    nodes = [node(r[2], r[3]) for r in rows]

    # Initials-only spellings join the single matching full-name person.
    full: dict[str, list[tuple[str, object]]] = {}  # surname -> (initials, root)
    for n, r in zip(nodes, rows, strict=True):
        surname, parts = split_name(r[2])
        if parts and not _initials_only(parts):
            full.setdefault(surname, []).append(("".join(p[0] for p in parts), n))
    by_initials: set[object] = set()
    for n, r in zip(nodes, rows, strict=True):
        surname, parts = split_name(r[2])
        if not _initials_only(parts) or n[0] != "key":  # ORCID rows already merged
            continue
        initials = "".join(parts)
        roots = {
            uf.find(m) for ini, m in full.get(surname, []) if ini.startswith(initials)
        }
        if len(roots) == 1:
            uf.union(next(iter(roots)), n)
            by_initials.add(n)
        elif len(roots) > 1:
            log.debug("%r matches %d people; not merged", r[2], len(roots))

    index: dict[object, int] = {}
    people: list[Person] = []
    by_record: dict[str, list[int]] = {}
    for n, (rid, pos, name, orcid, cde, unit) in zip(nodes, rows, strict=True):
        root = uf.find(n)
        if root not in index:
            index[root] = len(people)
            people.append(Person())
        i = index[root]
        p = people[i]
        p.spellings[name] += 1
        p.orcid = p.orcid or orcid
        p.is_cde = p.is_cde or bool(cde)
        if unit:
            p.units.add(unit)
        p.records.add(rid)
        if pos == 0:
            p.first_author.add(rid)
        p.by_initials = p.by_initials or n in by_initials
        if i not in by_record.setdefault(rid, []):
            by_record[rid].append(i)
    log.debug("%d author rows -> %d people", len(rows), len(people))
    return People(people=people, by_record=by_record)


# ---------------------------------------------------------------------------
# Matching records
# ---------------------------------------------------------------------------


def default_search(
    ctx: ToolContext, query: str, filters: Filters, depth: int
) -> tuple[list[Passage], bool]:
    """The real search: ``depth`` chunks, reranked. Returns ``(passages, reranked)``."""
    settings = SearchSettings.from_config(ctx.cfg, top_n=depth, candidates=depth)
    return search_passages(ctx, query, filters, settings), settings.will_rerank


@dataclass
class Matches:
    """The records that match a topic and filters, best first."""

    record_ids: list[str]
    passages: dict[str, Passage]  # best passage per record (search only)
    searched: bool
    cut: int = 0  # records above the score left out by experts.max_records


def matching_records(
    ctx: ToolContext,
    con: duckdb.DuckDBPyConnection,
    topic: str,
    filters: Filters,
    search: TopicSearch | None = None,
) -> Matches:
    """Step 2: the records behind the ranking (rules in the module docstring).

    Args:
        ctx: Tool context (config, endpoint).
        con: Open catalogue.
        topic: Search text; empty for a pure filter (tag) match.
        filters: Already expanded filters.
        search: Search hook; default :func:`default_search` (tests replace it).

    Raises:
        ToolError: Neither a topic nor a restricting filter.
    """
    s = ctx.cfg.experts
    if not topic.strip():
        if is_empty(filters):
            raise ToolError(
                "give a topic, filters or both (e.g. topic='pastoralism' or "
                "filters={'actors': ['ACT-pastoralists']})"
            )
        ids = filter_record_ids(con, filters) or []
        rows = con.execute(
            "SELECT record_id FROM records WHERE record_id IN "
            "(SELECT unnest(?::VARCHAR[])) ORDER BY year DESC NULLS LAST, record_id",
            [ids],
        ).fetchall()
        return Matches([r for (r,) in rows], {}, searched=False)

    passages, reranked = (search or default_search)(
        ctx, topic, filters, s.search_candidates
    )
    best: dict[str, Passage] = {}
    for p in passages:
        if reranked and p.score < s.min_score:
            continue
        best.setdefault(p.citation.record_id, p)
    ordered = sorted(best, key=lambda r: -best[r].score)
    kept = ordered[: s.max_records]
    return Matches(
        kept,
        {r: best[r] for r in kept},
        searched=True,
        cut=len(ordered) - len(kept),
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@dataclass
class ExpertiseReport:
    """The tool result plus what the ``chatcde experts`` workflow also needs."""

    result: ExpertiseResult
    matches: Matches
    by_initials: list[str]  # main names of listed people merged by initials


def expertise_report(
    ctx: ToolContext,
    topic: str = "",
    filters: Filters | None = None,
    k: int = 10,
    cde_only: bool = True,
    search: TopicSearch | None = None,
) -> ExpertiseReport:
    """Rank people by matching records; see :func:`chatcde.tools.core.find_expertise`.

    Raises:
        ToolError: ``k < 1``, no catalogue or authors, unknown filter codes, no
            topic and no filters, or the search failing.
    """
    if k < 1:
        raise ToolError("k must be at least 1")
    s = ctx.cfg.experts
    if k > s.max_k:
        log.info("k=%d capped at experts.max_k=%d", k, s.max_k)
        k = s.max_k
    if not ctx.cfg.data.catalogue.is_file():
        raise ToolError(f"no catalogue at {ctx.cfg.data.catalogue}")
    filters = ctx.expand(filters)
    with ctx.catalogue() as con:
        has = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'authors'"
        ).fetchone()
        if not (has and has[0]):
            raise ToolError("the catalogue has no authors; run `chatcde harvest`")
        matches = matching_records(ctx, con, topic, filters, search)
        people = build_people(con)
        years = dict(
            con.execute(
                "SELECT record_id, year FROM records WHERE record_id IN "
                "(SELECT unnest(?::VARCHAR[]))",
                [matches.record_ids],
            ).fetchall()
        )

    since = now().year - s.recent_years + 1
    matched: dict[int, list[str]] = {}
    without_cde = 0
    for rid in matches.record_ids:
        authors = people.by_record.get(rid, [])
        if not any(people.people[i].is_cde for i in authors):
            without_cde += 1
        for i in authors:
            matched.setdefault(i, []).append(rid)

    experts: list[tuple[Expert, bool]] = []
    for i, rids in matched.items():
        p = people.people[i]
        if cde_only and not p.is_cde:
            continue
        ys = [years[r] for r in rids if years.get(r) is not None]
        experts.append(
            (
                Expert(
                    name=p.name,
                    variants=p.variants,
                    orcid=p.orcid,
                    is_cde=p.is_cde,
                    cde_units=sorted(p.units),
                    record_count=len(rids),
                    first_author_count=sum(r in p.first_author for r in rids),
                    recent_count=sum(y >= since for y in ys),
                    first_year=min(ys) if ys else None,
                    last_year=max(ys) if ys else None,
                    total_records=len(p.records),
                    record_ids=rids,
                ),
                p.by_initials,
            )
        )
    experts.sort(
        key=lambda e: (
            -e[0].record_count,
            -e[0].recent_count,
            -(e[0].last_year or 0),
            e[0].name,
        )
    )
    shown = experts[:k]
    result = ExpertiseResult(
        topic=topic.strip() or None,
        filters=filters,
        matched_records=len(matches.record_ids),
        search_records=len(matches.record_ids) if matches.searched else 0,
        records_without_cde_author=without_cde,
        people_found=len(experts),
        experts=[e for e, _ in shown],
    )
    log.info(
        "find_expertise %r: %d records, %d people, %d shown",
        topic,
        result.matched_records,
        result.people_found,
        len(shown),
    )
    return ExpertiseReport(
        result=result,
        matches=matches,
        by_initials=[e.name for e, merged in shown if merged],
    )


def find_expertise(
    ctx: ToolContext,
    topic: str = "",
    filters: Filters | None = None,
    k: int = 10,
    cde_only: bool = True,
    search: TopicSearch | None = None,
) -> ExpertiseResult:
    """The tool: :func:`expertise_report` without the workflow extras."""
    return expertise_report(ctx, topic, filters, k, cde_only, search).result
