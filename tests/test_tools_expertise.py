"""Tests for ``find_expertise`` and the ``chatcde experts`` workflow (#71). No network.

A toy catalogue holds the spelling cases seen in BORIS: one person with and
without an ORCID and with spaced or joined given names, an initials-only
spelling, two people sharing a name key, and a record without a CDE author.
The search is a fake that returns fixed passages; the model is the scripted
fake from ``test_agent.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from chatcde import catalogue
from chatcde.cli import app
from chatcde.config import load_config
from chatcde.schemas import (
    Author,
    Chunk,
    Citation,
    Dimension,
    Filters,
    Passage,
    Provenance,
    Record,
    Tag,
    TextSource,
)
from chatcde.store.endpoint import EndpointClient
from chatcde.tools.context import ToolContext, ToolError
from chatcde.tools.expertise import (
    build_people,
    expertise_report,
    find_expertise,
    normalise_name,
)
from chatcde.tools.specs import tool_specs
from chatcde.workflows.experts import NO_REASON, render_experts, run_experts
from test_agent import ScriptedLLM, reply

NOW = datetime(2026, 11, 15, tzinfo=UTC)
THIS_YEAR = datetime.now(UTC).year
PASTORALISTS = "ACT-pastoralists"


def _record(rid: str, year: int) -> Record:
    return Record(
        record_id=rid,
        title=f"Record {rid}",
        abstract=f"Abstract of {rid}.",
        year=year,
        pub_type="article",
        boris_url=f"https://boris.test/{rid}",
        harvested_at=NOW,
    )


def _author(
    rid: str, pos: int, name: str, orcid: str | None = None, cde: bool = True,
    unit: str | None = None,
) -> Author:  # fmt: skip
    return Author(
        record_id=rid, position=pos, name=name, orcid=orcid, is_cde=cde, cde_unit=unit
    )


def _tag(rid: str) -> Tag:
    return Tag(
        record_id=rid,
        dimension=Dimension.ACTORS,
        term=PASTORALISTS,
        role="studied",
        confidence=0.9,
        source=TextSource.ABSTRACT_BORIS,
        provenance=Provenance(code_version="test", created_at=NOW),
    )


@pytest.fixture
def people_db(tmp_path: Path) -> Path:
    """Seven records; r1, r2 and r7 are tagged with pastoralists."""
    records = [
        _record("r1", THIS_YEAR),
        _record("r2", 2015),
        _record("r3", 2010),
        _record("r4", 2020),
        _record("r5", 2019),
        _record("r6", 2018),
        _record("r7", 2012),
    ]
    authors = [
        _author("r1", 0, "Liniger, Hans Peter", "X", unit="Sustainable Land Systems"),
        _author("r1", 1, "Smith, John", cde=False),
        _author("r2", 0, "Allan, E.", cde=False),
        _author("r2", 1, "Liniger, Hanspeter", cde=False),
        _author("r3", 0, "Allan, Eric", "Y"),
        _author("r3", 1, "Müller, Thomas", "A"),
        _author("r4", 0, "Muller, Thomas", "B"),
        _author("r4", 1, "Müller, Thomas"),
        _author("r4", 2, "Brown, A."),
        _author("r5", 0, "Brown, Anna"),
        _author("r6", 0, "Brown, Alex"),
        _author("r7", 0, "Ext, Only", cde=False),
    ]
    db = tmp_path / "people.duckdb"
    con = catalogue.connect(db)
    catalogue.ensure_tables(con, ["records", "authors", "tags"])
    catalogue.insert(con, "records", records)
    catalogue.insert(con, "authors", authors)
    catalogue.insert(con, "tags", [_tag("r1"), _tag("r2"), _tag("r7")])
    con.close()
    return db


def _context(
    tmp_path: Path, db: Path, extra: str = "", llm: ScriptedLLM | None = None
) -> ToolContext:
    path = tmp_path / "o.yaml"
    path.write_text(
        f'data:\n  catalogue: "{db.as_posix()}"\nlogging:\n  file: null\n' + extra,
        encoding="utf-8",
    )
    client = None
    if llm is not None:
        client = EndpointClient(
            "https://llm.test/v1",
            "k",
            timeout_s=5,
            max_retries=0,
            transport=httpx.MockTransport(llm),
            sleep=lambda _: None,
        )
    return ToolContext(load_config(path), client)


def _passage(rid: str, score: float) -> Passage:
    return Passage(
        chunk=Chunk(
            chunk_id=f"{rid}:0",
            record_id=rid,
            text=f"A passage of {rid} on pastoralism.",
            token_count=8,
            source=TextSource.ABSTRACT_BORIS,
        ),
        score=score,
        citation=Citation(
            record_id=rid,
            title=f"Record {rid}",
            year=2020,
            doi=None,
            boris_url=f"https://boris.test/{rid}",
        ),
    )


class FakeSearch:
    """Returns fixed passages (r3 twice) and says whether they were reranked."""

    def __init__(self, reranked: bool = True) -> None:
        self.reranked = reranked
        self.calls: list[tuple[str, Filters, int]] = []

    def __call__(self, ctx, query, filters, depth):  # type: ignore[no-untyped-def]
        self.calls.append((query, filters, depth))
        hits = [_passage("r3", 0.9), _passage("r1", 0.2), _passage("r2", 0.5)]
        return [*hits, _passage("r3", 0.8)], self.reranked


def _by_name(people):  # type: ignore[no-untyped-def]
    return {p.name: p for p in people}


# --- people ---------------------------------------------------------------------------


def test_normalise_name_ignores_accents_case_and_spacing():
    assert normalise_name("Liniger, Hanspeter") == normalise_name("Liniger, Hans Peter")
    assert normalise_name("Zähringer, Julie") == ("zahringer", "julie")
    assert normalise_name("CDE") == ("cde", "")


def test_build_people_merges_spellings(people_db: Path):
    con = duckdb.connect(str(people_db), read_only=True)
    people = build_people(con)
    con.close()
    by = _by_name(people.people)

    liniger = by["Liniger, Hans Peter"]  # ORCID row + no-ORCID joined spelling
    assert liniger.variants == ["Liniger, Hanspeter"]
    assert liniger.records == {"r1", "r2"} and liniger.is_cde
    assert liniger.units == {"Sustainable Land Systems"}

    allan = next(p for p in people.people if "Allan, Eric" in p.spellings)
    assert set(allan.spellings) == {"Allan, Eric", "Allan, E."}  # unique initials
    assert allan.by_initials and allan.orcid == "Y"

    # Two ORCIDs share the key "muller, thomas": three separate people.
    mullers = [p for p in people.people if normalise_name(p.name)[0] == "muller"]
    assert sorted(p.orcid or "-" for p in mullers) == ["-", "A", "B"]

    # "Brown, A." could be Anna or Alex: not merged.
    assert "Brown, A." in by and by["Brown, A."].records == {"r4"}
    assert by["Brown, Anna"].records == {"r5"}


# --- matching and ranking -------------------------------------------------------------


def test_tag_only_match_ranks_cde_people(people_db: Path, tmp_path: Path):
    ctx = _context(tmp_path, people_db)
    r = find_expertise(ctx, "", Filters(actors=[PASTORALISTS]))
    assert r.topic is None and r.search_records == 0
    assert r.matched_records == 3
    assert r.records_without_cde_author == 1  # r7
    names = [e.name for e in r.experts]
    assert names == ["Liniger, Hans Peter", "Allan, Eric"]
    top = r.experts[0]
    assert top.record_ids == ["r1", "r2"]  # newest first without a topic
    assert (top.record_count, top.first_author_count, top.recent_count) == (2, 1, 1)
    assert (top.first_year, top.last_year, top.total_records) == (2015, THIS_YEAR, 2)
    assert top.orcid == "X" and top.variants == ["Liniger, Hanspeter"]


def test_all_authors_lists_external_people(people_db: Path, tmp_path: Path):
    ctx = _context(tmp_path, people_db)
    r = find_expertise(ctx, "", Filters(actors=[PASTORALISTS]), cde_only=False)
    names = {e.name: e.is_cde for e in r.experts}
    assert names["Smith, John"] is False and names["Ext, Only"] is False
    assert r.people_found == 4


def test_tag_counts_match_sql_on_fixture(fixture_db: Path, tmp_path: Path):
    con = duckdb.connect(str(fixture_db), read_only=True)
    term, n = con.execute(
        "SELECT term, count(DISTINCT record_id) FROM tags WHERE dimension = 'country' "
        "AND confidence >= 0.5 GROUP BY term ORDER BY 2 DESC, 1 LIMIT 1"
    ).fetchone()
    con.close()
    ctx = _context(tmp_path, fixture_db)
    r = find_expertise(ctx, "", Filters(countries=[term]), k=25)
    assert r.matched_records == n
    assert all(e.is_cde for e in r.experts)
    assert all(e.record_count == len(e.record_ids) > 0 for e in r.experts)


def test_topic_match_applies_score_and_cap(people_db: Path, tmp_path: Path):
    search = FakeSearch()
    ctx = _context(tmp_path, people_db)
    rep = expertise_report(ctx, "pastoralism", None, search=search)
    assert rep.matches.record_ids == ["r3", "r2"]  # r1 (0.2) is below min_score
    assert rep.result.search_records == 2
    assert search.calls[0][2] == ctx.cfg.experts.search_candidates

    capped = _context(tmp_path, people_db, "experts:\n  max_records: 1\n")
    rep = expertise_report(capped, "pastoralism", None, search=FakeSearch())
    assert rep.matches.record_ids == ["r3"] and rep.matches.cut == 1


def test_topic_match_without_rerank_keeps_all(people_db: Path, tmp_path: Path):
    ctx = _context(tmp_path, people_db)
    rep = expertise_report(ctx, "pastoralism", None, search=FakeSearch(False))
    assert rep.matches.record_ids == ["r3", "r2", "r1"]


def test_ranking_ties_are_deterministic(people_db: Path, tmp_path: Path):
    ctx = _context(tmp_path, people_db)
    r = find_expertise(ctx, "", Filters(year_from=2018, year_to=2019))
    assert [e.name for e in r.experts] == ["Brown, Anna", "Brown, Alex"]


def test_k_cap_and_errors(people_db: Path, tmp_path: Path):
    ctx = _context(tmp_path, people_db, "experts:\n  max_k: 1\n")
    assert len(find_expertise(ctx, "", Filters(year_from=2000), k=10).experts) == 1
    with pytest.raises(ToolError, match="at least 1"):
        find_expertise(ctx, "", Filters(year_from=2000), k=0)
    with pytest.raises(ToolError, match="topic, filters or both"):
        find_expertise(ctx, "  ", None)


def test_agent_sees_find_expertise():
    spec = tool_specs(["find_expertise"])["find_expertise"]
    params = spec.definition()["function"]["parameters"]
    assert set(params["properties"]) == {"topic", "filters", "k", "cde_only"}
    assert "$ref" not in json.dumps(params)


# --- the workflow ---------------------------------------------------------------------


def _reasons(**over) -> dict:  # type: ignore[no-untyped-def]
    data = {
        "summary": "Allan looks best placed (a suggestion).",
        "people": [
            {"rank": 1, "reason": "Works on r3.", "evidence": ["r3", "made-up"]},
        ],
        "caveats": [],
    }
    data.update(over)
    return data


def test_workflow_reasons_are_checked(people_db: Path, tmp_path: Path):
    llm = ScriptedLLM([reply(json.dumps(_reasons()))])
    ctx = _context(tmp_path, people_db, llm=llm)
    r = run_experts(ctx, "pastoralism", None, search=FakeSearch())
    # Allan: r3 and r2 (as "Allan, E."); Liniger (r2, 2015) beats Müller (r3, 2010).
    names = [e.name for e in r.expertise.experts]
    assert names == ["Allan, Eric", "Liniger, Hans Peter", "Müller, Thomas"]
    assert r.judgement is not None
    first, second = r.judgement.people[:2]
    assert first.evidence == ["r3"] and r.dropped_ids == ["made-up"]
    assert second.reason == NO_REASON  # the model skipped rank 2
    assert len(r.judgement.people) == len(r.expertise.experts)
    request = llm.requests[0]
    assert request["response_format"]["json_schema"]["name"] == "experts_reasons"
    sent = json.loads(request["messages"][1]["content"])
    assert sent["people"][0]["records"][0]["record_id"] == "r3"
    assert r.model == ctx.cfg.models.agent and r.prompt_version == "experts.v1"

    console = Console(record=True, width=140)
    render_experts(console, r)
    text = console.export_text()
    assert "Allan, Eric" in text and "Works on r3." in text and "made-up" in text


def test_workflow_without_llm_makes_no_request(people_db: Path, tmp_path: Path):
    llm = ScriptedLLM([])
    ctx = _context(tmp_path, people_db, llm=llm)
    r = run_experts(ctx, "", Filters(actors=[PASTORALISTS]), use_llm=False)
    assert llm.requests == [] and r.judgement is None and r.model is None
    assert r.evidence[0].records[0].snippet == "Abstract of r1."
    assert any("no author with a CDE affiliation" in c for c in r.caveats)
    assert any("initials only" in c for c in r.caveats)


def test_workflow_reports_a_failed_reply(people_db: Path, tmp_path: Path):
    llm = ScriptedLLM([reply("not json")])
    ctx = _context(tmp_path, people_db, llm=llm)
    r = run_experts(ctx, "", Filters(year_from=2000))
    assert r.judgement is None and "not a valid" in (r.judgement_error or "")


def test_cli_experts_without_llm(people_db: Path, tmp_path: Path):
    path = tmp_path / "cli.yaml"
    path.write_text(
        f'data:\n  catalogue: "{people_db.as_posix()}"\nlogging:\n  file: null\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["--config", str(path), "experts", "--year-from", "2000", "--no-llm"]
    )
    assert result.exit_code == 0, result.output
    assert "Liniger, Hans Peter" in result.output
    empty = runner.invoke(app, ["--config", str(path), "experts", "--no-llm"])
    assert empty.exit_code == 1 and "topic, filters or both" in empty.output
