"""Wiring: checkpointer, audit log and the assembled agent."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from backend.agent.graph import Deps, build_graph
from backend.agent.llm import AgentLLM
from backend.audit.log import AuditLog
from backend.config import get_settings
from backend.gate.engine import Gate
from backend.pii.masker import PiiMasker
from backend.tools.client import PaytmTools
from mock_paytm.db import make_engine


@asynccontextmanager
async def open_checkpointer(url: str | None = None) -> AsyncIterator:
    """SQLite file by default; a postgresql:// URL uses Postgres; ':memory:' is for tests."""
    url = url if url is not None else get_settings().checkpoint_url
    if url == ":memory:":
        yield InMemorySaver()
    elif url.startswith("postgres"):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()
            yield saver
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = Path(url.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            yield saver


def open_audit(url: str | None = None) -> AuditLog:
    url = url or get_settings().audit_database_url
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return AuditLog(make_engine(url))


def build_agent(
    tools: PaytmTools,
    llm: AgentLLM,
    audit: AuditLog,
    checkpointer,
    gate: Gate | None = None,
    masker: PiiMasker | None = None,
    **deps_kw,
):
    deps = Deps(
        tools=tools,
        llm=llm,
        gate=gate or Gate(),
        audit=audit,
        masker=masker or PiiMasker(),
        **deps_kw,
    )
    return build_graph(deps, checkpointer), deps
