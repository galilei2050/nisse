"""Routing a sub-agent to an open model, when its config names one.

A model id with a slash (`moonshotai/kimi-k2-thinking`) is a gateway model: it goes to Vercel's
Anthropic-compatible endpoint instead of to Anthropic. Everything else is unchanged — the default
path is still `deps.anthropic`.

Why `retrieval` runs on one. Measured over 24 runs on real sub-questions from the expensive tail,
against `claude-sonnet-5` on the same four briefs: the completeness judge passed 9/12 vs 2/4, the
share of cited sources the run had actually opened was 36% vs 15%, and the median run cost $0.0256
vs $0.1164. It reads more of what it cites than the model it replaces, and costs a quarter as much.
"""

import functools
import json

import httpx
from anthropic import AsyncAnthropic
from baski.agents.pricing import MODEL_PRICING, ModelPrice
from baski.env import get_env

BASE_URL = "https://ai-gateway.vercel.sh"

# Per-bucket rates from the gateway catalogue, checked against what it actually charged: the balance
# was read before and after single runs and matched to six decimals. Not derived from `input` by
# Anthropic's multipliers — kimi bills a cached read at 0.30x and writes no cache at all.
GATEWAY_PRICES: dict[str, ModelPrice] = {
    "moonshotai/kimi-k2-thinking": ModelPrice(input=0.47, output=2.00, cache_write=0.0, cache_read=0.141),
    "openai/gpt-oss-120b": ModelPrice(input=0.10, output=0.50, cache_write=0.0, cache_read=0.0),
    "zai/glm-4.7-flash": ModelPrice(input=0.07, output=0.40, cache_write=0.0, cache_read=0.0),
}


def is_gateway_model(model: str) -> bool:
    """A `creator/model` id is served through the gateway; a bare one is Anthropic's."""
    return "/" in model


def price_for(model: str) -> ModelPrice:
    """What the model costs — from the gateway catalogue for an open model, else Anthropic's table."""
    return GATEWAY_PRICES[model] if is_gateway_model(model) else MODEL_PRICING[model]


class _RepairThinkingSignature(httpx.AsyncHTTPTransport):
    """Rewrite `"signature": null` to `""` on the way out.

    The gateway returns thinking blocks with a null signature, and the agent loop echoes the model's
    own reply back on the next turn — where the API rejects the null it just issued
    (`messages.N.content: Invalid input`), killing every multi-turn run on the second turn. The same
    block with an empty string is accepted; verified by sending both. Repairing it here rather than
    switching thinking off keeps the model reasoning, which is what it was chosen for.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Repair the payload, then send it."""
        body = json.loads(request.content)
        for message in body.get("messages", []):
            content = message["content"]
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "thinking" and block.get("signature") is None:
                    block["signature"] = ""
        repaired = httpx.Request(
            method=request.method,
            url=request.url,
            headers=[(k, v) for k, v in request.headers.raw if k.lower() != b"content-length"],
            content=json.dumps(body).encode(),
            extensions=request.extensions,
        )
        return await super().handle_async_request(repaired)


@functools.cache
def gateway_client() -> AsyncAnthropic:
    """The one gateway client for the process — built on first use, reused after.

    Cached rather than held on `CoreDeps` because only a sub-agent whose config names an open model
    ever touches it: a process that never delegates there never opens the connection, and never
    needs the key to exist.
    """
    return AsyncAnthropic(
        api_key=str(get_env("VERCEL_AI_GATEWAY_API_KEY")),
        base_url=BASE_URL,
        timeout=600.0,
        http_client=httpx.AsyncClient(timeout=600.0, transport=_RepairThinkingSignature()),
    )
