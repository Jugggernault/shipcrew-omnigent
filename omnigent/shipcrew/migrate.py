"""Apply the shipcrew Alembic lineage to a database."""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

VERSION_TABLE = "shipcrew_alembic_version"
_SCRIPT_LOCATION = Path(__file__).parent / "migrations"
_lock = threading.Lock()
_migrated: set[str] = set()


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Bring the shipcrew tables up to ``revision`` on ``engine``."""
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str = "base") -> None:
    """Roll the shipcrew tables back to ``revision`` on ``engine``."""
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def ensure_schema(engine: Engine) -> None:
    """Upgrade once per engine URL per process."""
    key = engine.url.render_as_string(hide_password=True)
    with _lock:
        if key in _migrated:
            return
        upgrade(engine)
        _migrated.add(key)
