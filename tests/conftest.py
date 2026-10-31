import os

# Tests run against an in-memory database; runtime uses Postgres (docker compose).
os.environ["MOCK_PAYTM_DATABASE_URL"] = "sqlite://"
os.environ["SEED_MERCHANTS"] = "15"
os.environ["SEED_DAYS"] = "30"
os.environ["SETTLEMENT_PROCESSING_SECONDS"] = "10"
os.environ["REFUND_PROCESSING_SECONDS"] = "10"

from datetime import UTC, datetime, time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from mock_paytm import clock  # noqa: E402
from mock_paytm.config import get_settings  # noqa: E402
from mock_paytm.db import init_db, make_engine, set_engine  # noqa: E402
from mock_paytm.seed import seed  # noqa: E402

HEALTHY = "M1013"


def pin_clock_to_midday() -> None:
    """Run every test at 12:00 UTC so 'is yesterday's settlement due yet' never depends on
    when the suite happens to run."""
    clock.reset()
    now = clock.now()
    clock.advance((datetime.combine(now.date(), time(12), UTC) - now).total_seconds())


@pytest.fixture
def engine(tmp_path):
    get_settings.cache_clear()
    pin_clock_to_midday()
    # A file per test: the API serves concurrent requests (parallel context fetch) from a
    # thread pool, which a single shared in-memory connection cannot handle.
    eng = make_engine(f"sqlite:///{tmp_path / 'mock_paytm.db'}")
    set_engine(eng)
    init_db(eng)
    with Session(eng) as session:
        seed(session)
    yield eng
    eng.dispose()
    clock.reset()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    from mock_paytm.app import app

    with TestClient(app) as c:
        yield c
