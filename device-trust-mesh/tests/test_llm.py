import dataclasses
import json
import unittest

import fastavro
import fastavro.validation
import httpx
from prometheus_client import REGISTRY

from dtm.common.envelope import Envelope
from dtm.common.schemas import load_avro_schema
from dtm.mesh.llm import (LLMRequest, LLMResponse, OllamaProvider, OpenAICompatibleProvider,
                          RecordedProvider, llm_interaction_record, observe_llm, provider_for,
                          request_hash)

SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 100},
                   "rationale": {"type": "string"}},
    "required": ["score", "rationale"],
}
OPTIONS = {"temperature": 0, "seed": 42, "think": False, "num_ctx": 4096}
REQ = LLMRequest("ollama", "qwen3:4b", [{"role": "system", "content": "Score the device."},
                                        {"role": "user", "content": "{\"device\": 1}"}],
                 SCHEMA, OPTIONS, "risk-prompt-v1", "m-1")
ENV = Envelope("m-2", "c-1", "m-1", "agent:risk", "dev", "abc123",
               1793323937770, "dev-0001", "cust-001", 5, 5)


def body_with_content(content: str) -> dict:
    return {"model": "qwen3:4b", "created_at": "2026-10-31T00:00:00Z",
            "message": {"role": "assistant", "content": content}, "done": True,
            "prompt_eval_count": 100, "eval_count": 20}


OK_BODY = body_with_content('{"score": 12, "rationale": "clean"}')


def complete_via(handler, req=REQ):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaProvider("http://h:11434", client).complete(req)


def complete_with(body: dict, req=REQ):
    return complete_via(lambda request: httpx.Response(200, json=body), req)


def latency_count(agent, model, status):
    labels = {"agent": agent, "model": model, "status": status}
    return REGISTRY.get_sample_value("dtm_llm_latency_ms_count", labels) or 0.0


class OllamaProviderTest(unittest.TestCase):
    def test_ollama_request_body(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["timeout"] = request.extensions["timeout"]
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=OK_BODY)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        OllamaProvider("http://h:11434", client).complete(REQ)
        self.assertEqual(seen["url"], "http://h:11434/api/chat")
        self.assertEqual(seen["model"], "qwen3:4b")
        self.assertEqual(seen["messages"], REQ.messages)
        self.assertIs(seen["stream"], False)
        self.assertEqual(seen["format"], REQ.schema)
        self.assertIs(seen["think"], False)
        self.assertEqual(seen["options"], {"temperature": 0, "seed": 42, "num_ctx": 4096})
        self.assertEqual(seen["timeout"]["read"], 30.0)

    def test_timeout_ms_sets_request_timeout(self):
        seen = {}

        def handler(request):
            seen["timeout"] = request.extensions["timeout"]
            return httpx.Response(200, json=OK_BODY)

        complete_via(handler, dataclasses.replace(REQ, timeout_ms=1500))
        self.assertEqual(seen["timeout"]["read"], 1.5)
        self.assertEqual(seen["timeout"]["connect"], 1.5)

    def test_options_without_think_omit_think(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=OK_BODY)

        complete_via(handler, dataclasses.replace(REQ, options={"temperature": 0}))
        self.assertNotIn("think", seen)
        self.assertEqual(seen["options"], {"temperature": 0})

    def test_ollama_ok_parses_content_and_tokens(self):
        r = complete_with(OK_BODY)
        self.assertEqual((r.status, r.content["score"], r.prompt_tokens, r.completion_tokens),
                         ("OK", 12, 100, 20))
        self.assertEqual(r.raw, OK_BODY)
        self.assertIsInstance(r.latency_ms, int)
        self.assertGreaterEqual(r.latency_ms, 0)
        self.assertIsNone(r.error)

    def test_schema_invalid_output_is_error(self):
        r = complete_with(body_with_content('{"score": 400}'))
        self.assertEqual(r.status, "ERROR")
        self.assertIsNone(r.content)
        self.assertIn("schema", r.error)
        self.assertEqual(r.raw["eval_count"], 20)
        self.assertEqual((r.prompt_tokens, r.completion_tokens), (100, 20))

    def test_non_json_output_is_error(self):
        r = complete_with(body_with_content("not json"))
        self.assertEqual(r.status, "ERROR")
        self.assertIsNone(r.content)
        self.assertIn("JSON", r.error)

    def test_missing_message_is_error(self):
        r = complete_with({"done": True})
        self.assertEqual(r.status, "ERROR")
        self.assertIsNone(r.content)

    def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        r = complete_via(handler)
        self.assertEqual(r.status, "TIMEOUT")
        self.assertIsNone(r.content)
        self.assertIsNone(r.raw)

    def test_connection_failure_is_error(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        self.assertEqual(complete_via(handler).status, "ERROR")

    def test_http_error(self):
        r = complete_via(lambda request: httpx.Response(500, text="boom"))
        self.assertEqual(r.status, "ERROR")
        self.assertIn("500", r.error)

    def test_default_client_is_created_when_none_given(self):
        self.assertIsInstance(OllamaProvider("http://h:11434"), OllamaProvider)


class RequestHashTest(unittest.TestCase):
    def test_request_hash_stable_and_sensitive(self):
        self.assertEqual(request_hash(REQ), request_hash(dataclasses.replace(REQ)))
        self.assertNotEqual(request_hash(REQ), request_hash(dataclasses.replace(REQ, model="qwen3:8b")))
        self.assertNotEqual(request_hash(REQ), request_hash(dataclasses.replace(REQ, input_message_id="m-2")))

    def test_request_hash_ignores_key_order_and_unhashed_fields(self):
        reordered = dataclasses.replace(REQ, options=dict(reversed(list(OPTIONS.items()))))
        self.assertEqual(request_hash(REQ), request_hash(reordered))
        self.assertEqual(request_hash(REQ), request_hash(dataclasses.replace(REQ, timeout_ms=1)))

    def test_request_hash_changes_with_schema_only(self):
        edited = dataclasses.replace(REQ, schema={**SCHEMA, "required": ["score"]})
        self.assertNotEqual(request_hash(REQ), request_hash(edited))

    def test_request_hash_is_sha256_hex(self):
        self.assertRegex(request_hash(REQ), r"^[0-9a-f]{64}$")


class RecordedProviderTest(unittest.TestCase):
    def test_recorded_provider(self):
        p = RecordedProvider({request_hash(REQ): {"content": {"score": 1, "rationale": "r"}}})
        hit = p.complete(REQ)
        self.assertEqual((hit.status, hit.content["score"]), ("OK", 1))
        miss = p.complete(dataclasses.replace(REQ, model="x"))
        self.assertEqual((miss.status, miss.error, miss.content), ("ERROR", "no recording", None))

    def test_recording_violating_request_schema_is_error(self):
        p = RecordedProvider({request_hash(REQ): {"content": {"score": 400, "rationale": "r"}}})
        r = p.complete(REQ)
        self.assertEqual((r.status, r.content), ("ERROR", None))
        self.assertIn("schema", r.error)

    def test_malformed_recording_is_error(self):
        h = request_hash(REQ)
        for rec in ({}, {"content": "text"}, {"content": None}, {"content": [1]}, None, "x"):
            with self.subTest(rec=rec):
                r = RecordedProvider({h: rec}).complete(REQ)
                self.assertEqual((r.status, r.content), ("ERROR", None))
                self.assertIn("malformed", r.error)

    def test_recording_for_old_schema_is_a_miss(self):
        p = RecordedProvider({request_hash(REQ): {"content": {"score": 1, "rationale": "r"}}})
        r = p.complete(dataclasses.replace(REQ, schema={**SCHEMA, "required": ["score"]}))
        self.assertEqual((r.status, r.error), ("ERROR", "no recording"))

    def test_recording_carries_tokens_and_raw(self):
        rec = {"content": {"score": 1, "rationale": "r"}, "raw": OK_BODY,
               "prompt_tokens": 7, "completion_tokens": 3}
        r = RecordedProvider({request_hash(REQ): rec}).complete(REQ)
        self.assertEqual((r.prompt_tokens, r.completion_tokens, r.raw), (7, 3, OK_BODY))


class ProviderForTest(unittest.TestCase):
    def test_provider_for(self):
        self.assertIsInstance(provider_for({"provider": "ollama", "endpoint": "http://h"}), OllamaProvider)
        with self.assertRaises(NotImplementedError):
            provider_for({"provider": "openai-compatible", "endpoint": "e"}).complete(REQ)

    def test_placeholder_message(self):
        with self.assertRaisesRegex(NotImplementedError, "placeholder"):
            OpenAICompatibleProvider("e").complete(REQ)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            provider_for({"provider": "nope", "endpoint": "e"})

    def test_provider_for_passes_client(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=OK_BODY)))
        r = provider_for({"provider": "ollama", "endpoint": "http://h"}, client).complete(REQ)
        self.assertEqual(r.status, "OK")


class InteractionRecordTest(unittest.TestCase):
    def validate(self, rec):
        fastavro.validation.validate(
            rec, fastavro.parse_schema(load_avro_schema("agent.llm-interactions-value")), raise_errors=True)

    def test_llm_interaction_record_validates(self):
        rec = llm_interaction_record(ENV, REQ, complete_with(OK_BODY), "none")
        self.validate(rec)
        self.assertEqual(rec["request_hash"], request_hash(REQ))
        self.assertEqual(rec["status"], "OK")
        self.assertEqual(rec["guardrail_result"], "none")
        self.assertEqual((rec["provider"], rec["model"]), ("ollama", "qwen3:4b"))
        self.assertEqual((rec["prompt_ref"], rec["input_message_id"]), ("risk-prompt-v1", "m-1"))
        self.assertEqual((rec["prompt_tokens"], rec["completion_tokens"]), (100, 20))
        self.assertIsNone(rec["error"])

    def test_record_json_fields_are_compact(self):
        rec = llm_interaction_record(ENV, REQ, complete_with(OK_BODY))
        self.assertEqual(rec["options_json"], json.dumps(OPTIONS, separators=(",", ":")))
        self.assertEqual(json.loads(rec["rendered_prompt"]), REQ.messages)
        self.assertNotIn(", ", rec["rendered_prompt"])
        self.assertEqual(json.loads(rec["response_json"]), OK_BODY)
        self.assertEqual(rec["response_json"], json.dumps(OK_BODY, separators=(",", ":")))
        sent = json.loads(rec["request_json"])
        self.assertEqual((sent["model"], sent["messages"], sent["schema"]), ("qwen3:4b", REQ.messages, SCHEMA))
        self.assertIsNone(rec["guardrail_result"])

    def test_record_envelope_is_avro_envelope(self):
        rec = llm_interaction_record(ENV, REQ, complete_with(OK_BODY))
        self.assertEqual(rec["envelope"], ENV.to_avro())

    def test_record_integers_stay_integers(self):
        rec = llm_interaction_record(ENV, REQ, complete_with(OK_BODY))
        self.assertIn('"seed":42', rec["options_json"])
        self.assertNotIn("42.0", rec["request_json"])
        self.assertIs(type(rec["latency_ms"]), int)

    def test_timeout_record_validates_with_null_response(self):
        resp = LLMResponse("TIMEOUT", None, None, 30000, error="timed out")
        rec = llm_interaction_record(ENV, REQ, resp)
        self.validate(rec)
        self.assertEqual(rec["status"], "TIMEOUT")
        self.assertIsNone(rec["response_json"])
        self.assertIsNone(rec["prompt_tokens"])
        self.assertEqual(rec["error"], "timed out")

    def test_error_record_validates(self):
        rec = llm_interaction_record(ENV, REQ, complete_with(body_with_content('{"score": 400}')), "blocked")
        self.validate(rec)
        self.assertEqual(rec["status"], "ERROR")
        self.assertIn("schema", rec["error"])


class ObserveLlmTest(unittest.TestCase):
    def test_observe_llm_records_latency_by_agent_model_status(self):
        before = latency_count("obs-agent", "qwen3:4b", "OK")
        observe_llm("obs-agent", REQ, LLMResponse("OK", {}, None, 12))
        self.assertEqual(latency_count("obs-agent", "qwen3:4b", "OK"), before + 1)
        total = REGISTRY.get_sample_value(
            "dtm_llm_latency_ms_sum", {"agent": "obs-agent", "model": "qwen3:4b", "status": "OK"})
        self.assertEqual(total, 12.0)
        self.assertEqual(latency_count("obs-agent", "qwen3:4b", "TIMEOUT"), 0.0)


if __name__ == "__main__":
    unittest.main()
