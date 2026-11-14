"""Compare counts and gaps at several confidence thresholds (#69, decision 0038).

Tag confidences are discrete (``tagging.confidence``: explicit 0.9, inferred 0.6,
weak 0.3; capped tags 0.4), so a threshold only matters at the steps between them.
This one-off script reports, for each threshold given, on the real catalogue:

* per dimension: records with at least one counted tag, and counted tags;
* for a few typical gap questions (``find_gaps``): how many cells are flagged, the
  top cells, and how many of the cells flagged at the first threshold stay flagged.

It reads the catalogue through the tool functions, so the rules are the same as in
answers (conference items left out of gaps, decision 0018). Output is Markdown on
stdout, for pasting into the decision record.

Usage::

    .venv/bin/python scripts/compare_confidence_thresholds.py 0.5 0.7
"""

from __future__ import annotations

import sys

import duckdb

from chatcde.config import load_config
from chatcde.schemas import Dimension, Filters, GapCell
from chatcde.tools import core

#: Gap questions to compare: (row, col, level).
GAP_QUESTIONS: list[tuple[Dimension, Dimension, str]] = [
    (Dimension.IMPACT_AREA, Dimension.METHOD, "group"),
    (Dimension.SUB_AREA, Dimension.METHOD, "group"),
    (Dimension.SUB_AREA, Dimension.DATA_TYPE, "group"),
    (Dimension.IMPACT_AREA, Dimension.SETTING, "group"),
    (Dimension.REGION, Dimension.IMPACT_AREA, "term"),
    (Dimension.SUB_AREA, Dimension.ACTORS, "group"),
]


def dimension_table(con: duckdb.DuckDBPyConnection, thresholds: list[float]) -> str:
    """Records with a counted tag, and counted tags, per dimension and threshold."""
    total = con.execute("SELECT count(*) FROM records").fetchone()[0]  # type: ignore[index]
    head = " | ".join(f"records ≥{t} | tags ≥{t}" for t in thresholds)
    lines = [
        f"Of {total:,} records.\n",
        f"| dimension | {head} |",
        "|---" * (1 + 2 * len(thresholds)) + "|",
    ]
    query = "SELECT DISTINCT dimension FROM tags ORDER BY 1"
    dims = [r[0] for r in con.execute(query).fetchall()]
    for dim in dims:
        cells = []
        for t in thresholds:
            recs, tags = con.execute(
                "SELECT count(DISTINCT record_id), count(*) FROM tags "
                "WHERE dimension = ? AND confidence >= ?",
                [dim, t],
            ).fetchone()  # type: ignore[misc]
            cells += [f"{recs:,} ({recs / total:.0%})", f"{tags:,}"]
        lines.append(f"| {dim} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _label(c: GapCell) -> str:
    return f"{c.row} × {c.col} ({c.count} vs {c.expected:.1f})"


def gap_table(thresholds: list[float]) -> str:
    """Flagged gap cells per question and threshold, with the top three."""
    lines = []
    for row, col, level in GAP_QUESTIONS:
        lines.append(f"\n**{row.value} × {col.value}, {level} level**\n")
        first: set[tuple[str, str]] | None = None
        for t in thresholds:
            f = Filters(min_confidence=t)
            cells = core.find_gaps(row, col, f, level=level)  # type: ignore[arg-type]
            keys = {(c.row, c.col) for c in cells}
            kept = "" if first is None else f"; {len(keys & first)} of {len(first)}"
            kept += "" if first is None else " kept"
            first = keys if first is None else first
            top = "; ".join(_label(c) for c in cells[:3]) or "none"
            lines.append(f"- ≥{t}: {len(cells)} gaps{kept}. Top: {top}")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    """Print both tables for the thresholds on the command line."""
    thresholds = [float(a) for a in argv] or [0.5, 0.7]
    cfg = load_config()
    with duckdb.connect(str(cfg.data.catalogue), read_only=True) as con:
        print(dimension_table(con, thresholds))
    print(gap_table(thresholds))


if __name__ == "__main__":
    main(sys.argv[1:])
