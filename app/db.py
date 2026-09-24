from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATE_LOCK = 7_310_001
SCAN_LOCK = 7_310_002

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


def migrate() -> list[str]:
    """Apply any SQL files in app/migrations that have not run yet. Safe to call from every process."""
    applied: list[str] = []
    with conn() as c:
        c.execute("select pg_advisory_lock(%s)", (MIGRATE_LOCK,))
        try:
            c.execute(
                "create table if not exists schema_migrations (name text primary key, applied_at timestamptz default now())"
            )
            done = {r["name"] for r in c.execute("select name from schema_migrations").fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                c.execute(path.read_text())
                c.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                applied.append(path.name)
            c.commit()
        finally:
            c.execute("select pg_advisory_unlock(%s)", (MIGRATE_LOCK,))
            c.commit()
    return applied
