"""Proactive monitor (design decision D13): find merchants with a proven problem they have not
raised yet, so Sahayak can message them first.

A case is started per merchant and topic, but never when a ticket on that topic is already open
(the merchant, or a person, is already on it) or when a case for it is already running.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.agent.playbook import CATEGORY_OF_TOPIC, TOPIC_OF
from backend.gate.evidence import Cause, detect
from backend.tools.client import PaytmTools
from backend.tools.context import fetch_context
from mock_paytm import models as m

# Causes worth contacting a merchant about unprompted. Risk holds and KYC name mismatches are
# left out: they need a person, and a message from a bot would only worry the merchant.
PROACTIVE_CAUSES = {
    Cause.DEVICE_OFFLINE,
    Cause.KYC_EXPIRING,
    Cause.KYC_HOLD,
    Cause.BANK_ACCOUNT_FAILED,
    Cause.SETTLEMENT_STUCK,
    Cause.RENTAL_DESYNC,
}
OPEN_TICKET_WINDOW = timedelta(days=7)


@dataclass
class Trigger:
    merchant_id: str
    topic: str
    causes: list[str]


async def scan(
    tools: PaytmTools,
    merchant_ids: list[str] | None = None,
    busy: set[tuple[str, str]] | None = None,
    concurrency: int = 8,
) -> list[Trigger]:
    """`busy` holds (merchant_id, topic) pairs that already have a running case."""
    busy = busy or set()
    if merchant_ids is None:
        merchant_ids = [x["id"] for x in await tools.list_merchants()]
    gate = asyncio.Semaphore(concurrency)

    async def one(mid: str) -> list[Trigger]:
        async with gate:
            ctx = await fetch_context(tools, mid)
        now = datetime.now(UTC)
        open_topics = {
            topic
            for topic, category in CATEGORY_OF_TOPIC.items()
            for t in ctx.tickets
            if t.category == category
            and t.status in (m.TicketStatus.OPEN, m.TicketStatus.IN_PROGRESS)
            and now - t.updated_at <= OPEN_TICKET_WINDOW
        }
        by_topic: dict[str, list[str]] = {}
        for f in detect(ctx):
            topic = TOPIC_OF[f.cause]
            if (
                f.cause in PROACTIVE_CAUSES
                and topic not in open_topics
                and (mid, topic) not in busy
            ):
                by_topic.setdefault(topic, []).append(f.cause.value)
        return [Trigger(mid, topic, causes) for topic, causes in by_topic.items()]

    found = await asyncio.gather(*(one(mid) for mid in merchant_ids))
    return [t for group in found for t in group]
