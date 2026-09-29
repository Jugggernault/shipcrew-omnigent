"""Alembic environment for the shipcrew tables.

A separate lineage with its own version table, so it never forks omnigent's
migration chain. Driven programmatically by :mod:`omnigent.shipcrew.migrate`.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from omnigent.shipcrew.migrate import VERSION_TABLE
from omnigent.shipcrew.models import ShipcrewBase

config = context.config
target_metadata = ShipcrewBase.metadata


def _run_with_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run against the shared connection, or open one from ``sqlalchemy.url``."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("shipcrew migrations need a connection or sqlalchemy.url")
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run_with_connection(conn)


run_migrations_online()
