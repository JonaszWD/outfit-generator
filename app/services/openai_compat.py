"""One chat-completion call for hosts that speak the OpenAI API with JSON-schema output
(Gemini's compatibility endpoint, OpenAI, Together, Fireworks, OpenRouter).

Shared by the tagger and the style reader. Errors never include the request, which holds the
photo and the API key.
"""

import asyncio
import base64
from typing import Any

import httpx

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
# Seconds to wait before each retry of a "try again later" answer (429, 500, 502, 503, 504).
# Gemini regularly answers 503 "high demand" for a moment; tests set this to ().
RETRY_DELAYS: tuple[float, ...] = (2.0, 6.0)
MAX_RETRY_AFTER = 20.0  # never sleep longer than this inside a request
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class CompatError(Exception):
    """The call failed. The message is safe to show to the user."""


def media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def image_message(text: str, image_bytes: bytes) -> dict[str, Any]:
    url = f"data:{media_type(image_bytes)};base64,"
    url += base64.standard_b64encode(image_bytes).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def http_error(label: str, response: httpx.Response) -> str:
    status = response.status_code
    if status in (401, 403):
        return f"The {label} rejected the API key ({status})."
    if status == 429:
        return f"The {label} rate limit was reached. Try again in a minute."
    detail = ""
    try:
        body = response.json()
        body = body[0] if isinstance(body, list) and body else body
        detail = str(body.get("error", {}).get("message", ""))[:200]
    except (ValueError, AttributeError):
        pass
    return f"{label.capitalize()} error {status}" + (f": {detail}" if detail else ".")


def _retry_after(response: httpx.Response) -> float:
    """Seconds the server asked us to wait (capped), or 0."""
    try:
        return min(max(float(response.headers.get("retry-after", "0")), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return 0.0


async def chat_json(
    *,
    label: str,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
    schema_name: str,
    timeout: float = 90.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """The model's raw JSON text. `label` names the service in messages ("tagging API")."""
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, *messages],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            for delay in (*RETRY_DELAYS, None):
                response = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if response.status_code not in RETRY_STATUSES or delay is None:
                    break
                await asyncio.sleep(max(delay, _retry_after(response)))
    except httpx.TimeoutException:
        raise CompatError(f"The {label} took too long to answer.") from None
    except httpx.HTTPError:
        raise CompatError(f"Could not reach the {label}.") from None
    if response.status_code != 200:
        raise CompatError(http_error(label, response))
    try:
        return str(response.json()["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError, ValueError):
        raise CompatError(f"The {label} returned an unexpected answer.") from None
