"""Reference-list chunks are left out of search (#68, decision 0044). No network."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from chatcde import catalogue
from chatcde.schemas import Chunk, TextSource
from chatcde.store.bm25 import build_bm25
from chatcde.store.references import is_reference_list, reference_score
from chatcde.store.search import hybrid_search
from test_store_search import indexed_db, settings  # noqa: F401 (fixture)

#: Shapes seen in the real index (2026-11-15): numbered MDPI style, APA, author-year.
REFERENCES = [
    "4. Ricciardi, V.; Ramankutty, N.; Mehrabi, Z. How Much of the World's Food Do "
    "Smallholders Produce? Glob. Food Secur. 2018, 17, 64–72. [CrossRef] 5. Graeub, "
    "B.E.; Chappell, M.J. The State of Family Farms in the World. World Dev. 2016, 87, "
    "1–15. [CrossRef]",
    "Corbetta, S. (2021). Educación Ambiental y formación ciudadana. Revista "
    "Departamento de Ciencia Política, 20(2), 86-109. https://revistas.unal.edu.co/ "
    "Ulrich, Ann. 2014. Export-Oriented Horticultural Production in Laikipia, Kenya. "
    "Sustainability 6 (1): 336–47.",
    "Clark WC. 1987. Scale relationships in the interactions of climate, ecosystems, "
    "and societies. In: Land KC, Schneider SH, editors. Forecasting in the Social and "
    "Natural Sciences. Dordrecht: Reidel; pp. 337–378. Ives CD, Kendal D. 2014. The "
    "role of social values. J Environ Manag. 144:67–72. doi: 10.1016/j.jenvman.2014.",
]
PROSE = [
    "Results show that household food insecurity was not exclusively the result of a "
    "lack of private property rights, as many farmers had retained their property "
    "rights. Instead, a major factor was access to farm technology (Ribot and Peluso, "
    "2003; Mutea et al., 2019), which was linked to social relations.",
    "Die Universität Bern misst seit 2017 jährlich, wie viele Lehrveranstaltungen "
    "nachhaltige Entwicklung behandeln. Im Jahr 2021 waren es 1250 von 4300 Kursen, "
    "also rund 29 Prozent, in allen acht Fakultäten.",
]


@pytest.mark.parametrize("text", REFERENCES)
def test_reference_lists_score_above_the_cutoff(text: str):
    assert reference_score(text) >= 0.06
    assert is_reference_list(text, 0.06)


@pytest.mark.parametrize("text", PROSE)
def test_prose_with_citations_and_years_stays_below(text: str):
    assert reference_score(text) < 0.06
    assert not is_reference_list(text, 0.06)


def test_no_cutoff_keeps_everything():
    assert reference_score("") == 0.0
    assert not is_reference_list(REFERENCES[0], None)


@pytest.fixture
def ref_db(indexed_db: Path, tmp_path: Path) -> Path:  # noqa: F811 (fixture)
    """The search fixture plus a full-text and an abstract chunk of references."""
    db = Path(shutil.copy(indexed_db, tmp_path / "r.duckdb"))
    con = catalogue.connect(db)
    record = con.execute("SELECT record_id FROM records LIMIT 1").fetchone()[0]
    text = "Zyxwvut " + REFERENCES[0]
    rows = [
        Chunk(
            chunk_id=f"{record}:900",
            record_id=record,
            text=text,
            page_start=12,
            page_end=12,
            token_count=80,
            source=TextSource.FULLTEXT,
        ),
        Chunk(
            chunk_id=f"{record}:901",
            record_id=record,
            text=text,
            token_count=80,
            source=TextSource.ABSTRACT_BORIS,
        ),
    ]
    catalogue.insert(con, "chunks", rows)
    con.close()
    build_bm25(db, force=True)
    return db


def test_search_leaves_out_full_text_reference_lists(ref_db: Path):
    with duckdb.connect(str(ref_db), read_only=True) as con:
        cut = hybrid_search(
            con,
            None,
            "zyxwvut",
            settings(mode="bm25", rerank=False, reference_cutoff=0.06),
        )
        kept = hybrid_search(con, None, "zyxwvut", settings(mode="bm25", rerank=False))
    assert [h.chunk.chunk_id.split(":")[1] for h in cut] == ["901"]  # abstract stays
    assert {h.chunk.chunk_id.split(":")[1] for h in kept} == {"900", "901"}
