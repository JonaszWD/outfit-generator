import copy
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.engine import vocab
from app.models import Category
from app.services.tagging import (
    TAGGED_FIELDS,
    ClaudeTagger,
    FakeTagger,
    OpenAICompatTagger,
    TaggingError,
    TagResult,
    apply_color_check,
    build_tagger,
    low_confidence_fields,
    tag_schema,
)
from tests.conftest import image_bytes

CONFIDENCE = dict.fromkeys(TAGGED_FIELDS, 0.9)
VALID = {
    "category": "top",
    "subtype": "shirt",
    "primary_color": "navy",
    "secondary_colors": ["white"],
    "pattern": "striped",
    "seasons": ["spring", "autumn"],
    "material": "cotton",
    "layer_role": "base",
    "confidence": CONFIDENCE,
}


def make_result(**overrides: Any) -> TagResult:
    return TagResult.model_validate({**VALID, **overrides})


# ---------------------------------------------------------------- validation


def test_fake_tagger_output_validates_and_is_deterministic() -> None:
    import asyncio

    tagger = FakeTagger()
    for color in ("red", "navy", "white"):
        data = image_bytes(color=color)
        first = asyncio.run(tagger.tag(data))
        second = asyncio.run(tagger.tag(data))
        assert first == second
        TagResult.model_validate(first.model_dump())  # round-trips through validation
        assert vocab.is_valid_subtype(first.category, first.subtype)
        assert set(first.confidence) == set(TAGGED_FIELDS)


def test_fake_tagger_reads_the_dominant_color() -> None:
    import asyncio

    result = asyncio.run(FakeTagger().tag(image_bytes(color="red", size=(90, 90))))
    assert result.primary_color == "red"


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "hat"},
        {"subtype": "jeans"},  # not a top
        {"primary_color": "turquoise"},
        {"secondary_colors": ["nope"]},
        {"pattern": "paisley"},
        {"seasons": ["monsoon"]},
        {"material": "unobtainium"},
        {"layer_role": "sideways"},
        {"confidence": {"category": 0.9}},  # missing fields
        {"confidence": {**CONFIDENCE, "subtype": 1.5}},
    ],
)
def test_tag_result_rejects_values_outside_vocab(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_result(**overrides)


def test_schema_is_built_from_vocab() -> None:
    props = tag_schema()["properties"]
    assert props["primary_color"]["enum"] == list(vocab.COLOR_NAMES)
    assert props["category"]["enum"] == [c.value for c in Category]
    assert "formality" not in props
    assert tag_schema()["additionalProperties"] is False


def test_low_confidence_fields() -> None:
    confidence = {**CONFIDENCE, "subtype": 0.4, "material": 0.69}
    assert low_confidence_fields(confidence, 0.7) == ["subtype", "material"]
    assert low_confidence_fields(confidence, 0.3) == []
    assert low_confidence_fields(None, 0.7) == []


# ----------------------------------------------------------- color cross-check


def test_color_agreement_leaves_confidence_alone() -> None:
    result = apply_color_check(make_result(), [("navy", 0.8), ("white", 0.2)])
    assert result.confidence["primary_color"] == 0.9 and result.flags == []


def test_close_colors_count_as_agreeing() -> None:
    result = apply_color_check(make_result(primary_color="black"), [("charcoal", 0.9)])
    assert result.flags == []


def test_color_disagreement_lowers_confidence_and_flags_without_overriding() -> None:
    original = make_result(primary_color="navy")
    result = apply_color_check(original, [("red", 0.9)])
    assert result.primary_color == "navy"  # neither answer is silently replaced
    assert result.confidence["primary_color"] < 0.5
    assert result.confidence["subtype"] == 0.9  # other fields untouched
    assert len(result.flags) == 1 and "red" in result.flags[0] and "navy" in result.flags[0]
    assert low_confidence_fields(result.confidence, 0.7) == ["primary_color"]
    assert original.confidence["primary_color"] == 0.9  # input not mutated


def test_secondary_dominant_color_can_vindicate_the_model() -> None:
    result = apply_color_check(make_result(primary_color="navy"), [("white", 0.5), ("navy", 0.4)])
    assert result.flags == []


def test_no_pixel_data_means_no_check() -> None:
    assert apply_color_check(make_result(), []).flags == []


# ------------------------------------------------------------- ClaudeTagger


class FakeMessages:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []
        self.stop_reason = "end_turn"

    async def create(self, **kwargs: Any) -> Any:
        recorded = dict(kwargs)
        recorded["messages"] = copy.deepcopy(kwargs["messages"])
        self.calls.append(recorded)
        text = self.outputs.pop(0)
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)]
        )


def fake_client(*outputs: str) -> SimpleNamespace:
    return SimpleNamespace(messages=FakeMessages(list(outputs)))


async def test_claude_tagger_valid_response() -> None:
    client = fake_client(json.dumps(VALID))
    tagger = ClaudeTagger(model="some-model", client=client)
    result = await tagger.tag(image_bytes())
    assert result.subtype == "shirt" and result.confidence["subtype"] == 0.9
    (call,) = client.messages.calls
    assert call["model"] == "some-model"  # from settings, not hardcoded
    assert call["output_config"]["format"]["type"] == "json_schema"
    image_block = call["messages"][0]["content"][0]
    assert image_block["type"] == "image" and image_block["source"]["media_type"] == "image/jpeg"
    assert "navy" in call["system"] and "ormality" not in call["system"]


async def test_claude_tagger_retries_once_with_validation_error() -> None:
    bad = json.dumps({**VALID, "subtype": "jeans"})  # jeans is not a top
    client = fake_client(bad, json.dumps(VALID))
    result = await ClaudeTagger(model="m", client=client).tag(image_bytes())
    assert result.subtype == "shirt"
    first, second = client.messages.calls
    assert len(first["messages"]) == 1 and len(second["messages"]) == 3
    assert second["messages"][1] == {"role": "assistant", "content": bad}
    assert "not a top subtype" in second["messages"][2]["content"]


async def test_claude_tagger_unparseable_then_valid() -> None:
    client = fake_client("this is not json", json.dumps(VALID))
    result = await ClaudeTagger(model="m", client=client).tag(image_bytes())
    assert result.category is Category.TOP
    assert len(client.messages.calls) == 2


async def test_claude_tagger_fails_gracefully_after_two_bad_outputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = json.dumps({**VALID, "pattern": "paisley"})
    client = fake_client(bad, bad)
    tagger = ClaudeTagger(model="m", api_key="sk-ant-secret-key", client=client)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TaggingError, match="did not pass validation"):
        await tagger.tag(image_bytes())
    assert len(client.messages.calls) == 2  # exactly one retry
    assert "sk-ant-secret-key" not in caplog.text  # no secrets or image bytes in logs


async def test_claude_tagger_refusal_is_a_tagging_error() -> None:
    client = fake_client(json.dumps(VALID))
    client.messages.stop_reason = "refusal"
    with pytest.raises(TaggingError, match="declined"):
        await ClaudeTagger(model="m", client=client).tag(image_bytes())


async def test_claude_tagger_empty_output() -> None:
    client = fake_client("  ")
    with pytest.raises(TaggingError, match="no tags"):
        await ClaudeTagger(model="m", client=client).tag(image_bytes())


# ------------------------------------------------------------ openai-compatible (gemini)


def compat_tagger(*answers: str, status: int = 200) -> tuple[OpenAICompatTagger, list[Any]]:
    seen: list[Any] = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "schema not supported"}})
        body = {"choices": [{"message": {"content": queue.pop(0)}}]}
        return httpx.Response(200, json=body)

    tagger = OpenAICompatTagger(
        "gemini-x", "https://host.test/openai/", "k3y", transport=httpx.MockTransport(handler)
    )
    return tagger, seen


async def test_compat_tagger_sends_image_schema_and_key_and_parses() -> None:
    tagger, seen = compat_tagger(json.dumps(VALID))
    result = await tagger.tag(image_bytes())
    assert result.category == Category.TOP and result.subtype == "shirt"
    request = seen[0]
    assert str(request.url) == "https://host.test/openai/chat/completions"
    assert request.headers["authorization"] == "Bearer k3y"
    body = json.loads(request.content)
    assert body["model"] == "gemini-x"
    assert body["response_format"]["json_schema"]["schema"]["required"]
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


async def test_compat_tagger_retries_once_with_the_validation_error() -> None:
    bad = json.dumps({**VALID, "subtype": "jeans"})  # jeans is not a top
    tagger, seen = compat_tagger(bad, json.dumps(VALID))
    assert (await tagger.tag(image_bytes())).subtype == "shirt"
    retry = json.loads(seen[1].content)["messages"]
    assert retry[-2]["role"] == "assistant" and "failed validation" in retry[-1]["content"]


async def test_compat_tagger_gives_up_after_two_invalid_answers() -> None:
    tagger, seen = compat_tagger("not json", "still not json")
    with pytest.raises(TaggingError, match="validation twice"):
        await tagger.tag(image_bytes())
    assert len(seen) == 2


async def test_compat_tagger_retries_a_busy_server_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.openai_compat.RETRY_DELAYS", (0.0, 0.0))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json=[{"error": {"message": "high demand"}}])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]})

    tagger = OpenAICompatTagger(
        "m", "https://host.test", "k", transport=httpx.MockTransport(handler)
    )
    assert (await tagger.tag(image_bytes())).subtype == "shirt"
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("status", "message"),
    [(403, "rejected the API key"), (429, "rate limit"), (400, "not supported")],
)
async def test_compat_tagger_http_errors_are_readable_and_leak_nothing(
    status: int, message: str
) -> None:
    tagger, _ = compat_tagger(status=status)
    with pytest.raises(TaggingError, match=message) as caught:
        await tagger.tag(image_bytes())
    assert "k3y" not in str(caught.value) and "base64" not in str(caught.value)


# -------------------------------------------------------------------- factory


def _settings(**kwargs: Any) -> Settings:
    return Settings(database_url="postgresql+psycopg://x:y@localhost/z", **kwargs)


def test_build_tagger_picks_backend_from_settings() -> None:
    assert isinstance(build_tagger(_settings(tagger_backend="fake")), FakeTagger)
    claude = build_tagger(
        _settings(tagger_backend="claude", anthropic_api_key=SecretStr("k"), tagger_model="m-1")
    )
    assert isinstance(claude, ClaudeTagger) and claude.model == "m-1"


def test_build_tagger_gemini_uses_the_gemini_endpoint_and_requires_a_key() -> None:
    gemini = build_tagger(
        _settings(tagger_backend="gemini", gemini_api_key=SecretStr("g"), gemini_model="gem-1")
    )
    assert isinstance(gemini, OpenAICompatTagger) and gemini.model == "gem-1"
    assert gemini.base_url.startswith("https://generativelanguage.googleapis.com/")
    with pytest.raises(TaggingError, match="GEMINI_API_KEY"):
        build_tagger(_settings(tagger_backend="gemini", gemini_api_key=None))


def test_build_tagger_claude_requires_key() -> None:
    with pytest.raises(TaggingError, match="ANTHROPIC_API_KEY"):
        build_tagger(_settings(tagger_backend="claude", anthropic_api_key=None))


def test_api_key_is_not_in_settings_repr() -> None:
    s = _settings(anthropic_api_key=SecretStr("sk-ant-visible"))
    assert "sk-ant-visible" not in repr(s) and "sk-ant-visible" not in str(s)
