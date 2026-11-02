"""The Compliance agent: audits every decision artifact on ``agent.task-events``, with no model involved."""
import time
from collections import OrderedDict
from collections.abc import Callable

from dtm.common.bus import AgentBus, Inbound
from dtm.common.config import AgentSettings
from dtm.common.envelope import Envelope
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import COMPLETED, Outputs, TaskEvent, parse_task_event, run_task
from dtm.mesh.agents.reasoning import AgentLlm

AUDIT_LOG_TOPIC = "audit.log"
AUDIT_RECORD = "audit-record"

_AGENT_PREFIX = "agent:"
_DESCRIPTION = "Records an audit entry for every agent decision artifact."
_SKILLS = [{"id": "audit-decisions", "name": "Audit decisions",
            "description": "Writes one audit.log record and one audit-record artifact per decision artifact.",
            "tags": ["audit", "compliance"]}]


class ComplianceAgent:
    name = "compliance"
    topics = ["agent.task-events"]
    request_topic = None

    def __init__(self, bus: AgentBus, context: SyncContext | None = None, llm: AgentLlm | None = None,
                 clock: Callable[[], float] = time.time, seen_limit: int = 10_000):
        self._bus = bus
        self._clock = clock
        self._seen_limit = seen_limit
        self._seen: OrderedDict[str, None] = OrderedDict()

    def describe(self, settings: AgentSettings) -> tuple[str, list[dict]]:
        return _DESCRIPTION, _SKILLS

    def __call__(self, inbound: Inbound, settings: AgentSettings) -> None:
        event = parse_task_event(self._bus, inbound)
        if event is None or not self._auditable(event):
            return
        outputs = Outputs(self._bus, self.name, settings, event, clock=self._clock, own_rpc_ids=True)
        task_id = outputs.new_task_id()
        run_task(outputs, task_id, lambda: self._audit(outputs, task_id, event))

    def _auditable(self, event: TaskEvent) -> bool:
        """Accept an artifact event other than an audit record or the agent's own, once per ``messageId``."""
        if event.kind != "artifactUpdate" or event.artifact_name == AUDIT_RECORD:
            return False
        if event.envelope.agent_id == f"{_AGENT_PREFIX}{self.name}" or event.message_id in self._seen:
            return False
        self._seen[event.message_id] = None
        if len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
        return True

    @staticmethod
    def _audit(outputs: Outputs, task_id: str, event: TaskEvent) -> None:
        data = event.artifact_data or {}
        agent_id = event.envelope.agent_id

        def record(envelope: Envelope) -> dict:
            return {"audit_id": envelope.message_id, "subject_task_id": event.task_id,
                    "subject_agent": agent_id.removeprefix(_AGENT_PREFIX), "artifact_name": event.artifact_name,
                    "outcome": data.get("outcome", data.get("method")), "policy_action": data.get("policyAction"),
                    "rationale": data.get("rationale"), "recorded_at": envelope.event_time}

        audit = outputs.avro(AUDIT_LOG_TOPIC, record)
        outputs.artifact(task_id, AUDIT_RECORD, {"auditId": audit.message_id, "subjectTaskId": event.task_id},
                         f"Audited {event.artifact_name} for {event.envelope.device_id}")
        outputs.status(task_id, COMPLETED)
