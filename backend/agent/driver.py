"""Run a conversation to completion by answering the graph's pauses.

The graph pauses (interrupt) whenever only someone else can move it forward: the merchant taps a
link or power-cycles a device, an approver clicks, the merchant answers an offer. A *responder*
supplies those answers. `SimResponder` plays the merchant and approver against the mock Paytm
systems (used by tests, the eval harness and `--auto` in the CLI); the CLI also has an interactive one.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

Responder = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Behaviour:
    """What the simulated merchant and approver do."""

    complete_kyc_link: bool = True
    complete_bank_update: bool = True
    power_cycle_when_asked: bool = True
    offer_reply: str | None = None
    approver_approves: bool = True


class SimResponder:
    def __init__(self, http, behaviour: Behaviour | None = None):
        self.http = http  # httpx.AsyncClient pointed at the mock Paytm service
        self.b = behaviour or Behaviour()
        self.log: list[dict[str, Any]] = []

    async def __call__(self, pause: dict[str, Any]) -> dict[str, Any]:
        kind = pause["type"]
        if kind == "approval":
            answer = {"approved": self.b.approver_approves, "approver": "sim-approver"}
        elif kind == "offer_reply":
            answer = {"reply": self.b.offer_reply}
        else:
            what, ref = pause["waiting_for"], pause["ref"]
            if what == "kyc_link":
                done = self.b.complete_kyc_link
                if done:
                    await self.http.post(f"/sim/kyc/{ref}/complete")
            elif what == "bank_update":
                done = self.b.complete_bank_update
                if done:
                    await self.http.post(f"/sim/bank-update/{ref}/complete")
            else:  # power_cycle
                done = self.b.power_cycle_when_asked
                if done:
                    await self.http.post(f"/sim/devices/{pause['device_id']}/power-cycle")
            answer = {"done": done}
        self.log.append({"pause": pause, "answer": answer})
        return answer


async def run_case(
    graph, initial: dict[str, Any], responder: Responder, max_pauses: int = 25
) -> dict[str, Any]:
    """Invoke the graph, answering each pause, until it finishes. Returns the final state."""
    config = {"configurable": {"thread_id": initial["thread_id"]}}
    result = await graph.ainvoke(initial, config)
    for _ in range(max_pauses):
        pauses = result.get("__interrupt__")
        if not pauses:
            return result
        answer = await responder(pauses[0].value)
        result = await graph.ainvoke(Command(resume=answer), config)
    raise RuntimeError(f"case did not finish after {max_pauses} pauses")
