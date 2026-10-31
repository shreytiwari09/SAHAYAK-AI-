"""The AI reps: merchant assignment, and one rep serving several merchants at once."""

import asyncio
from types import SimpleNamespace

from backend.agent.messages import disclosure
from backend.agents import Agent, AgentRoster
from backend.service import CaseService

ROSTER = AgentRoster([Agent("a", "Asha", ("M1001", "M1002")), Agent("b", "Vikram", ("M1003",))], 2)


def test_listed_merchants_go_to_their_rep_and_others_to_a_fixed_one():
    assert ROSTER.for_merchant("M1001").name == "Asha"
    assert ROSTER.for_merchant("M1003").name == "Vikram"
    unlisted = ROSTER.for_merchant("M1008")
    assert unlisted in ROSTER.agents and ROSTER.for_merchant("M1008") == unlisted


def test_the_shipped_roster_gives_the_mvp_merchant_a_rep():
    roster = AgentRoster.load("agents.yaml")
    assigned = [m for a in roster.agents for m in a.merchants]
    assert len(assigned) == len(set(assigned)) and "M1001" in assigned
    assert roster.for_merchant("M1001").name == "Asha"


def test_a_missing_file_gives_a_single_default_rep():
    roster = AgentRoster.load("does-not-exist.yaml")
    assert [a.id for a in roster.agents] == ["sahayak"]


def test_the_introduction_names_the_rep_when_there_is_one():
    assert "Main Asha, Paytm ka AI assistant" in disclosure("hi", "Asha")
    assert "I'm Asha, Paytm's AI assistant" in disclosure("en", "Asha")
    assert "Main Paytm ka AI assistant" in disclosure("hi")


class TimedGraph:
    """Each case takes a moment; records how many ran together, per rep."""

    def __init__(self):
        self.now, self.peak = {}, {}

    async def ainvoke(self, payload, config):
        rep = payload["agent_name"]
        self.now[rep] = self.now.get(rep, 0) + 1
        self.peak[rep] = max(self.peak.get(rep, 0), self.now[rep])
        await asyncio.sleep(0.05)
        self.now[rep] -= 1

    async def aget_state(self, config):
        return SimpleNamespace(values={}, tasks=[], next=())


async def test_one_rep_serves_its_merchants_together_up_to_its_limit():
    graph = TimedGraph()
    service = CaseService(graph, audit=None, tools=None, roster=ROSTER)
    recs = [service.start(m, "hi") for m in ("M1001", "M1002", "M1001", "M1002", "M1003")]
    await asyncio.gather(*(service.wait(r.thread_id) for r in recs))
    assert graph.peak["Asha"] == 2  # four cases, limit two: concurrent but capped
    assert graph.peak["Vikram"] == 1
    assert [r.agent_name for r in recs] == ["Asha", "Asha", "Asha", "Asha", "Vikram"]


async def test_reps_do_not_hold_each_other_up():
    graph = TimedGraph()
    service = CaseService(graph, audit=None, tools=None, roster=ROSTER)
    started = asyncio.get_running_loop().time()
    recs = [service.start(m, "hi") for m in ("M1001", "M1002", "M1003")]
    await asyncio.gather(*(service.wait(r.thread_id) for r in recs))
    assert asyncio.get_running_loop().time() - started < 0.09  # all three overlapped
    view = {a["id"]: a for a in service.agents_view()}
    assert view["a"]["finished"] == 2 and view["b"]["finished"] == 1
