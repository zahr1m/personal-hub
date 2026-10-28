import json
from datetime import date

import anthropic
import httpx2
import pytest

from hub.budget import BudgetExceeded, record_usage
from hub.llm import AnthropicLlm, LlmError
from tests.fakes import RecordingClient

TODAY = date(2026, 11, 2)


def _ok(**over):
    d = {"kind": "note", "title": "x", "due_date": None, "contour": None, "section": None}
    return {**d, **over}


def _llm(settings, db, clock, client):
    return AnthropicLlm(settings, db, clock, client=client)


async def test_anthropic_llm_sends_metadata_and_masks(db, settings, clock):
    client = RecordingClient(tool_input=_ok())
    await _llm(settings, db, clock, client).classify(7, "карта 4276 1234 5678 9012", TODAY, [])
    kw = client.kwargs
    assert kw["metadata"] == {"user_id": "personal-hub:7"}
    assert "4276" not in str(kw["messages"])
    assert kw["model"] == settings.model_fast
    assert kw["max_tokens"] == 512
    assert kw["tool_choice"] == {"type": "tool", "name": "save_entry"}
    assert "понедельник" in kw["system"]
    assert "2026-11-02" in kw["system"]


async def test_classify_parses_fields_and_records_usage(db, settings, clock):
    client = RecordingClient(
        tool_input=_ok(
            kind="task", title="Оплатить", due_date="2026-11-10", contour="работа", section="Дом"
        )
    )
    r = await _llm(settings, db, clock, client).classify(7, "оплатить", TODAY, ["Дом"])
    assert (r.kind, r.title, r.due_date, r.contour, r.section) == (
        "task",
        "Оплатить",
        date(2026, 11, 10),
        "работа",
        "Дом",
    )
    row = await (
        await db.execute(
            "SELECT tokens_in, tokens_out FROM llm_usage WHERE user_id = 7 AND day = '2026-11-02'"
        )
    ).fetchone()
    assert (row["tokens_in"], row["tokens_out"]) == (10, 5)


async def test_classify_normalizes_unknown_section_contour_and_long_title(db, settings, clock):
    client = RecordingClient(tool_input=_ok(title="я" * 200, contour="чужой", section="Выдуманный"))
    r = await _llm(settings, db, clock, client).classify(7, "t", TODAY, ["Дом"])
    assert r.contour is None
    assert r.section is None
    assert len(r.title) == 120


@pytest.mark.parametrize("bad", [{"kind": "spam"}, {"due_date": "завтра"}, {"title": ""}])
async def test_classify_rejects_invalid(db, settings, clock, bad):
    client = RecordingClient(tool_input=_ok(**bad))
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, client).classify(7, "t", TODAY, [])


async def test_classify_rejects_missing_tool_use(db, settings, clock):
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, RecordingClient()).classify(7, "t", TODAY, [])


def _subs(subs):
    return RecordingClient(text=json.dumps({"subtasks": subs}))


async def test_decompose_ok_and_masks(db, settings, clock):
    client = _subs(["a", "b", "c"])
    r = await _llm(settings, db, clock, client).decompose(7, "Ремонт", "паспорт 03 15 123456")
    assert r == ["a", "b", "c"]
    kw = client.kwargs
    assert kw["model"] == settings.model_smart
    assert kw["max_tokens"] == 2048
    assert "tool_choice" not in kw
    assert "tools" not in kw
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "subtasks" in kw["output_config"]["format"]["schema"]["properties"]
    assert "123456" not in str(kw["messages"])


@pytest.mark.parametrize("subs", [["a", "b"], list("abcdefgh"), ["a", "b", 3], "abc"])
async def test_decompose_rejects_bad_count_or_type(db, settings, clock, subs):
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, _subs(subs)).decompose(7, "t", "b")


@pytest.mark.parametrize("text", ["не json", "[1, 2, 3]", '{"x": 1}'])
async def test_decompose_rejects_invalid_json(db, settings, clock, text):
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, RecordingClient(text=text)).decompose(7, "t", "b")


@pytest.mark.parametrize("reason", ["refusal", "max_tokens"])
async def test_decompose_rejects_bad_stop_reason(db, settings, clock, reason):
    client = RecordingClient(text='{"subtasks": ["a", "b", "c"]}', stop_reason=reason)
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, client).decompose(7, "t", "b")


async def test_decompose_without_text_block_is_error(db, settings, clock):
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, RecordingClient()).decompose(7, "t", "b")


async def test_brief_intro(db, settings, clock):
    client = RecordingClient(text="  Доброе утро!  ", thinking="думаю")
    r = await _llm(settings, db, clock, client).brief_intro(7, "карта 4276123456789012")
    assert r == "Доброе утро!"
    assert client.kwargs["max_tokens"] == 1024
    assert client.kwargs["output_config"] == {"effort": "low"}
    assert "tool_choice" not in client.kwargs
    assert client.kwargs["model"] == settings.model_smart
    assert "4276" not in str(client.kwargs["messages"])


async def test_brief_intro_without_text_is_error(db, settings, clock):
    with pytest.raises(LlmError):
        await _llm(settings, db, clock, RecordingClient()).brief_intro(7, "f")


async def test_budget_exceeded_propagates_and_no_call(db, settings, clock):
    await record_usage(db, 7, TODAY, 2_000_000, 0)
    client = RecordingClient(tool_input=_ok())
    with pytest.raises(BudgetExceeded):
        await _llm(settings, db, clock, client).classify(7, "t", TODAY, [])
    assert client.kwargs == {}


async def test_sdk_error_becomes_llm_error_without_usage(db, settings, clock):
    class Boom:
        class messages:
            @staticmethod
            async def create(**kw):
                raise anthropic.APITimeoutError(request=httpx2.Request("POST", "http://x"))

    with pytest.raises(LlmError) as ei:
        await _llm(settings, db, clock, Boom()).classify(7, "t", TODAY, [])
    assert isinstance(ei.value.__cause__, anthropic.APIError)
    assert await (await db.execute("SELECT 1 FROM llm_usage")).fetchone() is None


async def test_aclose_closes_only_own_client(db, clock, settings):
    class Client:
        closed = False

        async def close(self):
            self.closed = True

    given = Client()
    await AnthropicLlm(settings, db, clock, client=given).aclose()
    assert given.closed is False
    own = AnthropicLlm(settings, db, clock)
    await own.aclose()


async def test_default_client_has_timeout_and_one_retry(db, settings, clock):
    llm = AnthropicLlm(settings, db, clock)
    try:
        assert llm._client.timeout == 60
        assert llm._client.max_retries == 1
    finally:
        await llm.aclose()


async def test_brief_intro_masks_calendar_titles(db, settings, clock):
    client = RecordingClient(text="Привет")
    facts = "Календарь\n10:00 Оплатить картой 4276 1234 5678 9012"
    assert await _llm(settings, db, clock, client).brief_intro(7, facts) == "Привет"
    assert "4276" not in str(client.kwargs["messages"])
