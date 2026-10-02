"""OllamaProvider's model awareness — what it learns from /api/show and how
that changes what it sends. A fake `requests` stands in for the server, so
these run with no Ollama installed. The live paths (a real reasoning model,
a real embedding model) are not covered here."""

from __future__ import annotations

import json

import pytest

from app.providers import ollama as ollama_module
from app.providers.base import ConversationError
from app.providers.ollama import OllamaProvider, _parse_parameter_billions


def _show(capabilities=("completion",), context_length=32768, parameter_size="7.6B", family="qwen2") -> dict:
    return {
        "capabilities": list(capabilities),
        "model_info": {f"{family}.context_length": context_length},
        "details": {"parameter_size": parameter_size, "family": family},
    }


class _Resp:
    def __init__(self, payload=None, status=200, lines=None):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self._lines = lines or []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise ollama_module.requests.HTTPError(f"status {self.status_code}")

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOllama:
    """Answers /api/show per model and records /api/chat payloads."""

    def __init__(self, shows: dict[str, dict | None], chat_lines=None, tags=None):
        self.shows = shows
        self.chat_lines = chat_lines or [json.dumps({"message": {"content": "hi"}, "done": True})]
        self.tags = tags or []
        self.chat_payloads: list[dict] = []
        self.show_calls: list[str] = []

    def post(self, url, json=None, timeout=None, stream=False):
        if url.endswith("/api/show"):
            self.show_calls.append(json["model"])
            show = self.shows.get(json["model"])
            return _Resp(show, status=200 if show is not None else 404)
        self.chat_payloads.append(json)
        return _Resp(lines=self.chat_lines)

    def get(self, url, timeout=None):
        return _Resp({"models": [{"name": n} for n in self.tags]})


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs) -> _FakeOllama:
        server = _FakeOllama(**kwargs)
        monkeypatch.setattr(ollama_module.requests, "post", server.post)
        monkeypatch.setattr(ollama_module.requests, "get", server.get)
        return server

    return install


def _provider(model="m", num_ctx=8192) -> OllamaProvider:
    return OllamaProvider({"model": model, "base_url": "http://ollama", "num_ctx": num_ctx}, timeout=30)


# --- parsing ---


@pytest.mark.parametrize(
    ("size", "expected"),
    [("7.6B", 7.6), ("116.8B", 116.8), ("567M", 0.567), (" 70.6b ", 70.6), ("", None), (None, None), ("big", None)],
)
def test_parameter_size_parsing(size, expected):
    parsed = _parse_parameter_billions(size)
    if expected is None:
        assert parsed is None
    else:
        assert parsed == pytest.approx(expected)


# --- construction and prepare ---


def test_an_unset_model_is_a_clear_error_not_a_silent_default():
    with pytest.raises(ConversationError, match=r"conversation\.ollama\.model is not set"):
        OllamaProvider({"base_url": "http://ollama"}, timeout=30)


def test_construction_does_no_network(fake):
    """The lookup happens in prepare(), which ConversationClient calls off
    the event loop — constructing the adapter alone must stay free."""
    server = fake(shows={"m": _show()})
    _provider()
    assert server.show_calls == []
    assert server.chat_payloads == []


def test_prepare_looks_the_model_up_only_once(fake):
    server = fake(shows={"m": _show()})
    provider = _provider()
    provider.prepare()
    provider.complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    provider.complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    assert server.show_calls == ["m"]


def test_an_embedding_only_model_is_rejected_at_prepare(fake):
    fake(shows={"nomic-embed-text": _show(capabilities=("embedding",))})
    with pytest.raises(ConversationError, match="can't hold a conversation"):
        _provider("nomic-embed-text").prepare()


def test_an_unknown_or_unreachable_model_is_tolerated(fake):
    fake(shows={})  # /api/show 404s
    provider = _provider("not-pulled")
    provider.prepare()
    assert provider._info is None
    assert provider.num_ctx == 8192


def test_a_server_without_capability_reporting_is_not_treated_as_non_chat(fake):
    fake(shows={"m": _show(capabilities=())})
    _provider().prepare()  # no raise


def test_num_ctx_is_capped_at_the_models_native_context(fake):
    fake(shows={"m": _show(context_length=4096)})
    provider = _provider(num_ctx=8192)
    provider.prepare()
    assert provider.num_ctx == 4096


def test_num_ctx_within_the_native_context_is_left_alone(fake):
    fake(shows={"m": _show(context_length=32768)})
    provider = _provider(num_ctx=8192)
    provider.prepare()
    assert provider.num_ctx == 8192


def test_an_unset_num_ctx_stays_unset(fake):
    fake(shows={"m": _show(context_length=4096)})
    provider = _provider(num_ctx=None)
    provider.prepare()
    assert provider.num_ctx is None


# --- prompt tier ---


@pytest.mark.parametrize(
    ("model", "parameter_size", "expected"),
    [
        ("gpt-oss:120b", "116.8B", "frontier"),  # no size marker in the name the old guess knew
        ("deepseek-r1:671b", "671.0B", "frontier"),
        ("llama3.1:70b", "70.6B", "frontier"),
        ("qwen2.5-coder:7b", "7.6B", "small"),
        ("qwen3:30b-a3b", "30.5B", "small"),
    ],
)
def test_prompt_tier_follows_reported_parameter_count(fake, model, parameter_size, expected):
    fake(shows={model: _show(parameter_size=parameter_size)})
    provider = _provider(model)
    provider.prepare()
    assert provider.prompt_tier() == expected


@pytest.mark.parametrize(("model", "expected"), [("llama3.1:70b", "frontier"), ("gpt-oss:120b", "small")])
def test_prompt_tier_falls_back_to_the_name_when_the_server_cant_say(fake, model, expected):
    fake(shows={})
    provider = _provider(model)
    provider.prepare()
    assert provider.prompt_tier() == expected


# --- reasoning ---


def test_a_non_thinking_model_is_never_sent_think(fake):
    server = fake(shows={"m": _show(capabilities=("completion", "tools"))})
    _provider().complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    assert "think" not in server.chat_payloads[0]
    assert server.chat_payloads[0]["options"]["num_predict"] == 300


def test_a_thinking_model_has_reasoning_turned_off(fake):
    server = fake(shows={"qwen3:8b": _show(capabilities=("completion", "thinking"), family="qwen3")})
    _provider("qwen3:8b").complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    assert server.chat_payloads[0]["think"] is False
    assert server.chat_payloads[0]["options"]["num_predict"] == 300


def test_gpt_oss_gets_low_effort_and_extra_budget_since_it_cant_turn_reasoning_off(fake):
    server = fake(shows={"gpt-oss:20b": _show(capabilities=("completion", "thinking"), family="gptoss")})
    _provider("gpt-oss:20b").complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    payload = server.chat_payloads[0]
    assert payload["think"] == "low"
    assert payload["options"]["num_predict"] == 300 + ollama_module._ALWAYS_ON_REASONING_ALLOWANCE


def test_reasoning_that_uses_the_whole_budget_says_so(fake):
    lines = [
        json.dumps({"message": {"thinking": "Let me consider..."}}),
        json.dumps({"message": {"content": ""}, "done": True, "prompt_eval_count": 5, "eval_count": 300}),
    ]
    fake(shows={"r": _show(capabilities=("completion", "thinking"))}, chat_lines=lines)
    with pytest.raises(ConversationError, match="whole 300-token budget reasoning"):
        _provider("r").complete([{"role": "user", "content": "q"}], "sys", 300, None, None)


def test_thinking_text_is_never_returned_as_the_answer(fake):
    lines = [
        json.dumps({"message": {"thinking": "secret reasoning"}}),
        json.dumps({"message": {"content": "The answer."}, "done": True}),
    ]
    fake(shows={"r": _show(capabilities=("completion", "thinking"))}, chat_lines=lines)
    result = _provider("r").complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    assert result.text == "The answer."


def test_num_ctx_sent_is_the_capped_value(fake):
    server = fake(shows={"m": _show(context_length=4096)})
    _provider(num_ctx=8192).complete([{"role": "user", "content": "q"}], "sys", 300, None, None)
    assert server.chat_payloads[0]["options"]["num_ctx"] == 4096


# --- model listing ---


def test_list_models_offers_only_chat_capable_models(fake):
    fake(
        shows={
            "qwen2.5-coder:7b": _show(),
            "nomic-embed-text:latest": _show(capabilities=("embedding",)),
            "mystery:latest": None,  # /api/show fails — kept, not evidence it can't chat
        },
        tags=["qwen2.5-coder:7b", "nomic-embed-text:latest", "mystery:latest"],
    )
    assert OllamaProvider.list_models({"base_url": "http://ollama"}) == ["mystery:latest", "qwen2.5-coder:7b"]
