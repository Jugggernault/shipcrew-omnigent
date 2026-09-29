"""The shipcrew Alembic lineage: upgrade, downgrade, and parity with the models."""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from omnigent.shipcrew import migrate
from omnigent.shipcrew.models import ShipcrewBase

_TABLES = {"shipcrew_missions", "shipcrew_tasks"}


def _tables(url: str) -> set[str]:
    return set(inspect(create_engine(url)).get_table_names())


def test_upgrade_then_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = create_engine(url)
    migrate.upgrade(engine)
    assert _tables(url) >= _TABLES | {migrate.VERSION_TABLE}
    with engine.connect() as conn:
        version = conn.execute(text(f"SELECT version_num FROM {migrate.VERSION_TABLE}"))
        assert version.scalar_one() == "sc0005pj"
    migrate.downgrade(engine)
    assert not _tables(url) & _TABLES
    # Upgrading again after a full downgrade restores the schema.
    migrate.upgrade(engine)
    assert _tables(url) >= _TABLES


def test_leaves_foreign_tables_alone(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('upstream_head')"))
    migrate.upgrade(engine)
    migrate.downgrade(engine)
    with engine.connect() as conn:
        upstream = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert upstream == "upstream_head"


def test_migration_matches_models(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    migrate.upgrade(engine)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"version_table": migrate.VERSION_TABLE, "compare_type": True}
        )
        diff = [
            d
            for d in compare_metadata(context, ShipcrewBase.metadata)
            if not (d[0] == "remove_table" and d[1].name == migrate.VERSION_TABLE)
        ]
    assert diff == []


def test_ensure_schema_runs_once_per_engine(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    migrate.ensure_schema(engine)
    migrate.ensure_schema(engine)
    assert set(inspect(engine).get_table_names()) >= _TABLES
