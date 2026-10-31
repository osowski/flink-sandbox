"""The LLM call an agent makes: a request built from its settings, issued through a cached provider."""
import json
from collections.abc import Callable

from dtm.common.config import AgentSettings
from dtm.mesh.llm import LLMProvider, LLMRequest, LLMResponse, observe_llm, provider_for

_DEFAULT_TIMEOUT_MS = 30000


class AgentLlm:
    """Builds ``LLMRequest``s from an agent's settings and sends them through the provider for its config.

    The provider is rebuilt when ``config_version`` changes, so a hot-reloaded model or endpoint applies
    to the next call.
    """

    def __init__(self, agent: str, provider_factory: Callable[[dict], LLMProvider] = provider_for):
        self._agent = agent
        self._provider_factory = provider_factory
        self._cached: tuple[str, LLMProvider] | None = None

    def complete(self, settings: AgentSettings, messages: list[dict], schema: dict,
                 input_message_id: str) -> tuple[LLMRequest, LLMResponse]:
        if settings.prompt_text is None:
            raise ValueError(f"agent {self._agent!r} has no prompt configured")
        llm = settings.llm
        req = LLMRequest(provider=llm["provider"], model=llm["model"], messages=messages, schema=schema,
                         options=llm.get("options", {}), prompt_ref=settings.prompt_ref,
                         input_message_id=input_message_id, timeout_ms=llm.get("timeoutMs", _DEFAULT_TIMEOUT_MS))
        resp = self._provider(settings).complete(req)
        observe_llm(self._agent, req, resp)
        return req, resp

    def _provider(self, settings: AgentSettings) -> LLMProvider:
        if self._cached is None or self._cached[0] != settings.config_version:
            self._cached = (settings.config_version, self._provider_factory(settings.llm))
        return self._cached[1]


def system_and_user(prompt_text: str, facts: dict) -> list[dict]:
    """Return the chat messages for a prompt and its facts; the facts are compact JSON with sorted keys."""
    return [{"role": "system", "content": prompt_text},
            {"role": "user", "content": json.dumps(facts, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False)}]
