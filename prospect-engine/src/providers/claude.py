"""Claude: structured-output calls (ICP translation, qualification, title classification).

Verified against the Claude API reference and the installed `anthropic` SDK on
2026-11-24 (docs/PROVIDERS.md):
- client.messages.parse(..., output_format=PydanticModel) -> response.parsed_output
- Sonnet calls use the server-side refusal fallback via client.beta.messages.parse
- the SDK's own retries are off; providers.base does retries, budget, cache and logging.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import anthropic
from pydantic import BaseModel

from src.providers.base import (
    CallContext,
    CostEstimate,
    ProviderError,
    ProviderHTTPError,
    RetryableError,
    guarded_call,
    provider_settings,
    require_key,
)

PROVIDER = "claude"
KEY_ENV = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)


@dataclass
class ClaudeAnswer(Generic[T]):
    """A validated answer plus the model that actually produced it."""

    output: T
    model: str               # response.model: the fallback model if the fallback served it
    fallback_used: bool = False

_clients: dict[str, anthropic.Anthropic] = {}


def _client() -> anthropic.Anthropic:
    key = require_key(KEY_ENV)
    if key not in _clients:
        _clients[key] = anthropic.Anthropic(api_key=key, max_retries=0, timeout=120.0)
    return _clients[key]


def _prices(settings: dict[str, Any], model: str) -> tuple[float, float]:
    table = provider_settings(settings, PROVIDER).get("unit_cost_usd_per_million_tokens") or {}
    p = table.get(model)
    if not p:
        raise ProviderError(f"claude: no price configured for model {model} in settings.yaml")
    return float(p["input"]), float(p["output"])


def estimate_cost(settings: dict[str, Any], model: str, prompt_chars: int,
                  max_tokens: int) -> CostEstimate:
    """Worst case: input ~ chars/4 tokens, output = max_tokens (thinking + answer)."""
    pin, pout = _prices(settings, model)
    tokens_in = prompt_chars / 4 + 500  # + schema/system overhead
    return CostEstimate(usd=(tokens_in * pin + max_tokens * pout) / 1_000_000)


def _usage_cost(settings: dict[str, Any], model: str, usage: dict[str, Any]) -> CostEstimate:
    pin, pout = _prices(settings, model)
    usd = (usage.get("input_tokens", 0) * pin
           + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
           + usage.get("cache_read_input_tokens", 0) * pin * 0.1
           + usage.get("output_tokens", 0) * pout) / 1_000_000
    return CostEstimate(usd=usd)


def _task_setting(settings: dict[str, Any], key: str, task: str, default: Any) -> Any:
    return (provider_settings(settings, PROVIDER).get(key) or {}).get(task, default)


def structured(ctx: CallContext, *, task: str, model: str, system: str, user: str,
               schema: type[T], fixture_key: str | None = None) -> T:
    """Like `structured_answer`, returning only the validated output."""
    return structured_answer(ctx, task=task, model=model, system=system, user=user,
                             schema=schema, fixture_key=fixture_key).output


def structured_answer(ctx: CallContext, *, task: str, model: str, system: str, user: str,
                      schema: type[T], fixture_key: str | None = None) -> ClaudeAnswer[T]:
    """Ask Claude for output matching `schema`. Cached, budgeted, retried and logged.

    Raises ProviderError on refusal, truncation or API errors (the stage marks the
    record and continues).
    """
    settings = ctx.settings
    max_tokens = int(_task_setting(settings, "max_tokens", task, 8000))
    effort = _task_setting(settings, "effort", task, "medium")
    use_fallback = (model.startswith("claude-sonnet")
                    and provider_settings(settings, PROVIDER).get("sonnet_refusal_fallback", True))

    def send() -> dict[str, Any]:
        client = _client()
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema, output_config={"effort": effort},
        )
        try:
            if use_fallback:
                resp = client.beta.messages.parse(betas=[FALLBACK_BETA], fallbacks="default",
                                                  **kwargs)
            else:
                resp = client.messages.parse(**kwargs)
        except anthropic.RateLimitError as exc:
            raise RetryableError(f"claude: rate limited (429)", 429) from None
        except anthropic.InternalServerError as exc:
            raise RetryableError(f"claude: server error ({exc.status_code})", exc.status_code) from None
        except anthropic.APIConnectionError:
            raise RetryableError("claude: network error or timeout") from None
        except anthropic.AuthenticationError:
            raise ProviderHTTPError("claude: 401: check ANTHROPIC_API_KEY in .env", 401) from None
        except anthropic.APIStatusError as exc:
            raise ProviderHTTPError(f"claude: HTTP {exc.status_code}: {exc.message}"[:300],
                                    exc.status_code) from None

        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise ProviderError(f"claude: refused (category={category})")
        if resp.stop_reason == "max_tokens":
            raise ProviderError(f"claude: output cut off at max_tokens={max_tokens}")
        if resp.parsed_output is None:
            raise ProviderError("claude: no parsed output returned")
        usage = resp.usage
        # Docs: a "fallback_message" entry in usage.iterations = the fallback model served it.
        fallback_used = any(getattr(it, "type", None) == "fallback_message"
                            for it in (getattr(usage, "iterations", None) or []))
        return {
            "output": resp.parsed_output.model_dump(mode="json"),
            "model": resp.model,
            "fallback_used": fallback_used,
            "usage": {
                "input_tokens": usage.input_tokens or 0,
                "output_tokens": usage.output_tokens or 0,
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            },
        }

    def actual(data: dict[str, Any]) -> CostEstimate:
        served_by = data.get("model") or model
        try:
            return _usage_cost(settings, served_by, data.get("usage") or {})
        except ProviderError:  # fallback model without a configured price: use requested model's
            return _usage_cost(settings, model, data.get("usage") or {})

    hash_params = {"model": model, "system": system, "user": user, "effort": effort,
                   "schema": json.dumps(schema.model_json_schema(), sort_keys=True)}
    data = guarded_call(ctx, provider=PROVIDER, endpoint=task, hash_params=hash_params,
                        estimate=estimate_cost(settings, model, len(system) + len(user), max_tokens),
                        send=send, actual=actual, fixture_key=fixture_key)
    served = data.get("model") or model
    used = bool(data.get("fallback_used"))
    if used:
        log.info("claude %s: answered by fallback model %s (requested %s)", task, served, model)
    return ClaudeAnswer(output=schema.model_validate(data["output"]), model=served,
                        fallback_used=used)
