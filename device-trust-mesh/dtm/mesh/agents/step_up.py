"""The Step-up agent: picks a challenge method from context and orders the challenge, with no model involved."""
import time
from collections.abc import Callable

from dtm.common.a2a import SELF_TASK_TOPIC
from dtm.common.bus import AgentBus, Inbound
from dtm.common.config import AgentSettings
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import COMPLETED, WORKING, Outputs, Request, parse_request, run_task
from dtm.mesh.agents.reasoning import AgentLlm

MFA_PUSH, OTP, BIOMETRIC = "MFA_PUSH", "OTP", "BIOMETRIC"
_WEAK_OS_SIGNALS = frozenset({"OUTDATED_OS", "COMPROMISED_OS"})

_DESCRIPTION = "Chooses a step-up challenge method for a challenged enrolment and orders the challenge."
_SKILLS = [{"id": "challenge-device", "name": "Challenge device",
            "description": "Picks MFA push, OTP or biometric from the device, its customer and the cited signals.",
            "tags": ["step-up", "enrolment"]}]


def choose_method(device: dict, customer: dict | None, signals: list[dict], cited: list[str]) -> str:
    """Pick the challenge method: MFA push through another trusted device, else OTP for a weak OS, else biometric.

    Only the signals whose ``signal_id`` is in ``cited`` count.
    """
    others = (customer or {}).get("devices", [])
    if any(d["trust_status"] == "TRUSTED" and d["device_id"] != device["device_id"] for d in others):
        return MFA_PUSH
    if device.get("rooted") is True or any(s["signal_id"] in cited and s["signal_type"] in _WEAK_OS_SIGNALS
                                           for s in signals):
        return OTP
    return BIOMETRIC


class StepUpAgent:
    name = "step-up"
    topics = None
    request_topic = SELF_TASK_TOPIC

    def __init__(self, bus: AgentBus, context: SyncContext, llm: AgentLlm | None = None,
                 clock: Callable[[], float] = time.time):
        self._bus = bus
        self._context = context
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
        run_task(outputs, task_id, lambda: self._challenge(outputs, task_id, request))

    def _challenge(self, outputs: Outputs, task_id: str, request: Request) -> None:
        envelope = request.envelope
        device_id = envelope.device_id
        context = self._context.get_device_context(device_id, min_seq=envelope.required_context_seq)
        if context["status"] != "OK":
            raise RuntimeError(f"device context not materialized: {context['status']}")
        customer = None
        if envelope.customer_id is not None:
            customer_context = self._context.get_customer_context(envelope.customer_id)
            customer = customer_context["customer"] if customer_context["status"] == "OK" else None
        signals = self._context.list_risk_signals(device_id)

        enrolment_task_id = request.data["enrolmentTaskId"]
        method = choose_method(context["device"], customer, signals, request.data.get("signalIds", []))
        outputs.policy_command("CHALLENGE", request.data["reason"], challenge_method=method,
                               enrolment_task_id=enrolment_task_id)
        outputs.artifact(task_id, "step-up-challenge", {"method": method, "enrolmentTaskId": enrolment_task_id},
                         f"Challenge {device_id} with {method}")
        outputs.status(task_id, COMPLETED)
