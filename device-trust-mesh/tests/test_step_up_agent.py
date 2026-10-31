import unittest

from agent_fakes import context_table, make_bus, produced, request_inbound, settings
from dtm.common import ids
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import COMPLETED, FAILED, SUBMITTED, WORKING
from dtm.mesh.agents.step_up import StepUpAgent, choose_method
from dtm.mesh.context.server import build_mcp

CLOCK = lambda: 1793323940.5  # noqa: E731
TOPIC = "agent.tasks.step-up"
ENROLMENT_TASK_ID = "enrolment-task-1"
THIS = {"device_id": "dev-0001", "trust_status": "PENDING"}
OTHER_TRUSTED = {"device_id": "dev-0002", "trust_status": "TRUSTED"}
OTHER_PENDING = {"device_id": "dev-0003", "trust_status": "PENDING"}
CUSTOMERS = [{"customer_id": "cust-001"}]


def signal(signal_id, signal_type, device_id="dev-0001"):
    return {"device_id": device_id, "signal_id": signal_id, "signal_type": signal_type}


def step_up_data(signal_ids=(), reason="Risk 70 needs step-up"):
    return {"enrolmentTaskId": ENROLMENT_TASK_ID, "reason": reason, "signalIds": list(signal_ids)}


class ChooseMethodTest(unittest.TestCase):
    def test_pure_selection(self):
        trusted_peer = {"devices": [{"device_id": "dev-0002", "trust_status": "TRUSTED"}]}
        self.assertEqual(choose_method({"device_id": "dev-0001"}, trusted_peer, [], []), "MFA_PUSH")
        self.assertEqual(choose_method({"device_id": "dev-0001"}, None, [], []), "BIOMETRIC")

    def test_the_device_itself_is_not_another_device(self):
        own = {"devices": [{"device_id": "dev-0001", "trust_status": "TRUSTED"}]}
        self.assertEqual(choose_method({"device_id": "dev-0001"}, own, [], []), "BIOMETRIC")


class StepUpAgentTest(unittest.TestCase):
    def setUp(self):
        self.bus = make_bus("step-up")
        self.settings = settings("step-up")

    def run_request(self, data=None, devices=(THIS,), signals=(), customers=CUSTOMERS):
        table = context_table(devices=devices, customers=customers, signals=signals)
        context = SyncContext(build_mcp(table, wait_timeout_ms=50), timeout_s=5.0)
        self.addCleanup(context.close)
        agent = StepUpAgent(self.bus, context, clock=CLOCK)
        agent(request_inbound(TOPIC, data or step_up_data()), self.settings)

    def events(self):
        return [next(iter(record["result"].items())) for _, record in produced(self.bus, "agent.task-events")]

    def states(self):
        return [carrier["artifact"]["name"] if kind == "artifactUpdate" else carrier["status"]["state"]
                for kind, carrier in self.events()]

    def commands(self):
        return [value for _, value in produced(self.bus, "device.policy-commands")]

    def assert_method(self, method):
        (command,) = self.commands()
        self.assertEqual((command["action"], command["challenge_method"]), ("CHALLENGE", method))

    def test_mfa_push_when_customer_has_other_trusted_device(self):
        self.run_request(devices=[THIS, OTHER_TRUSTED])
        self.assert_method("MFA_PUSH")

    def test_pending_peer_does_not_count_as_trusted(self):
        self.run_request(devices=[THIS, OTHER_PENDING])
        self.assert_method("BIOMETRIC")

    def test_own_trust_does_not_count_as_another_device(self):
        self.run_request(devices=[{**THIS, "trust_status": "TRUSTED"}])
        self.assert_method("BIOMETRIC")

    def test_otp_for_outdated_os_signal(self):
        self.run_request(step_up_data(["sig-0"]), signals=[signal("sig-0", "OUTDATED_OS")])
        self.assert_method("OTP")

    def test_otp_for_compromised_os_signal(self):
        self.run_request(step_up_data(["sig-0"]), signals=[signal("sig-0", "COMPROMISED_OS")])
        self.assert_method("OTP")

    def test_otp_for_rooted_device(self):
        self.run_request(devices=[{**THIS, "rooted": True}])
        self.assert_method("OTP")

    def test_mfa_push_outranks_otp(self):
        self.run_request(step_up_data(["sig-0"]), devices=[{**THIS, "rooted": True}, OTHER_TRUSTED],
                         signals=[signal("sig-0", "OUTDATED_OS")])
        self.assert_method("MFA_PUSH")

    def test_biometric_otherwise(self):
        self.run_request(step_up_data(["sig-0"]), signals=[signal("sig-0", "IMPOSSIBLE_TRAVEL")])
        self.assert_method("BIOMETRIC")

    def test_uncited_signals_ignored(self):
        self.run_request(step_up_data(["sig-1"]),
                         signals=[signal("sig-0", "OUTDATED_OS"), signal("sig-1", "IMPOSSIBLE_TRAVEL")])
        self.assert_method("BIOMETRIC")

    def test_missing_customer_context_falls_through(self):
        self.run_request(customers=[])
        self.assert_method("BIOMETRIC")

    def test_emits_challenge_command_and_artifact(self):
        self.run_request(devices=[THIS, OTHER_TRUSTED])
        self.assertEqual(self.states(), [SUBMITTED, WORKING, "step-up-challenge", COMPLETED])
        (key, command), = produced(self.bus, "device.policy-commands")
        self.assertEqual(key, "dev-0001")
        self.assertEqual((command["action"], command["reason"], command["challenge_method"],
                          command["enrolment_task_id"]),
                         ("CHALLENGE", "Risk 70 needs step-up", "MFA_PUSH", ENROLMENT_TASK_ID))
        artifact = self.events()[2][1]["artifact"]
        self.assertEqual(artifact["parts"][0]["data"], {"method": "MFA_PUSH", "enrolmentTaskId": ENROLMENT_TASK_ID})
        self.assertEqual(artifact["parts"][1]["text"], "Challenge dev-0001 with MFA_PUSH")
        self.assertEqual(self.events()[-1][1]["status"]["state"], COMPLETED)
        self.assertEqual({k for k, _ in produced(self.bus, "agent.task-events")}, {"dev-0001"})

    def test_outputs_are_emitted_in_order_with_the_final_status_last(self):
        self.run_request()
        message_id = lambda i: ids.message_id("agent:step-up", "router-0", "cfg-1", i)  # noqa: E731
        self.assertEqual([c["metadata"]["urn:device-trust-mesh:envelope:v1"]["messageId"] for _, c in self.events()],
                         [message_id(0), message_id(1), message_id(3), message_id(4)])
        self.assertEqual(self.commands()[0]["envelope"]["message_id"], message_id(2))

    def test_no_llm_used(self):
        self.run_request()
        self.assertEqual(produced(self.bus, "agent.llm-interactions"), [])

    def test_unmaterialized_device_fails_the_task(self):
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(devices=[])
        self.assertEqual(self.states(), [SUBMITTED, WORKING, FAILED])
        self.assertIn("device context not materialized",
                      self.events()[-1][1]["status"]["message"]["parts"][0]["text"])
        self.assertEqual(self.commands(), [])

    def test_serve_entry(self):
        from dtm.mesh import step_up
        self.assertEqual(step_up.StepUpAgent.name, "StepUpAgent")
        agent = step_up.make(self.bus, None, None)
        self.assertEqual(agent.name, "step-up")


if __name__ == "__main__":
    unittest.main()
