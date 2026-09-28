"""Runtime peer discovery from the Foundry project's agent list and each agent's A2A card.

No peer names are configured. The directory lists every agent in the project, reads the
A2A card each one publishes, and hands the cards to the supervisor's model, which picks
peers by their skills. Agents without a card have not enabled A2A and are not discoverable.
The supervisor excludes itself using the platform-provided ``FOUNDRY_AGENT_NAME``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from a2a_client import A2AError, FoundryA2AClient

logger = logging.getLogger(__name__)

# Foundry cards always advertise text input modes and azure.yaml cannot declare MIME types,
# so a peer declares which attachments it accepts through its card's skill tags.
FILE_TAG = "file"
WORKBOOK_TAG = "excel"


@dataclass(frozen=True)
class Peer:
    """One discovered agent and the A2A card it publishes."""

    name: str
    kind: str | None
    card_url: str
    card: dict[str, Any]

    @property
    def skills(self) -> list[dict[str, Any]]:
        return [skill for skill in self.card.get("skills") or [] if isinstance(skill, dict)]

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(
            str(tag).strip().lower() for skill in self.skills for tag in skill.get("tags") or []
        )

    @property
    def accepts_files(self) -> bool:
        return FILE_TAG in self.tags

    @property
    def accepts_workbooks(self) -> bool:
        return WORKBOOK_TAG in self.tags

    def summary(self) -> dict[str, Any]:
        """The card content the model routes on, plus what the card says it accepts.

        The card URL is left out to keep the per-turn catalog small; it is recorded in the
        discovery evidence and on every hop instead.
        """
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.card.get("description"),
            "skills": [
                {key: skill[key] for key in ("id", "name", "description", "tags", "examples") if skill.get(key)}
                for skill in self.skills
            ],
            "accepts_attachments": {"files": self.accepts_files, "excel_workbooks": self.accepts_workbooks},
        }


class AgentDirectory:
    """Discovers peers and refreshes them so that card changes change routing."""

    def __init__(self, a2a: FoundryA2AClient, *, self_name: str | None, ttl: float = 300.0) -> None:
        self._a2a = a2a
        self._self_name = self_name
        self._ttl = ttl
        self._peers: dict[str, Peer] = {}
        self._loaded_at = float("-inf")
        self._lock = asyncio.Lock()

    async def peers(self, *, refresh: bool = False) -> dict[str, Peer]:
        async with self._lock:
            if refresh or time.monotonic() - self._loaded_at > self._ttl:
                self._peers = await self._discover()
                self._loaded_at = time.monotonic()
            return dict(self._peers)

    async def get(self, name: str) -> Peer | None:
        peers = await self.peers()
        if name not in peers:
            # The agent may have published its card after the last discovery.
            peers = await self.peers(refresh=True)
        return peers.get(name)

    async def _discover(self) -> dict[str, Peer]:
        listing = await self._a2a.list_project_agents()
        names = [str(agent["name"]) for agent in listing
                 if agent.get("name") and agent["name"] != self._self_name]
        kinds = {str(agent.get("name")): _kind(agent) for agent in listing}
        cards = await asyncio.gather(*(self._card(name) for name in names))
        peers = {
            name: Peer(name, kinds.get(name), self._a2a.card_url(name), card)
            for name, card in zip(names, cards, strict=True) if card is not None
        }
        logger.info("Discovered %d peer(s) from A2A agent cards: %s", len(peers), sorted(peers))
        return peers

    async def _card(self, name: str) -> dict[str, Any] | None:
        try:
            return await self._a2a.fetch_agent_card(name, refresh=True)
        except (A2AError, httpx.HTTPError) as exc:
            # A 404 means the agent has not enabled A2A; any other failure hides it until
            # the next discovery, so it is logged rather than silently dropped.
            logger.warning("Agent %s is not discoverable: %s", name, exc)
            return None


def _kind(agent: dict[str, Any]) -> str | None:
    latest = (agent.get("versions") or {}).get("latest") or {}
    kind = (latest.get("definition") or {}).get("kind")
    return str(kind) if kind else None
