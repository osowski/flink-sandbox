import dataclasses
import json
import unittest

from prometheus_client import REGISTRY

from agent_fakes import ScriptedProvider, settings
from dtm.mesh.agents.reasoning import AgentLlm, system_and_user
from dtm.mesh.llm import ERROR, OK, LLMResponse

LLM = {"provider": "ollama", "endpoint": "http://h:11434", "model": "qwen3:4b",
       "options": {"temperature": 0, "seed": 42}, "timeoutMs": 12000}
SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "{}"}]


def llm_settings(version="cfg-1", **over):
    fields = {"llm": LLM, "prompt_ref": "risk.v1", "prompt_text": "You assess risk.",
              "config_version": version, **over}
    return settings("risk", **fields)


def observed(model, status):
    return REGISTRY.get_sample_value("dtm_llm_latency_ms_count",
                                     {"agent": "risk", "model": model, "status": status}) or 0.0


class AgentLlmTest(unittest.TestCase):
    def setUp(self):
        self.provider = ScriptedProvider(*[LLMResponse(OK, {"score": 1}, {}, 5) for _ in range(3)])
        self.built: list[dict] = []

        def factory(llm):
            self.built.append(llm)
            return self.provider

        self.llm = AgentLlm("risk", factory)

    def test_request_built_from_settings_and_cached_per_version(self):
        req, resp = self.llm.complete(llm_settings(), MESSAGES, SCHEMA, "msg-1")
        self.assertEqual((req.provider, req.model, req.options, req.timeout_ms),
                         ("ollama", "qwen3:4b", {"temperature": 0, "seed": 42}, 12000))
        self.assertEqual((req.prompt_ref, req.input_message_id, req.messages, req.schema),
                         ("risk.v1", "msg-1", MESSAGES, SCHEMA))
        self.assertEqual(resp.content, {"score": 1})
        self.assertIs(self.provider.requests[0], req)

        self.llm.complete(llm_settings(), MESSAGES, SCHEMA, "msg-2")
        self.assertEqual(len(self.built), 1)
        self.llm.complete(llm_settings("cfg-2"), MESSAGES, SCHEMA, "msg-3")
        self.assertEqual(len(self.built), 2)
        self.assertEqual(self.provider.calls, 3)

    def test_factory_receives_the_llm_block(self):
        self.llm.complete(llm_settings(), MESSAGES, SCHEMA, "msg-1")
        self.assertEqual(self.built, [LLM])

    def test_options_and_timeout_default(self):
        bare = {"provider": "ollama", "endpoint": "http://h", "model": "m"}
        req, _ = self.llm.complete(dataclasses.replace(llm_settings(), llm=bare), MESSAGES, SCHEMA, "msg-1")
        self.assertEqual((req.options, req.timeout_ms), ({}, 30000))

    def test_missing_prompt_raises_before_any_call(self):
        with self.assertRaisesRegex(ValueError, "prompt"):
            self.llm.complete(llm_settings(prompt_text=None), MESSAGES, SCHEMA, "msg-1")
        self.assertEqual((len(self.built), self.provider.calls), (0, 0))

    def test_completion_is_observed_under_the_agent(self):
        self.provider = ScriptedProvider(LLMResponse(ERROR, None, None, 3, error="boom"))
        before = observed("qwen3:4b", "ERROR")
        _, resp = self.llm.complete(llm_settings(), MESSAGES, SCHEMA, "msg-1")
        self.assertEqual(resp.status, ERROR)
        self.assertEqual(observed("qwen3:4b", "ERROR"), before + 1)


class SystemAndUserTest(unittest.TestCase):
    def test_system_and_user_is_stable(self):
        a = system_and_user("prompt", {"b": 1, "a": {"y": [1, 2], "x": "é"}})
        b = system_and_user("prompt", {"a": {"x": "é", "y": [1, 2]}, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a[0], {"role": "system", "content": "prompt"})
        self.assertEqual(a[1], {"role": "user", "content": '{"a":{"x":"é","y":[1,2]},"b":1}'})
        self.assertEqual(json.loads(a[1]["content"])["b"], 1)


if __name__ == "__main__":
    unittest.main()
