"""Async path: guard().aexecute()/aexecute_repaired() and cruxial.arun().

The sync execute() must refuse an async executor loudly (not silently no-op);
aexecute() awaits it. arun() drives the full managed turn against an async
client — execute, repair, and bypass detection — all awaited.
"""
from __future__ import annotations

import json

import pytest

from cruxial import GuardConfig, NoopCruxial, arun, guard
from cruxial.errors import CruxialError, ProviderUnsupported

_NULL = GuardConfig(sinks=("null",))
S = {"t": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}


def _g(executor):
    return guard(schemas=S, executors={"t": executor}, config=_NULL)


# ─── aexecute / sync-guard ───────────────────────────────────────────────────

async def test_aexecute_awaits_async_executor():
    async def ex(x):
        return f"ran:{x}"
    r = await _g(ex).aexecute("t", {"x": "hi"})
    assert r.ok and r.value == "ran:hi"


async def test_aexecute_accepts_sync_executor():
    def ex(x):
        return f"sync:{x}"
    r = await _g(ex).aexecute("t", {"x": "hi"})
    assert r.ok and r.value == "sync:hi"


def test_sync_execute_raises_clearly_on_async_executor():
    async def ex(x):
        return x
    with pytest.raises(CruxialError, match="async"):
        _g(ex).execute("t", {"x": "hi"})


async def test_aexecute_validation_failure_does_not_run():
    ran = []
    async def ex(x):
        ran.append(1)
        return x
    r = await _g(ex).aexecute("t", {})  # missing required 'x'
    assert not r.ok and r.failure.category == "missing_required"
    assert ran == []  # never executed


async def test_aexecute_executor_error_fails_open():
    async def ex(x):
        raise ValueError("boom")
    r = await _g(ex).aexecute("t", {"x": "hi"})
    assert not r.ok and r.failure.category == "executor_error"
    assert isinstance(r.error, ValueError)


async def test_aexecute_repaired_marks_corrected():
    async def ex(x):
        return x
    r = await _g(ex).aexecute_repaired("t", {"x": "fixed"})
    assert r.ok and r.repaired


async def test_noop_aexecute_is_safe():
    r = await NoopCruxial().aexecute("t", {"x": "hi"})
    assert not r.ok  # no-op mode can't execute, but doesn't blow up


# ─── fake AsyncOpenAI client ─────────────────────────────────────────────────

class _F:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, id, name, arguments):
        self.id, self.type, self.function = id, "function", _F(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


class _AsyncCompletions:
    def __init__(self, scripted):
        self._s, self.n = list(scripted), 0

    async def create(self, **kw):
        r = self._s[self.n] if self.n < len(self._s) else self._s[-1]
        self.n += 1
        return r


class FakeAsyncOpenAI:
    """Minimal AsyncOpenAI shape: awaitable client.chat.completions.create."""
    def __init__(self, scripted):
        self.chat = type("Chat", (), {"completions": _AsyncCompletions(scripted)})()


def _tc_resp(name, args: dict, tc_id="c1", content=None):
    return _Resp(_Msg(content=content, tool_calls=[_TC(tc_id, name, json.dumps(args))]))


def _text_resp(text):
    return _Resp(_Msg(content=text, tool_calls=None))


SCHEMA = {"type": "object",
          "properties": {"to": {"type": "string"}, "subject": {"type": "string"},
                         "body": {"type": "string"}},
          "required": ["to", "subject", "body"]}
TOOLS = [{"type": "function", "function": {"name": "send_email", "parameters": SCHEMA}}]
GOOD = {"to": "a@b.com", "subject": "Hi", "body": "yo"}


# ─── arun e2e ────────────────────────────────────────────────────────────────

async def test_arun_executes_valid_async_call():
    sent = []
    async def send_email(to, subject, body):
        sent.append(to)
        return "sent"
    client = FakeAsyncOpenAI([_tc_resp("send_email", GOOD)])
    res = await arun(client, model="gpt-4o", messages=[{"role": "user", "content": "email them"}],
                     tools=TOOLS, executors={"send_email": send_email}, bypass="off", config=_NULL)
    assert not res.finished
    assert res.tool_calls[0]["ok"] and res.tool_calls[0]["value"] == "sent"
    assert sent == ["a@b.com"]  # the async executor actually ran (awaited)


async def test_arun_repairs_invalid_call():
    async def send_email(to, subject, body):
        return "sent"
    bad = _tc_resp("send_email", {"subject": "Hi", "body": "yo"})        # missing 'to'
    good = _tc_resp("send_email", GOOD, tc_id="c2")
    client = FakeAsyncOpenAI([bad, good])
    res = await arun(client, model="gpt-4o", messages=[{"role": "user", "content": "email"}],
                     tools=TOOLS, executors={"send_email": send_email}, bypass="off", config=_NULL)
    assert res.tool_calls[0]["ok"] and res.tool_calls[0]["repaired"]
    assert client.chat.completions.n == 2  # original call + one repair round-trip


async def test_arun_bypass_records_unknown_deterministically():
    async def send_email(to, subject, body):
        return "sent"
    claim = _text_resp("I've sent the email to the team.")   # claims action, no call
    client = FakeAsyncOpenAI([claim])
    res = await arun(client, model="gpt-4o", messages=[{"role": "user", "content": "send the email"}],
                     tools=TOOLS, executors={"send_email": send_email}, config=_NULL)
    assert res.bypass is not None and res.bypass.tool == "send_email"
    assert res.state("send_email") == "unknown"   # surfaced, never re-prompted
    assert res.finished is True
    assert client.chat.completions.n == 1         # deterministic — zero extra model calls


async def test_arun_streaming_raises():
    client = FakeAsyncOpenAI([_text_resp("hi")])
    with pytest.raises(ProviderUnsupported):
        await arun(client, model="gpt-4o", messages=[], tools=TOOLS,
                   executors={}, config=_NULL, stream=True)
