from collections.abc import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from mock_paytm.config import get_settings

_engine: Engine | None = None


def make_engine(url: str) -> Engine:
    if url in ("sqlite://", "sqlite:///:memory:"):
        # In-memory SQLite has one connection; only safe for single-threaded use.
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite"):
        # File SQLite: a connection per thread, so concurrent requests don't share one.
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return create_engine(url, pool_pre_ping=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().mock_paytm_database_url)
    return _engine


def set_engine(engine: Engine) -> None:
    global _engine
    _engine = engine


def init_db(engine: Engine | None = None, drop: bool = False) -> None:
    from mock_paytm import models  # noqa: F401  (register tables)

    engine = engine or get_engine()
    if drop:
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
