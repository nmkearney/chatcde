"""``chatcde migrate``: stored dimension names follow renames (decision 0040)."""

from __future__ import annotations

from pathlib import Path

import duckdb
from typer.testing import CliRunner

from chatcde import catalogue
from chatcde.cli import app
from chatcde.schemas import Dimension


def _old_catalogue(path: Path) -> None:
    """A catalogue with tags stored under the old name ``output_type``."""
    con = duckdb.connect(str(path))
    for table in catalogue.DIMENSION_TABLES:
        con.execute(f"CREATE TABLE {table} (record_id VARCHAR, dimension VARCHAR)")
        con.execute(
            f"INSERT INTO {table} VALUES ('r1', 'output_type'), ('r1', 'sdg'), "
            "('r2', 'output_type')"
        )
    con.close()


def test_renamed_dimensions_point_to_current_ones():
    for old, new in catalogue.RENAMED_DIMENSIONS.items():
        assert Dimension(new)
        assert old not in {d.value for d in Dimension}


def test_migrate_renames_and_is_idempotent(tmp_path: Path):
    path = tmp_path / "c.duckdb"
    _old_catalogue(path)
    con = duckdb.connect(str(path))
    assert catalogue.migrate(con) == {"tags.output_type": 2, "tag_runs.output_type": 2}
    assert catalogue.migrate(con) == {"tags.output_type": 0, "tag_runs.output_type": 0}
    for table in catalogue.DIMENSION_TABLES:
        rows = con.execute(f"SELECT dimension FROM {table} ORDER BY 1").fetchall()
        assert rows == [("audience_type",), ("audience_type",), ("sdg",)]
    con.close()


def test_migrate_skips_missing_tables(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "empty.duckdb"))
    assert catalogue.migrate(con) == {}
    con.close()


def test_cli_migrate(tmp_path: Path):
    db = tmp_path / "c.duckdb"
    _old_catalogue(db)
    override = tmp_path / "o.yaml"
    override.write_text(
        f'data:\n  catalogue: "{db.as_posix()}"\nlogging:\n  file: null\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    first = runner.invoke(app, ["--config", str(override), "migrate"])
    assert first.exit_code == 0, first.output
    assert "tags: 2 rows output_type -> audience_type" in first.output
    again = runner.invoke(app, ["--config", str(override), "migrate"])
    assert "up to date" in again.output
    missing = tmp_path / "m.yaml"
    missing.write_text(
        f'data:\n  catalogue: "{(tmp_path / "none.duckdb").as_posix()}"\n'
        "logging:\n  file: null\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["--config", str(missing), "migrate"]).exit_code == 1
