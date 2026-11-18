import os
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def get_engine() -> Engine:
    return _engine_for(os.environ.get("DATABASE_URL", "sqlite:///./adrobot.db"))


@lru_cache
def _engine_for(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+psycopg")
        return create_engine(url, pool_pre_ping=True)

    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30},
        **({"poolclass": StaticPool} if url.database in (None, "", ":memory:") else {}),
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine


def get_db() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
