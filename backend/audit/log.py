"""Append-only, hash-chained audit log (PRD US-5, FREE-AI Rec 24).

Every agent step writes one event: what it saw, what it proposed, which gate rule fired, who
approved, what the system returned and whether the fix was verified. Each event stores the hash
of the previous one, so editing or deleting history breaks `verify_chain`.
The class exposes no update or delete.
"""

import hashlib
import json
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, col, select

GENESIS = "0" * 64


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_event"

    seq: int | None = Field(default=None, primary_key=True)
    ts: datetime
    thread_id: str = Field(index=True)
    merchant_id: str | None = Field(default=None, index=True)
    node: str
    event: str  # e.g. CONTEXT_FETCHED, ACTION_PROPOSED, GATE_DECIDED, APPROVED, EXECUTED, VERIFIED
    input_hash: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    action: str | None = None
    args: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    gate: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    confidence: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    actor: str = "sahayak"
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    verified: bool | None = None
    prev_hash: str
    hash: str


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def hash_text(text: str) -> str:
    """Store a hash of masked input, never the text itself."""
    return hashlib.sha256(text.encode()).hexdigest()


_CONTENT = (
    "ts",
    "thread_id",
    "merchant_id",
    "node",
    "event",
    "input_hash",
    "evidence_ids",
    "action",
    "args",
    "gate",
    "confidence",
    "actor",
    "result",
    "verified",
    "prev_hash",
)


def _content(e: AuditEvent) -> dict[str, Any]:
    d = {k: getattr(e, k) for k in _CONTENT}
    d["ts"] = e.ts.astimezone(UTC).isoformat() if e.ts.tzinfo else e.ts.isoformat()
    return d


class AuditLog:
    def __init__(self, engine: Engine):
        self.engine = engine
        # Serialises appends in this process so two events never chain off the same parent.
        self._lock = threading.Lock()
        SQLModel.metadata.create_all(engine, tables=[AuditEvent.__table__])

    def append(
        self,
        *,
        thread_id: str,
        node: str,
        event: str,
        merchant_id: str | None = None,
        input_hash: str | None = None,
        evidence_ids: list[str] | None = None,
        action: str | None = None,
        args: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
        confidence: dict[str, Any] | None = None,
        actor: str = "sahayak",
        result: dict[str, Any] | None = None,
        verified: bool | None = None,
    ) -> AuditEvent:
        with self._lock, Session(self.engine) as session:
            last = session.exec(select(AuditEvent).order_by(col(AuditEvent.seq).desc())).first()
            e = AuditEvent(
                ts=datetime.now(UTC),
                thread_id=thread_id,
                merchant_id=merchant_id,
                node=node,
                event=event,
                input_hash=input_hash,
                evidence_ids=evidence_ids or [],
                action=action,
                args=args or {},
                gate=gate,
                confidence=confidence,
                actor=actor,
                result=result,
                verified=verified,
                prev_hash=last.hash if last else GENESIS,
                hash="",
            )
            e.hash = _digest(_content(e))
            session.add(e)
            session.commit()
            session.refresh(e)
            return e

    def events(
        self,
        thread_id: str | None = None,
        merchant_id: str | None = None,
        limit: int = 500,
        after_seq: int = 0,
    ) -> list[AuditEvent]:
        with Session(self.engine) as session:
            q = select(AuditEvent).where(AuditEvent.seq > after_seq).order_by(col(AuditEvent.seq))
            if thread_id:
                q = q.where(AuditEvent.thread_id == thread_id)
            if merchant_id:
                q = q.where(AuditEvent.merchant_id == merchant_id)
            return list(session.exec(q.limit(limit)).all())

    def verify_chain(self) -> tuple[bool, int | None]:
        """(True, None) if intact, else (False, seq of the first bad event)."""
        prev = GENESIS
        with Session(self.engine) as session:
            for e in session.exec(select(AuditEvent).order_by(col(AuditEvent.seq))):
                if e.prev_hash != prev or e.hash != _digest(_content(e)):
                    return False, e.seq
                prev = e.hash
        return True, None
