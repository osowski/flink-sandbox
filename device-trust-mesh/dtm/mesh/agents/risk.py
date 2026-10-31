"""The Risk agent: scores a device's enrolment risk from context-mcp and hands the result to Enrolment."""
import time
from collections.abc import Callable

from dtm.common.a2a import SELF_TASK_TOPIC
from dtm.common.bus import AgentBus, Inbound
from dtm.common.config import AgentSettings
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import COMPLETED, WORKING, Outputs, Request, parse_request, run_task
from dtm.mesh.agents.reasoning import AgentLlm, system_and_user
from dtm.mesh.llm import OK

RISK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["score", "rationale"],
               "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 100},
                              "rationale": {"type": "string", "minLength": 1}}}

_DESCRIPTION = "Scores device enrolment risk from context-mcp and hands the decision to enrolment."
_SKILLS = [{"id": "assess-enrolment", "name": "Assess enrolment risk",
            "description": "Scores the risk of enrolling a device from its context, its customer and its risk signals.",
            "tags": ["risk", "enrolment"]}]


class RiskAgent:
    name = "risk"
    topics = None
    request_topic = SELF_TASK_TOPIC

    def __init__(self, bus: AgentBus, context: SyncContext, llm: AgentLlm,
                 clock: Callable[[], float] = time.time):
        self._bus = bus
        self._context = context
        self._llm = llm
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
        outputs.status(task_id, WORKING)
        run_task(outputs, task_id, lambda: self._assess(outputs, task_id, request, settings))

    def _assess(self, outputs: Outputs, task_id: str, request: Request, settings: AgentSettings) -> None:
        device_id, customer_id = request.data["deviceId"], request.data["customerId"]
        device = self._context.get_device_context(device_id, min_seq=request.data["requiredContextSeq"])
        as_of = device.get("as_of") or {}
        if device["status"] != "OK" or "contextSeq" not in as_of:
            raise RuntimeError(f"device context not materialized: {device['status']}")
        customer = self._context.get_customer_context(customer_id)
        signals = self._context.list_risk_signals(device_id)

        facts = {"fingerprintSummary": request.data["fingerprintSummary"], "device": device["device"],
                 "customer": customer["customer"],
                 "signals": [{"signalId": s["signal_id"], "signalType": s["signal_type"],
                              "severity": s["severity"], "detail": s["detail"]} for s in signals]}
        req, resp = self._llm.complete(settings, system_and_user(settings.prompt_text, facts), RISK_SCHEMA,
                                       request.message_id)
        interaction_id = outputs.llm_interaction(req, resp)
        if resp.status != OK:
            raise RuntimeError(f"risk model failed: {resp.status} {resp.error}")

        score, rationale = resp.content["score"], resp.content["rationale"]
        signal_ids = [s["signalId"] for s in facts["signals"]]
        outputs.artifact(task_id, "risk-assessment",
                         {"score": score, "rationale": rationale, "signalIds": signal_ids,
                          "asOf": {key: as_of[key] for key in ("contextSeq", "materializedAt", "sourceOffsets")}},
                         f"Risk {score}/100: {rationale}")
        outputs.status(task_id, COMPLETED)
        outputs.send("enrolment",
                     {"taskType": "DECIDE_ENROLMENT", "deviceId": device_id, "customerId": customer_id,
                      "riskAssessment": {"score": score, "rationale": rationale, "signalIds": signal_ids,
                                         "llmInteractionId": interaction_id}},
                     f"Decide enrolment for {device_id} (risk {score})", reference_task_ids=[task_id])
