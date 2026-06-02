"""`cruxial.run` — one managed turn, exercised against fake provider clients.

No network. Fake clients return scripted, provider-shaped responses so we can
assert the full turn: validate → execute → repair → append results → return.
"""

from __future__ import annotations

import json

import pytest

from cruxial import GuardConfig, ProviderUnsupported, run

NULL = GuardConfig(sinks=("null",))

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "to": {"type": "string", "format": "email"},
        "subject": {"type": "string", "maxLength": 200},
        "body": {"type": "string"},
    },
    "required": ["to", "subject", "body"],
}
OPENAI_TOOLS = [{"type": "function", "function": {"name": "send_email", "parameters": SCHEMA}}]
ANTHROPIC_TOOLS = [{"name": "send_email", "input_schema": SCHEMA}]


def _executors():
    sent = []

    def send_email(to, subject, body):
        sent.append(to)
        return {"sent": to}

    return {"send_email": send_email}, sent


# ─── OpenAI-shaped fakes ────────────────────────────────────────────────────


class _Fn:
    def __init__(self, name, arguments): self.name = name; self.arguments = arguments


class _TC:
    def __init__(self, id, name, arguments): self.id = id; self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None): self.content = content; self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg): self.choices = [type("C", (), {"message": msg})()]


class _Completions:
    def __init__(self, outer): self._o = outer
    def create(self, **kwargs):
        self._o.calls.append(kwargs)
        return self._o.scripted.pop(0)


class _Chat:
    def __init__(self, outer): self.completions = _Completions(outer)


class FakeOpenAI:
    def __init__(self, scripted): self.scripted = list(scripted); self.calls = []; self.chat = _Chat(self)


def _oai_toolcall(args: dict, name="send_email", id="call_1"):
    return _Resp(_Msg(content=None, tool_calls=[_TC(id, name, json.dumps(args))]))


def _oai_text(text):
    return _Resp(_Msg(content=text, tool_calls=None))


# ─── tests: OpenAI ──────────────────────────────────────────────────────────


def test_openai_happy_path_executes_and_appends_result():
    ex, sent = _executors()
    client = FakeOpenAI([_oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"})])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email a@b.com"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)

    assert r.finished is False
    assert r.tool_calls[0]["ok"] is True
    assert sent == ["a@b.com"]                       # the executor actually ran
    assert r.messages[-1]["role"] == "tool"           # tool result appended
    assert r.stats["passed"] == 1


def test_openai_no_tool_calls_is_finished():
    ex, _ = _executors()
    client = FakeOpenAI([_oai_text("All done — email sent.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert r.finished is True
    assert r.text == "All done — email sent."
    assert r.tool_calls == []


def test_openai_intercept_then_repair():
    ex, sent = _executors()
    # 1st response: bad args (to is an int → type_mismatch).
    # 2nd response (repair round-trip): corrected args.
    client = FakeOpenAI([
        _oai_toolcall({"to": 12345, "subject": "hi", "body": "yo"}),
        _oai_toolcall({"to": "fixed@b.com", "subject": "hi", "body": "yo"}),
    ])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email someone"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)

    assert r.tool_calls[0]["ok"] is True
    assert r.tool_calls[0]["repaired"] is True
    assert sent == ["fixed@b.com"]                    # ran with the corrected args
    assert r.stats["repaired"] == 1
    assert len(client.calls) == 2                     # original + one repair round-trip


def _oai_multi(calls):
    tcs = [_TC(cid, "send_email", json.dumps(a)) for cid, a in calls]
    return _Resp(_Msg(content=None, tool_calls=tcs))


def test_openai_parallel_calls_one_bad_gets_repaired():
    ex, sent = _executors()
    client = FakeOpenAI([
        _oai_multi([("c1", {"to": "a@b.com", "subject": "hi", "body": "yo"}),
                    ("c2", {"to": 999, "subject": "hi", "body": "yo"})]),
        _oai_toolcall({"to": "fixed@b.com", "subject": "hi", "body": "yo"}, id="c2b"),
    ])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)

    assert len(r.tool_calls) == 2
    assert all(tc["ok"] for tc in r.tool_calls)
    assert any(tc["repaired"] for tc in r.tool_calls)
    assert set(sent) == {"a@b.com", "fixed@b.com"}
    # one tool result per original call_id (provider requirement)
    assert len([m for m in r.messages if m.get("role") == "tool"]) == 2


def test_repair_rewrites_persisted_assistant_args_for_coherence():
    ex, _ = _executors()
    client = FakeOpenAI([
        _oai_toolcall({"to": 12345, "subject": "hi", "body": "yo"}, id="cc"),
        _oai_toolcall({"to": "fixed@b.com", "subject": "hi", "body": "yo"}, id="cc2"),
    ])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)

    asst = [m for m in r.messages if m.get("role") == "assistant"][0]
    args = json.loads(asst["tool_calls"][0]["function"]["arguments"])
    assert args["to"] == "fixed@b.com"   # rewritten to what actually ran, not 12345


def test_openai_intercept_no_repair_surfaces_failure():
    ex, sent = _executors()
    client = FakeOpenAI([_oai_toolcall({"to": 12345, "subject": "hi", "body": "yo"})])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, repair=False, config=NULL)

    assert r.tool_calls[0]["ok"] is False
    assert r.tool_calls[0]["failure"].category == "type_mismatch"
    assert sent == []                                 # executor never ran
    assert len(client.calls) == 1                     # no repair round-trip


# ─── tests: provider guards ─────────────────────────────────────────────────


def test_streaming_is_rejected_clearly():
    ex, _ = _executors()
    client = FakeOpenAI([_oai_text("hi")])
    with pytest.raises(ProviderUnsupported):
        run(client, model="gpt-4o", messages=[], tools=OPENAI_TOOLS, executors=ex,
            config=NULL, stream=True)


def test_unknown_client_raises_provider_unsupported():
    ex, _ = _executors()
    with pytest.raises(ProviderUnsupported):
        run(object(), model="x", messages=[], tools=OPENAI_TOOLS, executors=ex, config=NULL)


# ─── tests: LiteLLM callable (OpenAI-shaped via shim) ───────────────────────


def test_litellm_callable_client():
    ex, sent = _executors()
    scripted = [_oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"})]

    def completion(**kwargs):
        return scripted.pop(0)

    r = run(completion, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert r.tool_calls[0]["ok"] is True
    assert sent == ["a@b.com"]


# ─── Anthropic-shaped fakes ─────────────────────────────────────────────────


class _Block:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _AResp:
    def __init__(self, content): self.content = content


class _AMessages:
    def __init__(self, outer): self._o = outer
    def create(self, **kwargs):
        self._o.calls.append(kwargs)
        return self._o.scripted.pop(0)


class FakeAnthropic:
    def __init__(self, scripted): self.scripted = list(scripted); self.calls = []; self.messages = _AMessages(self)


def test_anthropic_happy_path():
    ex, sent = _executors()
    resp = _AResp([
        _Block(type="text", text="Sending now."),
        _Block(type="tool_use", id="toolu_1", name="send_email",
               input={"to": "a@b.com", "subject": "hi", "body": "yo"}),
    ])
    client = FakeAnthropic([resp])
    r = run(client, model="claude-x", messages=[{"role": "user", "content": "email a@b.com"}],
            tools=ANTHROPIC_TOOLS, executors=ex, config=NULL)

    assert r.tool_calls[0]["ok"] is True
    assert sent == ["a@b.com"]
    # Anthropic tool results go in a single user turn of tool_result blocks.
    last = r.messages[-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "toolu_1"


def test_anthropic_max_tokens_defaulted():
    ex, _ = _executors()
    client = FakeAnthropic([_AResp([_Block(type="text", text="done")])])
    run(client, model="claude-x", messages=[], tools=ANTHROPIC_TOOLS, executors=ex, config=NULL)
    assert client.calls[0]["max_tokens"] == 1024       # required by Anthropic, set for you
