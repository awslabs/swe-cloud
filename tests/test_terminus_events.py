"""The terminus-2 wrapper raises on safety-filter declines and counts truncated replies."""

import asyncio
import logging
from types import SimpleNamespace

from swecloud.agents import terminus


def _response(finish_reason: str, completion_tokens: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=""))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=completion_tokens),
    )


def _call(monkeypatch, finish_reason: str, completion_tokens: int = 7):
    async def fake(*args, **kwargs):
        return _response(finish_reason, completion_tokens)

    monkeypatch.setattr(terminus, "_acompletion", fake)
    monkeypatch.setattr(terminus.litellm, "completion_cost", lambda **kwargs: 0.5)
    events = terminus._LLMEvents(logger=logging.getLogger("test"))
    token = terminus._events.set(events)
    try:
        result = asyncio.run(terminus._acompletion_checked(model="bedrock/m"))
    except terminus.ContentFilteredError as error:
        result = error
    finally:
        terminus._events.reset(token)
    return result, events


def test_content_filter_raises_and_is_counted(monkeypatch):
    result, events = _call(monkeypatch, "content_filter")
    assert isinstance(result, terminus.ContentFilteredError)
    assert events.content_filter_declines == 1
    assert events.uncounted_cost_usd == 0.5


def test_length_is_counted_and_passed_through(monkeypatch):
    result, events = _call(monkeypatch, "length", completion_tokens=128000)
    assert result.choices[0].finish_reason == "length"
    assert events.max_tokens_truncations == 1
    assert events.uncounted_output_tokens == 128000


def test_normal_reply_is_untouched(monkeypatch):
    result, events = _call(monkeypatch, "stop")
    assert result.choices[0].finish_reason == "stop"
    assert events.details == []


def test_litellm_is_patched():
    assert terminus.litellm.acompletion is terminus._acompletion_checked
