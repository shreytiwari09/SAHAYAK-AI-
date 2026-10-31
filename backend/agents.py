"""The AI sales reps: who looks after which merchants, and how many cases each runs at once.

An agent is a name plus a list of merchants. It is not a separate process: every case is its own
saved thread, and an agent's merchants are served concurrently through one shared runtime,
limited per agent so a busy rep cannot starve the others.
"""

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    merchants: tuple[str, ...] = ()


@dataclass
class AgentRoster:
    agents: list[Agent]
    concurrency: int = 3
    _slots: dict[str, asyncio.Semaphore] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "AgentRoster":
        p = Path(path)
        if not p.is_file():
            return cls.default()
        raw = yaml.safe_load(p.read_text()) or {}
        agents = [
            Agent(a["id"], a["name"], tuple(a.get("merchants", []))) for a in raw.get("agents", [])
        ]
        return cls(agents or cls.default().agents, int(raw.get("concurrency", 3)))

    @classmethod
    def default(cls) -> "AgentRoster":
        return cls([Agent("sahayak", "Sahayak")])

    def for_merchant(self, merchant_id: str) -> Agent:
        for a in self.agents:
            if merchant_id in a.merchants:
                return a
        digits = re.sub(r"\D", "", merchant_id)
        return self.agents[int(digits or 0) % len(self.agents)]

    def get(self, agent_id: str) -> Agent | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def slot(self, agent_id: str) -> asyncio.Semaphore:
        """The agent's concurrency limit. Created on first use, inside the running loop."""
        if agent_id not in self._slots:
            self._slots[agent_id] = asyncio.Semaphore(self.concurrency)
        return self._slots[agent_id]
