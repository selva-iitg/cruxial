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


# ─── tests: tool_bypass (claim-without-call) ────────────────────────────────


def test_bypass_recover_corrects():
    # bypass="recover" = deterministic detect + the opt-in re-prompt remediation
    # (the old "on" behaviour, now opt-in).
    ex, sent = _executors()
    client = FakeOpenAI([
        _oai_text("I've sent the email to a@b.com."),                  # claim, no call
        _oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"}),  # re-prompt → re-emits
    ])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email a@b.com"}],
            tools=OPENAI_TOOLS, executors=ex, bypass="recover", config=NULL)
    assert r.bypass is not None and r.bypass.tool == "send_email"
    assert r.finished is False           # the corrected call means the turn isn't done
    assert sent == ["a@b.com"]            # the tool actually ran after correction
    assert r.stats["bypass"] == 1
    assert len(client.calls) == 2         # original + one neutral re-prompt


def test_bypass_default_on_records_unknown_without_reprompt():
    # THE v0.5 FLIP: default "on" detects deterministically and records the
    # action as UNKNOWN — no re-prompt (a confident model just re-affirms the
    # false claim). The receipt's absence is the oracle.
    ex, sent = _executors()
    client = FakeOpenAI([_oai_text("I've sent the email.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert r.bypass is not None and r.bypass.tool == "send_email"
    assert r.state("send_email") == "unknown"   # surfaced, not trusted
    assert r.finished is True
    assert sent == []                            # NO fabricated action
    assert len(client.calls) == 1                # deterministic — zero extra model calls


def test_bypass_off_skips_the_reprompt():
    ex, sent = _executors()
    client = FakeOpenAI([_oai_text("I've sent the email.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, bypass="off", config=NULL)
    assert r.bypass is None and r.finished is True
    assert len(client.calls) == 1         # no extra call when disabled


def test_benign_final_reply_costs_no_extra_call():
    ex, _ = _executors()
    client = FakeOpenAI([_oai_text("Here's a summary of your open tasks.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert r.bypass is None and r.finished is True
    assert len(client.calls) == 1         # not suspect → zero overhead


def test_strict_bypass_confirmed_judges_then_forces_emit():
    ex, sent = _executors()
    client = FakeOpenAI([
        _oai_text("I've sent the email to a@b.com."),                    # claim, no call
        _oai_text("NEEDED"),                                             # no-tool judgment
        _oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"}),  # forced emit
    ])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email a@b.com"}],
            tools=OPENAI_TOOLS, executors=ex, bypass="strict", config=NULL)
    assert r.bypass is not None
    assert sent == ["a@b.com"]
    assert len(client.calls) == 3        # turn + judgment + forced emit


def test_strict_judged_done_still_records_unknown():
    # Even in strict mode, detection is deterministic: a claimed completion with
    # no call anywhere in the trace is recorded UNKNOWN. The judge's "DONE" only
    # governs RECOVERY (no forced emit) — it no longer suppresses the finding.
    ex, sent = _executors()
    client = FakeOpenAI([
        _oai_text("I already sent the email earlier today."),            # claim, no call in trace
        _oai_text("DONE"),                                               # judgment → already done
    ])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "status?"}],
            tools=OPENAI_TOOLS, executors=ex, bypass="strict", config=NULL)
    assert r.bypass is not None                  # detected + recorded
    assert r.state("send_email") == "unknown"
    assert sent == []                    # judge said DONE → no forced emit, no duplicate
    assert len(client.calls) == 2        # turn + judgment, NO forced emit


def test_bypass_not_flagged_when_tool_was_called_earlier():
    ex, _ = _executors()
    client = FakeOpenAI([_oai_text("I've sent the email.")])
    prior = [
        {"role": "user", "content": "email a@b.com"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "send_email", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "{}"},
        {"role": "user", "content": "did it go?"},
    ]
    r = run(client, model="gpt-4o", messages=prior, tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert r.bypass is None and r.finished is True
    assert len(client.calls) == 1         # send_email was already called → not suspect


# ─── tests: v0.5 action layer through run() ─────────────────────────────────


def _email_exec(mid="m1"):
    sent = []

    def send_email(to, subject, body):
        sent.append(to)
        return {"message_id": mid}

    return {"send_email": send_email}, sent


def _action_guard(executors, *, adapter=True, hook=None):
    from cruxial.actions import ActionRegistry
    from cruxial.core import guard as _guard_ctor
    from cruxial.receipts import ReceiptRegistry, id_field
    from cruxial.telemetry import NullSink

    ar = ActionRegistry()
    ar.mark_action("send_email")
    rr = ReceiptRegistry()
    if adapter:
        rr.register("send_email", id_field("message_id", kind="email"))
    if hook:
        ar.register_verify("send_email", hook)
    return _guard_ctor(
        {"send_email": {"type": "object"}}, executors, sink=NullSink(),
        receipt_registry=rr, action_registry=ar,
    )


def test_absence_populates_operations_and_render():
    ex, sent = _executors()
    client = FakeOpenAI([_oai_text("I've sent the email.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert len(r.operations) == 1 and r.operations[0].state == "unknown"
    assert "unknown" in r.render() and "done" not in r.render()
    # Said-vs-Did: structured (non-PII) claim by default
    assert r.operations[0].claim == "claimed a completed 'send' action"


def test_absence_claim_verbatim_with_capture_args():
    ex, _ = _executors()
    client = FakeOpenAI([_oai_text("Done! I've sent the email to a@b.com.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex,
            config=GuardConfig(sinks=("null",), capture_args=True))
    assert "Done! I've sent the email to a@b.com." in r.operations[0].claim


def test_run_action_posts_and_renders():
    ex, sent = _email_exec()
    g = _action_guard(ex)
    client = FakeOpenAI([_oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"})])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email"}],
            tools=OPENAI_TOOLS, executors=ex, guard=g, config=NULL)
    assert sent == ["a@b.com"]
    assert r.state("send_email") == "posted"
    assert len(r.operations) == 1 and r.operations[0].receipt.id == "m1"
    assert "done" in r.render()


def test_run_action_without_adapter_is_unknown():
    ex, _ = _email_exec()  # returns a dict, but no adapter registered → default → no evidence
    g = _action_guard(ex, adapter=False)
    client = FakeOpenAI([_oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"})])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email"}],
            tools=OPENAI_TOOLS, executors=ex, guard=g, config=NULL)
    assert r.state("send_email") == "unknown"


def test_run_verify_halt_lands_in_halted():
    from cruxial.actions import HALT

    ex, _ = _email_exec()
    g = _action_guard(ex, hook=lambda a, r: HALT("recipient blocked"))
    client = FakeOpenAI([_oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"})])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email"}],
            tools=OPENAI_TOOLS, executors=ex, guard=g, config=NULL)
    assert r.state("send_email") == "needs_review"
    assert len(r.halted) == 1 and r.halted[0].tool == "send_email"
    assert "needs review" in r.render()


def test_tool_result_carries_receipt_to_model():
    ex, _ = _email_exec()
    g = _action_guard(ex)
    client = FakeOpenAI([_oai_toolcall({"to": "a@b.com", "subject": "hi", "body": "yo"})])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "email"}],
            tools=OPENAI_TOOLS, executors=ex, guard=g, config=NULL)
    tool_msg = [m for m in r.messages if m.get("role") == "tool"][0]
    content = json.loads(tool_msg["content"])
    assert content["status"] == "posted" and content["receipt"]["id"] == "m1"


def test_render_falls_back_to_text_when_no_ops():
    ex, _ = _executors()
    client = FakeOpenAI([_oai_text("Here's your summary.")])
    r = run(client, model="gpt-4o", messages=[{"role": "user", "content": "x"}],
            tools=OPENAI_TOOLS, executors=ex, config=NULL)
    assert r.render() == "Here's your summary." and r.operations == []


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
