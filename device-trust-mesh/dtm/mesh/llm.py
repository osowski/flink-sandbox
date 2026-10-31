"""LLM providers behind one interface, plus the llm-interactions record built from each call."""
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx
from jsonschema import Draft202012Validator

from dtm.common.envelope import Envelope
from dtm.common.metrics import LLM_LATENCY_MS

OK, ERROR, TIMEOUT = "OK", "ERROR", "TIMEOUT"


@dataclass(frozen=True)
class LLMRequest:
    provider: str
    model: str
    messages: list[dict]
    schema: dict
    options: dict
    prompt_ref: str
    input_message_id: str
    timeout_ms: int = 30000


@dataclass(frozen=True)
class LLMResponse:
    status: str
    content: dict | None
    raw: dict | None
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


class LLMProvider(Protocol):
    def complete(self, req: LLMRequest) -> LLMResponse: ...


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def request_hash(req: LLMRequest) -> str:
    """SHA-256 over the canonical JSON of the fields that determine a completion."""
    canonical = json.dumps(
        {"provider": req.provider, "model": req.model, "options": req.options,
         "messages": req.messages, "schema": req.schema, "input_message_id": req.input_message_id},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _integral_floats_to_ints(value):
    """Return ``value`` with every integral float (``12.0``) turned into an int, recursively."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _integral_floats_to_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_integral_floats_to_ints(v) for v in value]
    return value


def _first_schema_error(schema: dict, content) -> str | None:
    errors = sorted(Draft202012Validator(schema).iter_errors(content), key=lambda e: list(e.path))
    return errors[0].message if errors else None


class OllamaProvider:
    """Calls Ollama's ``/api/chat`` with a JSON-schema ``format`` and validates the reply against it.

    Integral floats in the reply (``12.0``) become ints before validation, so integers stay JSON integers.
    """

    def __init__(self, endpoint: str, client: httpx.Client | None = None):
        self._url = endpoint.rstrip("/") + "/api/chat"
        self._client = client if client is not None else httpx.Client()

    def _body(self, req: LLMRequest) -> dict:
        body = {"model": req.model, "messages": req.messages, "stream": False, "format": req.schema}
        if "think" in req.options:
            body["think"] = req.options["think"]
        body["options"] = {k: v for k, v in req.options.items() if k != "think"}
        return body

    def complete(self, req: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            http = self._client.post(self._url, json=self._body(req), timeout=req.timeout_ms / 1000)
        except httpx.TimeoutException as exc:
            return LLMResponse(TIMEOUT, None, None, _elapsed_ms(started), error=f"timeout: {exc}")
        except httpx.HTTPError as exc:
            return LLMResponse(ERROR, None, None, _elapsed_ms(started), error=f"transport: {exc!r}")
        latency = _elapsed_ms(started)
        if http.status_code != 200:
            return LLMResponse(ERROR, None, None, latency,
                               error=f"http {http.status_code}: {http.text[:200]}")
        try:
            raw = http.json()
            text = raw["message"]["content"]
        except (ValueError, KeyError, TypeError):
            return LLMResponse(ERROR, None, None, latency, error="response has no message content")
        tokens = {"prompt_tokens": raw.get("prompt_eval_count"),
                  "completion_tokens": raw.get("eval_count")}
        try:
            content = _integral_floats_to_ints(json.loads(text))
        except (ValueError, TypeError):
            return LLMResponse(ERROR, None, raw, latency, error="output is not JSON", **tokens)
        problem = _first_schema_error(req.schema, content)
        if problem is not None:
            return LLMResponse(ERROR, None, raw, latency, error=f"output violates schema: {problem}", **tokens)
        return LLMResponse(OK, content, raw, latency, **tokens)


class OpenAICompatibleProvider:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def complete(self, req: LLMRequest) -> LLMResponse:
        raise NotImplementedError("openai-compatible provider is a placeholder")


class RecordedProvider:
    """Replays recorded completions keyed by ``request_hash``.

    A miss, a malformed recording, or recorded content that violates the request schema is an ``ERROR``.
    """

    def __init__(self, recordings: Mapping[str, dict]):
        self._recordings = recordings

    def complete(self, req: LLMRequest) -> LLMResponse:
        key = request_hash(req)
        if key not in self._recordings:
            return LLMResponse(ERROR, None, None, 0, error="no recording")
        rec = self._recordings[key]
        content = rec.get("content") if isinstance(rec, Mapping) else None
        if not isinstance(content, dict):
            return LLMResponse(ERROR, None, None, 0, error="malformed recording")
        problem = _first_schema_error(req.schema, content)
        if problem is not None:
            return LLMResponse(ERROR, None, None, 0, error=f"recording violates schema: {problem}")
        return LLMResponse(OK, content, rec.get("raw"), 0,
                           rec.get("prompt_tokens"), rec.get("completion_tokens"))


def provider_for(llm: dict, client: httpx.Client | None = None) -> LLMProvider:
    """Build the provider named by an agents.yaml ``llm`` block."""
    name = llm["provider"]
    if name == "ollama":
        return OllamaProvider(llm["endpoint"], client)
    if name == "openai-compatible":
        return OpenAICompatibleProvider(llm["endpoint"])
    if name == "recorded":
        raise NotImplementedError("recorded provider needs a recordings source; build RecordedProvider "
                                  "with the recordings for replay")
    raise ValueError(f"unknown llm provider {name!r}")


def observe_llm(agent: str, req: LLMRequest, resp: LLMResponse) -> None:
    """Record one completion's latency under the calling agent's name."""
    LLM_LATENCY_MS.labels(agent, req.model, resp.status).observe(resp.latency_ms)


def llm_interaction_record(envelope: Envelope, req: LLMRequest, resp: LLMResponse,
                           guardrail_result: str | None = None) -> dict:
    """Build the Avro ``LlmInteraction`` for one exchange; JSON members are compact strings."""
    request_fields = {"provider": req.provider, "model": req.model, "messages": req.messages,
                      "schema": req.schema, "options": req.options,
                      "prompt_ref": req.prompt_ref, "input_message_id": req.input_message_id}
    return {
        "envelope": envelope.to_avro(),
        "request_hash": request_hash(req),
        "provider": req.provider,
        "model": req.model,
        "options_json": _compact(req.options),
        "prompt_ref": req.prompt_ref,
        "rendered_prompt": _compact(req.messages),
        "input_message_id": req.input_message_id,
        "request_json": _compact(request_fields),
        "response_json": None if resp.raw is None else _compact(resp.raw),
        "latency_ms": resp.latency_ms,
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
        "guardrail_result": guardrail_result,
        "status": resp.status,
        "error": resp.error,
    }
