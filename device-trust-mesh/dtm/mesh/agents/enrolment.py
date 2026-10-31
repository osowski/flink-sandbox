"""The Enrolment agent: decides a device's enrolment, with guardrails around the model's answer.

``DECIDE_ENROLMENT`` reads the device and its risk signals from context-mcp, applies the guardrails that
need no model, asks the model, and applies the guardrails that bound its answer. A decision that needs a
step-up ends the task ``AUTH_REQUIRED`` after handing off to the Step-up agent; ``RESOLVE_CHALLENGE`` resumes
that task with the challenge result. ``REMOVE_TRUST`` blocks an unenrolled device without context or a model.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass

from dtm.common.a2a import SELF_TASK_TOPIC
from dtm.common.bus import AgentBus, Inbound
from dtm.common.config import AgentSettings
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import (AUTH_REQUIRED, COMPLETED, REJECTED, WORKING, Outputs, Request, parse_request,
                                 run_task)
from dtm.mesh.agents.reasoning import AgentLlm, system_and_user
from dtm.mesh.llm import OK, LLMResponse

GRANT, DENY, CHALLENGE, GRANT_READ_ONLY = "GRANT", "DENY", "CHALLENGE", "GRANT_READ_ONLY"
DEFAULT_CHALLENGE_AT_RISK_SCORE = 60

DECISION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["decision", "rationale"],
                   "properties": {"decision": {"enum": [GRANT, DENY, CHALLENGE, GRANT_READ_ONLY]},
                                  "rationale": {"type": "string", "minLength": 1}}}

# decision -> (artifact outcome, policy action)
_OUTCOMES = {GRANT: ("GRANTED", "ALLOW"), GRANT_READ_ONLY: ("READ_ONLY", "READ_ONLY"),
             DENY: ("DENIED", "BLOCK"), CHALLENGE: ("CHALLENGED", "CHALLENGE")}
# challenge result -> (decision, policy-command reason, rationale)
_RESOLUTIONS = {"PASSED": (GRANT, "CHALLENGE_PASSED", "Step-up challenge passed"),
                "FAILED": (DENY, "CHALLENGE_FAILED", "Step-up challenge failed")}

_DESCRIPTION = ("Decides device enrolment from risk, context and signals behind guardrails, "
                "and resumes a challenged enrolment when its step-up resolves.")
_SKILLS = [
    {"id": "decide-enrolment", "name": "Decide enrolment",
     "description": "Grants, denies, restricts or challenges an enrolment from the risk assessment and device context.",
     "tags": ["enrolment", "guardrails"]},
    {"id": "remove-trust", "name": "Remove trust",
     "description": "Blocks a device that was unenrolled.", "tags": ["enrolment"]},
    {"id": "resolve-challenge", "name": "Resolve challenge",
     "description": "Completes a challenged enrolment with the step-up result.", "tags": ["enrolment", "step-up"]},
]


@dataclass(frozen=True)
class _Decision:
    decision: str
    rationale: str
    guardrails: list[str]
    final: str


class EnrolmentAgent:
    name = "enrolment"
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
        resuming = request.data["taskType"] == "RESOLVE_CHALLENGE" and request.task_id is not None
        task_id = request.task_id if resuming else outputs.new_task_id()
        if not resuming:
            outputs.submitted(task_id)
        outputs.status(task_id, WORKING)
        run_task(outputs, task_id, lambda: self._handle(outputs, task_id, request, settings, resuming))

    def _handle(self, outputs: Outputs, task_id: str, request: Request, settings: AgentSettings,
                resuming: bool) -> None:
        task_type = request.data["taskType"]
        if task_type == "DECIDE_ENROLMENT":
            self._decide(outputs, task_id, request, settings)
        elif task_type == "REMOVE_TRUST":
            self._remove_trust(outputs, task_id)
        elif resuming:
            self._resolve(outputs, task_id, request)
        else:
            raise ValueError("RESOLVE_CHALLENGE without taskId")

    def _decide(self, outputs: Outputs, task_id: str, request: Request, settings: AgentSettings) -> None:
        device_id = request.data["deviceId"]
        context = self._context.get_device_context(device_id, min_seq=request.envelope.required_context_seq)
        as_of = context.get("as_of") or {}
        if context["status"] != "OK":
            raise RuntimeError(f"device context not materialized: {context['status']}")
        device = context["device"]
        signals = [{"signalId": s["signal_id"], "signalType": s["signal_type"], "severity": s["severity"],
                    "detail": s["detail"]} for s in self._context.list_risk_signals(device_id)]
        risk = request.data.get("riskAssessment")

        decision = self._pre_llm(device, signals)
        if decision is None:
            decision = self._ask_model(outputs, request, settings, device, risk, signals)

        outcome, action = _OUTCOMES[decision.decision]
        if decision.decision != CHALLENGE:
            outputs.policy_command(action, outcome)
        artifact = {"outcome": outcome, "policyAction": action, "rationale": decision.rationale,
                    "guardrails": decision.guardrails}
        if risk is not None:
            artifact["riskScore"] = risk["score"]
        if all(key in as_of for key in ("contextSeq", "materializedAt", "sourceOffsets")):
            artifact["asOf"] = {key: as_of[key] for key in ("contextSeq", "materializedAt", "sourceOffsets")}
        outputs.artifact(task_id, "enrolment-decision", artifact, f"{outcome}: {decision.rationale}")
        if decision.decision == CHALLENGE:
            outputs.send("step-up",
                         {"enrolmentTaskId": task_id, "reason": decision.rationale,
                          "signalIds": list((risk or {}).get("signalIds", []))},
                         f"Step-up challenge for {device_id}", reference_task_ids=[task_id])
        outputs.status(task_id, decision.final, "Step-up challenge required" if decision.final == AUTH_REQUIRED
                       else None)

    @staticmethod
    def _pre_llm(device: dict, signals: list[dict]) -> _Decision | None:
        """Decide without the model for a device that is compromised, or one that is already trusted."""
        if device.get("rooted") is True or any(s["signalType"] == "COMPROMISED_OS" for s in signals):
            return _Decision(DENY, "Device is rooted or reports a compromised OS.", ["COMPROMISED_OS"], REJECTED)
        if device.get("trust_status") == "TRUSTED":
            return _Decision(GRANT, "Device is already trusted.", ["ALREADY_TRUSTED"], COMPLETED)
        return None

    def _ask_model(self, outputs: Outputs, request: Request, settings: AgentSettings, device: dict,
                   risk: dict | None, signals: list[dict]) -> _Decision:
        facts = {"device": device, "riskAssessment": risk, "signals": signals}
        req, resp = self._llm.complete(settings, system_and_user(settings.prompt_text, facts), DECISION_SCHEMA,
                                       request.message_id)
        decision = self._bound(resp, risk, settings.guardrails.get("challengeAtRiskScore",
                                                                   DEFAULT_CHALLENGE_AT_RISK_SCORE))
        outputs.llm_interaction(req, resp, ",".join(decision.guardrails) or "PASSED")
        return decision

    @staticmethod
    def _bound(resp: LLMResponse, risk: dict | None, threshold: int) -> _Decision:
        """Apply the post-model guardrails in order; the first that fires turns the decision into a challenge."""
        if resp.status != OK:
            return _Decision(CHALLENGE, f"Model output unusable ({resp.status}); step-up required.",
                             ["LLM_OUTPUT_INVALID"], AUTH_REQUIRED)
        decision, rationale = resp.content["decision"], resp.content["rationale"]
        fired = None
        if decision in (GRANT, GRANT_READ_ONLY) and risk is None:
            fired = "NO_RISK_ASSESSMENT"
        elif decision == GRANT and risk["score"] >= threshold:
            fired = "RISK_THRESHOLD"
        elif decision == GRANT and risk.get("signalIds"):
            fired = "UNRESOLVED_SIGNALS"
        if fired is not None:
            return _Decision(CHALLENGE, f"Guardrail {fired} turned {decision} into CHALLENGE: {rationale}",
                             [fired], AUTH_REQUIRED)
        return _Decision(decision, rationale, [], AUTH_REQUIRED if decision == CHALLENGE else COMPLETED)

    @staticmethod
    def _remove_trust(outputs: Outputs, task_id: str) -> None:
        rationale = "Device unenrolled by request"
        outputs.policy_command("BLOCK", "UNENROLLED")
        outputs.artifact(task_id, "enrolment-decision",
                         {"outcome": "REMOVED", "policyAction": "BLOCK", "rationale": rationale, "guardrails": []},
                         f"REMOVED: {rationale}")
        outputs.status(task_id, COMPLETED)

    @staticmethod
    def _resolve(outputs: Outputs, task_id: str, request: Request) -> None:
        result = request.data.get("challengeResult")
        if result not in _RESOLUTIONS:
            raise ValueError(f"RESOLVE_CHALLENGE with unknown challengeResult {result!r}")
        decision, reason, rationale = _RESOLUTIONS[result]
        outcome, action = _OUTCOMES[decision]
        outputs.policy_command(action, reason)
        outputs.artifact(task_id, "enrolment-decision",
                         {"outcome": outcome, "policyAction": action, "rationale": rationale, "guardrails": []},
                         f"{outcome}: {rationale}")
        outputs.status(task_id, COMPLETED)
