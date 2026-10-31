import unittest
from pathlib import Path

from agent_fakes import make_bus, produced, request_inbound, settings
from dtm.common import a2a, ids
from dtm.common.config import FileConfigSource
from dtm.mesh.agents.kit import REJECTED, SUBMITTED
from dtm.mesh.agents.revocation import RevocationAgent
from dtm.mesh.runtime import AgentRuntime

CLOCK = lambda: 1793323940.5  # noqa: E731
TOPIC = "agent.tasks.revocation"
FIXTURE = Path(__file__).parent / "fixtures" / "etc-dtm"
SCAFFOLD_TEXT = "Revocation is scaffolded; no revocation routing exists yet."


class RevocationAgentTest(unittest.TestCase):
    def setUp(self):
        self.bus = make_bus("revocation")
        self.settings = settings("revocation")
        self.agent = RevocationAgent(self.bus, None, None, clock=CLOCK)

    def events(self):
        return [next(iter(record["result"].items())) for _, record in produced(self.bus, "agent.task-events")]

    def test_rejects_with_scaffold_text(self):
        self.agent(request_inbound(TOPIC, {"deviceId": "dev-0001", "reason": "Device reported stolen"}),
                   self.settings)
        events = self.events()
        self.assertEqual([carrier["status"]["state"] for _, carrier in events], [SUBMITTED, REJECTED])
        final = events[-1][1]
        self.assertEqual(final["status"]["message"]["parts"][0]["text"], SCAFFOLD_TEXT)
        self.assertEqual(final["taskId"], ids.task_id("agent:revocation", "router-0"))
        self.assertEqual([c["metadata"]["urn:device-trust-mesh:envelope:v1"]["messageId"] for _, c in events],
                         [ids.message_id("agent:revocation", "router-0", "cfg-1", i) for i in range(2)])
        self.assertEqual({key for key, _ in produced(self.bus, "agent.task-events")}, {"dev-0001"})
        self.assertEqual(produced(self.bus, "device.policy-commands"), [])
        self.assertEqual(produced(self.bus, "agent.llm-interactions"), [])

    def test_invalid_request_is_skipped(self):
        inbound = request_inbound(TOPIC, {"deviceId": "dev-0001"})
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            self.agent(inbound, self.settings)
        self.assertEqual(self.events(), [])

    def test_card_validates(self):
        description, skills = self.agent.describe(self.settings)
        card = a2a.agent_card(self.agent.name, description, skills, "cfg-1", "kafka:9092",
                              self.agent.request_topic)
        a2a.validate("agent.cards", card)
        self.assertEqual([skill["id"] for skill in skills], ["revoke-trust"])
        self.assertIn("agent.task-events", description)
        self.assertIn("device.policy-commands", description)
        self.assertEqual(card["supportedInterfaces"][0]["url"], f"kafka://kafka:9092/{TOPIC}")

    def test_disabled_by_default_config_tombstones_card(self):
        runtime = AgentRuntime(self.agent.name, self.bus, FileConfigSource(str(FIXTURE), {"LLM_ENDPOINT": "http://h"}),
                               self.agent.describe, self.agent, bootstrap="kafka:9092",
                               topics=self.agent.topics, request_topic=self.agent.request_topic)
        runtime.start()
        cards = [r for r in self.bus._producer.records if r.topic == "agent.cards"]
        self.assertEqual([(r.key, r.value) for r in cards], [(b"revocation", None)])
        self.assertTrue(self.bus.paused)

    def test_prompt_is_packaged(self):
        text = (Path(a2a.__file__).parents[1] / "prompts" / "revocation.v1").read_text()
        self.assertIn("revoke", text.lower())
        self.assertIn("compromised", text.lower())

    def test_serve_entry(self):
        from dtm.mesh import revocation
        self.assertEqual(revocation.RevocationAgent.name, "RevocationAgent")
        agent = revocation.make(self.bus, None, None)
        self.assertEqual(agent.name, "revocation")


if __name__ == "__main__":
    unittest.main()
