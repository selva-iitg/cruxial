"""Adapters: extract_schemas + auto_repair (against fake clients)."""

from __future__ import annotations

import pytest

from cruxial.adapters.openai import auto_repair as openai_repair
from cruxial.adapters.openai import auto_repair_batch as openai_repair_batch
from cruxial.adapters.openai import extract_schemas as openai_extract
from cruxial.adapters.anthropic import auto_repair as anthropic_repair
from cruxial.adapters.anthropic import extract_schemas as anthropic_extract
from cruxial.errors import RepairExhausted
from cruxial.types import Failure


def test_openai_extract_schemas():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "send_email",
                "parameters": {"type": "object", "properties": {"to": {"type": "string"}}},
            },
        },
        {"type": "function", "function": {"name": "noop", "parameters": {"type": "object"}}},
        {"type": "not_a_function"},  # should be skipped
    ]
    out = openai_extract(tools)
    assert set(out) == {"send_email", "noop"}
    assert out["send_email"]["properties"]["to"]["type"] == "string"


def test_anthropic_extract_schemas():
    tools = [
        {
            "name": "send_email",
            "input_schema": {"type": "object", "properties": {"to": {"type": "string"}}},
        },
        {"name": "broken"},  # no input_schema — skip
    ]
    out = anthropic_extract(tools)
    assert set(out) == {"send_email"}


# ─── auto_repair with fake clients ───────────────────────────────────────


class _FakeOpenAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _FakeOpenAIToolCall:
    def __init__(self, name, args_json):
        class _Fn:
            def __init__(self, n, a):
                self.name = n
                self.arguments = a
        self.function = _Fn(name, args_json)


class _FakeOpenAIChoice:
    def __init__(self, message):
        self.message = message


class _FakeOpenAIResponse:
    def __init__(self, message):
        self.choices = [_FakeOpenAIChoice(message)]


class _FakeOpenAIClient:
    def __init__(self, response_args_json):
        self._response_args_json = response_args_json
        self._calls = []

        class _ChatCompletions:
            def __init__(_self):
                _self._parent = self
            def create(_self, **kwargs):
                _self._parent._calls.append(kwargs)
                return _FakeOpenAIResponse(
                    _FakeOpenAIMessage(tool_calls=[
                        _FakeOpenAIToolCall("send_email", _self._parent._response_args_json)
                    ])
                )

        class _Chat:
            def __init__(_self):
                _self.completions = _ChatCompletions()

        self.chat = _Chat()


def test_openai_auto_repair_with_tool_call_id_uses_tool_message():
    """When tool_call_id is provided, we use the canonical tool-result format
    and the id must match exactly (OpenAI API requirement)."""
    client = _FakeOpenAIClient(
        response_args_json='{"to": "a@b.com", "subject": "hi", "body": "x"}'
    )
    failure = Failure(category="type_mismatch", tool="send_email", message="bad type")
    args = openai_repair(
        client,
        model="gpt-4o",
        messages=[{"role": "user", "content": "send a recap"}],
        tools=[{"type": "function", "function": {"name": "send_email", "parameters": {}}}],
        failure=failure,
        failed_args={"to": 42, "subject": "hi", "body": "x"},
        repair_prompt="please fix the args",
        tool_call_id="call_abc123",
    )
    assert args == {"to": "a@b.com", "subject": "hi", "body": "x"}
    sent = client._calls[0]["messages"]
    # Last message is the tool-result turn carrying the real id.
    assert sent[-1]["role"] == "tool"
    assert sent[-1]["tool_call_id"] == "call_abc123"
    assert "please fix the args" in sent[-1]["content"]


def test_openai_auto_repair_without_tool_call_id_falls_back_to_user_message():
    """Without a tool_call_id we fall back to user-message format so the
    API can't reject us for a mismatched id."""
    client = _FakeOpenAIClient(
        response_args_json='{"to": "a@b.com", "subject": "hi", "body": "x"}'
    )
    failure = Failure(category="type_mismatch", tool="send_email", message="bad type")
    args = openai_repair(
        client,
        model="gpt-4o",
        messages=[{"role": "user", "content": "send a recap"}],
        tools=[{"type": "function", "function": {"name": "send_email", "parameters": {}}}],
        failure=failure,
        failed_args={"to": 42, "subject": "hi", "body": "x"},
        repair_prompt="please fix the args",
        # tool_call_id intentionally omitted
    )
    assert args == {"to": "a@b.com", "subject": "hi", "body": "x"}
    sent = client._calls[0]["messages"]
    # Last message is a user-role fallback. No synthetic tool_call_id sneaks in.
    assert sent[-1]["role"] == "user"
    for m in sent:
        assert "tool_call_id" not in m or m.get("role") != "tool" or m["tool_call_id"]


def test_openai_auto_repair_exhausts_when_model_returns_no_tool_call():
    # Model returns junk that doesn't match any tool
    bad_resp = _FakeOpenAIResponse(_FakeOpenAIMessage(tool_calls=[]))

    class _BadClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return bad_resp

    failure = Failure(category="type_mismatch", tool="send_email", message="bad type")
    with pytest.raises(RepairExhausted):
        openai_repair(
            _BadClient,
            model="gpt-4o",
            messages=[],
            tools=[],
            failure=failure,
            failed_args={},
            repair_prompt="fix it",
            max_attempts=2,
        )


# ─── auto_repair_batch ────────────────────────────────────────────────


class _BatchFakeOpenAIClient:
    """Returns a configurable response with N tool_calls of specified (name, args)."""

    def __init__(self, response_tool_calls: list[tuple[str, str]]):
        self._calls: list[dict] = []
        self._response = response_tool_calls

        class _ChatCompletions:
            def __init__(_self):
                _self._parent = self

            def create(_self, **kwargs):
                _self._parent._calls.append(kwargs)
                tool_calls = [
                    _FakeOpenAIToolCall(name, args_json)
                    for (name, args_json) in _self._parent._response
                ]
                return _FakeOpenAIResponse(_FakeOpenAIMessage(tool_calls=tool_calls))

        class _Chat:
            def __init__(_self):
                _self.completions = _ChatCompletions()

        self.chat = _Chat()


def test_batch_repair_returns_empty_when_no_failures():
    """If all outcomes are ok, no API call should be made and result is {}."""
    client = _BatchFakeOpenAIClient(response_tool_calls=[])
    result = openai_repair_batch(
        client,
        model="gpt-4o",
        messages=[],
        tools=[],
        tool_call_outcomes=[
            {"tool_call_id": "a", "name": "x", "args": {}, "ok": True, "value": "done"},
            {"tool_call_id": "b", "name": "y", "args": {}, "ok": True, "value": "done"},
        ],
    )
    assert result == {}
    assert client._calls == [], "no API call should fire when nothing failed"


def test_batch_repair_handles_single_failure_among_successes():
    client = _BatchFakeOpenAIClient(
        response_tool_calls=[("send_email", '{"to": "a@b.com", "subject": "hi", "body": "x"}')]
    )
    failure = Failure(category="type_mismatch", tool="send_email", message="bad")
    result = openai_repair_batch(
        client,
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_ok", "type": "function", "function": {"name": "log_audit", "arguments": "{}"}},
                {"id": "call_bad", "type": "function", "function": {"name": "send_email", "arguments": "{}"}},
            ]},
        ],
        tools=[],
        tool_call_outcomes=[
            {"tool_call_id": "call_ok", "name": "log_audit", "args": {}, "ok": True, "value": "logged"},
            {"tool_call_id": "call_bad", "name": "send_email", "args": {"to": 42}, "ok": False,
             "failure": failure, "repair_prompt": "fix the to field"},
        ],
    )
    assert result == {"call_bad": {"to": "a@b.com", "subject": "hi", "body": "x"}}

    # The repair turn MUST include tool_result for BOTH prior calls (otherwise OpenAI 400s).
    sent_msgs = client._calls[0]["messages"]
    tool_msgs = [m for m in sent_msgs if m.get("role") == "tool"]
    ids = sorted(m["tool_call_id"] for m in tool_msgs)
    assert ids == ["call_bad", "call_ok"], "every prior tool_call must get a tool_result"


def test_batch_repair_distributes_n_corrections_to_n_originals_of_same_name():
    """The deploy_application case: 4 calls with the same name, all failed,
    all repaired by the model emitting 4 new ones. Each corrected call must
    map back to its original id in emission order."""
    client = _BatchFakeOpenAIClient(response_tool_calls=[
        ("deploy", '{"version": "1.0.0", "env": "dev"}'),
        ("deploy", '{"version": "1.0.0", "env": "staging"}'),
        ("deploy", '{"version": "1.0.0", "env": "production"}'),
        ("deploy", '{"version": "1.0.0", "env": "canary"}'),
    ])
    fail = Failure(category="format_violation", tool="deploy", message="version must be semver")
    outcomes = [
        {"tool_call_id": f"id_{i}", "name": "deploy", "args": {"version": "1.0"},
         "ok": False, "failure": fail, "repair_prompt": "use full semver"}
        for i in range(4)
    ]
    assistant_tcs = [
        {"id": f"id_{i}", "type": "function",
         "function": {"name": "deploy", "arguments": '{"version": "1.0"}'}}
        for i in range(4)
    ]
    result = openai_repair_batch(
        client,
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "deploy 1.0 to all envs"},
            {"role": "assistant", "content": None, "tool_calls": assistant_tcs},
        ],
        tools=[],
        tool_call_outcomes=outcomes,
    )
    # All 4 originals matched to corrections in order
    assert set(result.keys()) == {"id_0", "id_1", "id_2", "id_3"}
    assert result["id_0"]["env"] == "dev"
    assert result["id_3"]["env"] == "canary"


def test_batch_repair_raises_when_model_emits_nothing_useful():
    """Model returns zero tool_calls → RepairExhausted."""
    client = _BatchFakeOpenAIClient(response_tool_calls=[])
    failure = Failure(category="enum_violation", tool="send_email", message="bad enum")
    with pytest.raises(RepairExhausted):
        openai_repair_batch(
            client,
            model="gpt-4o",
            messages=[],
            tools=[],
            tool_call_outcomes=[
                {"tool_call_id": "x", "name": "send_email", "args": {},
                 "ok": False, "failure": failure, "repair_prompt": "fix it"},
            ],
            max_attempts=1,
        )


def test_batch_repair_omits_unfixed_calls_from_result():
    """If the model only re-emits some of the failed calls, the others
    are omitted from the result (caller decides how to treat them)."""
    client = _BatchFakeOpenAIClient(response_tool_calls=[
        # Only repair send_email; ignore the failed create_event entirely
        ("send_email", '{"to": "a@b.com", "subject": "hi", "body": "x"}')
    ])
    fail = Failure(category="format_violation", tool="x", message="bad")
    outcomes = [
        {"tool_call_id": "id_email", "name": "send_email", "args": {}, "ok": False,
         "failure": fail, "repair_prompt": "fix"},
        {"tool_call_id": "id_event", "name": "create_event", "args": {}, "ok": False,
         "failure": fail, "repair_prompt": "fix"},
    ]
    result = openai_repair_batch(
        client,
        model="gpt-4o",
        messages=[{"role": "assistant", "content": None, "tool_calls": [
            {"id": "id_email", "type": "function", "function": {"name": "send_email", "arguments": "{}"}},
            {"id": "id_event", "type": "function", "function": {"name": "create_event", "arguments": "{}"}},
        ]}],
        tools=[],
        tool_call_outcomes=outcomes,
    )
    assert "id_email" in result
    assert "id_event" not in result


# Anthropic counterpart
class _FakeAnthropicBlock:
    def __init__(self, type_, name=None, input_=None):
        self.type = type_
        self.name = name
        self.input = input_


class _FakeAnthropicResponse:
    def __init__(self, blocks):
        self.content = blocks


class _FakeAnthropicClient:
    def __init__(self, blocks):
        self._blocks = blocks
        self._calls = []

        class _Messages:
            def __init__(_self):
                _self._parent = self
            def create(_self, **kwargs):
                _self._parent._calls.append(kwargs)
                return _FakeAnthropicResponse(_self._parent._blocks)

        self.messages = _Messages()


def test_anthropic_auto_repair_returns_corrected_args():
    client = _FakeAnthropicClient([
        _FakeAnthropicBlock(
            "tool_use",
            name="send_email",
            input_={"to": "a@b.com", "subject": "hi", "body": "x"},
        )
    ])
    failure = Failure(category="format_violation", tool="send_email", message="bad email")
    args = anthropic_repair(
        client,
        model="claude-3-5-sonnet-latest",
        messages=[{"role": "user", "content": "send a recap"}],
        tools=[{"name": "send_email", "input_schema": {}}],
        failure=failure,
        failed_args={"to": "bad", "subject": "hi", "body": "x"},
        repair_prompt="please fix",
    )
    assert args == {"to": "a@b.com", "subject": "hi", "body": "x"}
    sent = client._calls[0]["messages"]
    assert sent[-1]["content"][0]["type"] == "tool_result"
    assert sent[-1]["content"][0]["is_error"] is True
