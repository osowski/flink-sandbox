"""The Revocation agent: a scaffold that publishes a card and rejects every task, as no revocation routing exists."""
import time
from collections.abc import Callable

from dtm.common.a2a import SELF_TASK_TOPIC
from dtm.common.bus import AgentBus, Inbound
from dtm.common.config import AgentSettings
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import REJECTED, Outputs, parse_request
from dtm.mesh.agents.reasoning import AgentLlm

REJECTION = "Revocation is scaffolded; no revocation routing exists yet."

_DESCRIPTION = ("Decides whether to revoke trust for a compromised, previously trusted device. "
                "Outputs to agent.task-events and device.policy-commands.")
_SKILLS = [{"id": "revoke-trust", "name": "Revoke trust",
            "description": "Decides whether to revoke trust for a compromised, previously trusted device.",
            "tags": ["revocation"]}]


class RevocationAgent:
    name = "revocation"
    topics = None
    request_topic = SELF_TASK_TOPIC

    def __init__(self, bus: AgentBus, context: SyncContext, llm: AgentLlm | None = None,
                 clock: Callable[[], float] = time.time):
        self._bus = bus
        self._clock = clock

    def describe(self, settings: AgentSettings) -> tuple[str, list[dict]]:
        return _DESCRIPTION, _SKILLS

    def __call__(self, inbound: Inbound, settings: AgentSettings) -> None:
        request = parse_request(self._bus, inbound)
        if request is None:
            return
        outputs = Outputs(self._bus, self.name, settings, request, clock=self._clock)
        task_id = outputs.new_task_id()
        outputs.submitted(task_id)
        outputs.status(task_id, REJECTED, REJECTION)
