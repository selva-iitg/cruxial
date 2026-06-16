"""`cruxial.run` — one managed agent turn, no framework.

A single call that does the whole tool step: call the model, validate every
tool call against your schema, execute the valid ones, auto-repair the invalid
ones in one round-trip, append the results to your conversation, and return.

It is deliberately ONE turn, not a loop. You keep your loop:

    result = cruxial.run(client, model=m, messages=msgs, tools=tools, executors=ex)
    while not result.finished:
        result = cruxial.run(client, model=m, messages=result.messages,
                             tools=tools, executors=ex)

Design constraints (so it stays a drop-in, not a framework):
  - Single turn. Exactly one model call per run (+1 only if repair fires).
  - Non-streaming. Pass stream=True and it raises with a pointer.
  - Provider-agnostic via duck-typed strategies: OpenAI / Azure / Anthropic /
    LiteLLM. Your already-configured client is reused as-is — all per-provider
    config (endpoint, api_version, base_url, timeouts, network retries) comes
    along for free.
  - Schemas are derived from `tools` — you pass what you already pass the LLM.
  - Fail-open: if Cruxial's own machinery errors, your tool still runs.
  - Unknown client → ProviderUnsupported, never silent misbehaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from cruxial.bypass import BypassSuspicion, detect_bypass
from cruxial.core import Cruxial, GuardConfig, _absence_claim, guard as _guard
from cruxial.errors import ProviderUnsupported


_STATE_LABEL = {
    "posted": "done ✓", "sent": "sent ✓", "queued": "queued",
    "failed": "failed ✗", "needs_review": "needs review ⚠",
    "unknown": "unknown — not confirmed ✗", "pending": "pending",
}


def _render_state(state: str | None) -> str:
    return _STATE_LABEL.get(state, state or "unknown")


@dataclass
class RunResult:
    """The outcome of one managed turn.

    Attributes:
        messages:   Your conversation with this turn appended (assistant turn +
                    tool results, in the provider's format). Pass it straight
                    back into the next ``run()`` call.
        text:       The assistant's natural-language text this turn, if any.
        tool_calls: One entry per tool call this turn:
                    {name, args, ok, value, failure, repaired, state, op_id}.
        finished:   True when the model returned no tool calls — your cue to
                    stop the outer loop.
        bypass:     A BypassSuspicion when the model claimed an action in prose
                    but emitted no matching call this turn — DETECTED
                    deterministically (the receipt's absence is the oracle, not
                    a model re-prompt). The action is recorded as `unknown`. This
                    is the "your agent said it sent the email, it didn't" catch.
        operations: The ledger Operations resolved this turn (absence + each
                    call-fired action), carrying receipt-derived `state`.
        halted:     Operations whose verify hook returned HALT (state
                    `needs_review`) — surface to the caller; do not blind-retry.
        stats:      {passed, intercepted, repaired, bypass} counts for this turn.
    """

    messages: list[Any]
    text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    bypass: BypassSuspicion | None = None
    operations: list[Any] = field(default_factory=list)
    halted: list[Any] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def state(self, tool: str) -> str | None:
        """Latest receipt-derived state for `tool` this turn (or None)."""
        for op in reversed(self.operations):
            if getattr(op, "tool", None) == tool:
                return getattr(op, "state", None)
        return None

    def render(self) -> str:
        """A user-facing summary derived from RECEIPTS, never the model's prose.
        Never reports a bare "done" — an unconfirmed action reads as "unknown"."""
        if not self.operations:
            return self.text or ""
        return " · ".join(
            f"{op.tool}: {_render_state(getattr(op, 'state', None))}" for op in self.operations
        )


def run(
    client: Any,
    *,
    model: str,
    messages: list[Any],
    tools: list[dict[str, Any]],
    executors: Mapping[str, Callable[..., Any]],
    repair: bool = True,
    bypass: str = "on",          # "on" = detect + record `unknown`; "off" = skip

    side_effecting: list[str] | None = None,
    provider: str = "auto",
    guard: Cruxial | None = None,
    config: GuardConfig | None = None,
    **create_kwargs: Any,
) -> RunResult:
    """Run one managed agent turn. See module docstring for the full contract."""
    if create_kwargs.get("stream"):
        raise ProviderUnsupported(
            "cruxial.run() does not support streaming. Stream the call yourself "
            "and validate each tool call with guard().execute() in your handler."
        )

    client, strat = _resolve(client, provider)

    # Derive schemas from the tools you already pass the LLM, and build (or
    # reuse) a guard so telemetry flows to the same place as guard()/stats.
    cx = guard or _guard(
        schemas=strat.extract_schemas(tools),
        executors=dict(executors),
        config=config or GuardConfig(),
    )

    # 1. One model call.
    resp = strat.create(client, model, messages, tools, create_kwargs)

    # 2. Parse assistant text + tool calls.
    assistant_msg, calls, text = strat.parse(resp)

    new_messages = list(messages)
    new_messages.append(assistant_msg)

    # 3. No tool calls → candidate final reply. Before declaring done, check for
    #    tool_bypass: did the text CLAIM a completed action it never called? A
    #    local, zero-cost pre-filter flags it and we record it DETERMINISTICALLY
    #    as `unknown` — the receipt's absence is the oracle. We never re-prompt
    #    the model to confirm (a confident model just re-affirms the false claim);
    #    remediation is the caller's policy, applied to the `unknown` signal.
    bypass_finding: BypassSuspicion | None = None
    absence_ops: list[Any] = []
    if not calls:
        suspicion = None
        if bypass != "off":
            names, descs = _tool_meta(tools)
            suspicion = detect_bypass(
                text,
                tool_calls_this_turn=[],
                tool_names=names,
                called_tools=_called_tools(messages),
                side_effecting=side_effecting,
                descriptions=descs,
            )
        if suspicion is not None:
            bypass_finding = suspicion
            if hasattr(cx, "record_absence"):
                capture = getattr(getattr(cx, "config", None), "capture_args", False)
                op = cx.record_absence(
                    suspicion.tool, claim=_absence_claim(text, capture, suspicion.action))
                if op is not None:
                    absence_ops.append(op)

    if not calls:
        return RunResult(
            messages=new_messages, text=text, tool_calls=[], finished=True,
            bypass=bypass_finding, operations=absence_ops, halted=[],
            stats={"passed": 0, "intercepted": 0, "repaired": 0,
                   "bypass": 1 if bypass_finding else 0},
        )

    # 4. Validate + execute each call.
    outcomes: list[_Outcome] = []
    for c in calls:
        res = cx.execute(c["name"], c["args"])
        outcomes.append(_Outcome(
            id=c["id"], name=c["name"], args=c["args"],
            ok=res.ok, value=res.value if res.ok else None,
            failure=res.failure if not res.ok else None,
            state=res.state, receipt=res.receipt, op=res.operation,
            repair_prompt=(
                cx.build_repair_prompt(res.failure, c["args"])
                if (not res.ok and res.failure) else None
            ),
        ))

    # 5. Repair the failed ones in a single round-trip, then re-execute.
    failed = [o for o in outcomes if not o.ok]
    if failed and repair:
        try:
            corrected = strat.repair(client, model, new_messages, tools, outcomes, create_kwargs)
        except Exception:
            corrected = {}  # repair is best-effort; surface the failures as-is
        for o in failed:
            new_args = corrected.get(o.id)
            if new_args is None:
                continue
            retry = cx.execute_repaired(o.name, new_args)
            if retry.ok:
                o.ok, o.value, o.repaired, o.failure, o.args = True, retry.value, True, None, new_args
                o.state, o.receipt, o.op = retry.state, retry.receipt, retry.operation
                # Keep history coherent: the persisted assistant turn should
                # show the args that ACTUALLY ran, not the rejected ones.
                strat.apply_correction(assistant_msg, o.id, new_args)
            else:
                o.failure = retry.failure  # still bad after one shot

    # 6. Append tool results (provider format), carrying the final outcomes.
    new_messages += strat.tool_result_msgs(outcomes)

    operations = list(absence_ops) + [o.op for o in outcomes if o.op is not None]
    halted = [op for op in operations if getattr(op, "state", None) == "needs_review"]
    stats = {
        "passed": sum(1 for o in outcomes if o.ok and not o.repaired),
        "intercepted": sum(1 for o in outcomes if o.repaired or not o.ok),
        "repaired": sum(1 for o in outcomes if o.repaired),
        "bypass": 1 if bypass_finding else 0,
    }
    return RunResult(
        messages=new_messages,
        text=text,
        tool_calls=[o.public() for o in outcomes],
        finished=False,
        bypass=bypass_finding,
        operations=operations,
        halted=halted,
        stats=stats,
    )


async def arun(
    client: Any,
    *,
    model: str,
    messages: list[Any],
    tools: list[dict[str, Any]],
    executors: Mapping[str, Callable[..., Any]],
    repair: bool = True,
    bypass: str = "on",          # "on" = detect + record `unknown`; "off" = skip

    side_effecting: list[str] | None = None,
    provider: str = "auto",
    guard: Cruxial | None = None,
    config: GuardConfig | None = None,
    **create_kwargs: Any,
) -> RunResult:
    """Async twin of ``run()`` — for ``AsyncOpenAI`` / async Anthropic clients and
    async tool executors. Same contract; every model call and tool execution is
    awaited. See ``run()`` for the full semantics. Mirrors ``run()`` step-for-step.
    """
    if create_kwargs.get("stream"):
        raise ProviderUnsupported(
            "cruxial.arun() does not support streaming. Stream the call yourself "
            "and validate each tool call with guard().aexecute() in your handler."
        )

    client, strat = _resolve(client, provider)

    cx = guard or _guard(
        schemas=strat.extract_schemas(tools),
        executors=dict(executors),
        config=config or GuardConfig(),
    )

    resp = await strat.acreate(client, model, messages, tools, create_kwargs)
    assistant_msg, calls, text = strat.parse(resp)

    new_messages = list(messages)
    new_messages.append(assistant_msg)

    bypass_finding: BypassSuspicion | None = None
    absence_ops: list[Any] = []
    if not calls:
        suspicion = None
        if bypass != "off":
            names, descs = _tool_meta(tools)
            suspicion = detect_bypass(
                text, tool_calls_this_turn=[], tool_names=names,
                called_tools=_called_tools(messages),
                side_effecting=side_effecting, descriptions=descs,
            )
        if suspicion is not None:
            # DETERMINISTIC absence catch (receipt-absence oracle, not a re-prompt).
            bypass_finding = suspicion
            if hasattr(cx, "record_absence"):
                capture = getattr(getattr(cx, "config", None), "capture_args", False)
                op = cx.record_absence(
                    suspicion.tool, claim=_absence_claim(text, capture, suspicion.action))
                if op is not None:
                    absence_ops.append(op)

    if not calls:
        return RunResult(
            messages=new_messages, text=text, tool_calls=[], finished=True,
            bypass=bypass_finding, operations=absence_ops, halted=[],
            stats={"passed": 0, "intercepted": 0, "repaired": 0,
                   "bypass": 1 if bypass_finding else 0},
        )

    outcomes: list[_Outcome] = []
    for c in calls:
        res = await cx.aexecute(c["name"], c["args"])
        outcomes.append(_Outcome(
            id=c["id"], name=c["name"], args=c["args"],
            ok=res.ok, value=res.value if res.ok else None,
            failure=res.failure if not res.ok else None,
            state=res.state, receipt=res.receipt, op=res.operation,
            repair_prompt=(
                cx.build_repair_prompt(res.failure, c["args"])
                if (not res.ok and res.failure) else None
            ),
        ))

    failed = [o for o in outcomes if not o.ok]
    if failed and repair:
        try:
            corrected = await strat.arepair(client, model, new_messages, tools, outcomes, create_kwargs)
        except Exception:
            corrected = {}
        for o in failed:
            new_args = corrected.get(o.id)
            if new_args is None:
                continue
            retry = await cx.aexecute_repaired(o.name, new_args)
            if retry.ok:
                o.ok, o.value, o.repaired, o.failure, o.args = True, retry.value, True, None, new_args
                o.state, o.receipt, o.op = retry.state, retry.receipt, retry.operation
                strat.apply_correction(assistant_msg, o.id, new_args)
            else:
                o.failure = retry.failure

    new_messages += strat.tool_result_msgs(outcomes)

    operations = list(absence_ops) + [o.op for o in outcomes if o.op is not None]
    halted = [op for op in operations if getattr(op, "state", None) == "needs_review"]
    stats = {
        "passed": sum(1 for o in outcomes if o.ok and not o.repaired),
        "intercepted": sum(1 for o in outcomes if o.repaired or not o.ok),
        "repaired": sum(1 for o in outcomes if o.repaired),
        "bypass": 1 if bypass_finding else 0,
    }
    return RunResult(
        messages=new_messages, text=text,
        tool_calls=[o.public() for o in outcomes], finished=False,
        bypass=bypass_finding, operations=operations, halted=halted, stats=stats,
    )


# ─── bypass helpers ────────────────────────────────────────────────────────


def _tool_meta(tools: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """Extract ({names}, {name: description}) from OpenAI or Anthropic tool defs."""
    names: list[str] = []
    descs: dict[str, str] = {}
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            n, d = fn.get("name"), fn.get("description", "")
        else:
            n, d = t.get("name"), t.get("description", "")
        if n:
            names.append(n)
            descs[n] = d or ""
    return names, descs


def _called_tools(messages: list[Any]) -> set[str]:
    """Names of every tool called anywhere in the conversation (both formats)."""
    out: set[str] = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        for tc in (m.get("tool_calls") or []):          # OpenAI
            fn = tc.get("function") if isinstance(tc, dict) else None
            if fn and fn.get("name"):
                out.add(fn["name"])
        content = m.get("content")
        if isinstance(content, list):                    # Anthropic
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                    out.add(b["name"])
    return out


# ─── internal outcome record ──────────────────────────────────────────────


@dataclass
class _Outcome:
    id: str
    name: str
    args: dict[str, Any]
    ok: bool
    value: Any = None
    failure: Any = None
    repair_prompt: str | None = None
    repaired: bool = False
    # v0.5 action layer (None for an uninstrumented tool).
    state: Any = None
    receipt: Any = None
    op: Any = None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "args": self.args, "ok": self.ok,
            "value": self.value, "failure": self.failure, "repaired": self.repaired,
            "state": self.state, "op_id": getattr(self.op, "op_id", None),
        }

    def result_content(self) -> str:
        # The model's next-turn tool result is rendered from the RECEIPT, so it
        # works with "posted"/"unknown" + the proof, not its own assumption.
        confirmed = self.state in ("posted", "sent", "queued")
        if self.ok and (self.state is None or confirmed):
            payload: dict[str, Any] = {"ok": True, "result": self.value}
            if self.state is not None:
                payload["status"] = self.state
            if self.receipt is not None:
                payload["receipt"] = {"ok": self.receipt.ok, "id": self.receipt.id}
            return _safe_json(payload)
        if self.state in ("unknown", "needs_review", "failed"):
            return _safe_json({
                "ok": False, "status": self.state, "note": getattr(self.op, "note", None),
                "receipt": ({"ok": self.receipt.ok, "id": self.receipt.id} if self.receipt else None),
            })
        cat = getattr(self.failure, "category", "error")
        msg = getattr(self.failure, "message", "tool call rejected")
        return _safe_json({"ok": False, "error": f"{cat}: {msg}"})


# ─── provider resolution (duck-typed, no optional imports) ─────────────────


def _resolve(client: Any, provider: str) -> tuple[Any, "_Strategy"]:
    """Return (possibly-wrapped client, strategy).

    A LiteLLM `completion` callable is wrapped in an OpenAI-shaped shim so the
    whole OpenAI path — including auto_repair_batch — works unchanged.
    """
    p = provider
    if p == "auto":
        if hasattr(client, "chat") and hasattr(getattr(client, "chat"), "completions"):
            p = "openai"            # OpenAI, AzureOpenAI
        elif hasattr(client, "messages") and hasattr(getattr(client, "messages"), "create"):
            p = "anthropic"
        elif callable(client):
            p = "litellm"           # litellm.completion(...)
        else:
            raise ProviderUnsupported(
                "could not detect the provider from this client. Pass "
                "provider='openai'/'anthropic'/'litellm', or integrate manually "
                "with guard().execute()."
            )

    strat = _STRATEGIES.get(p)
    if strat is None:
        raise ProviderUnsupported(
            f"unknown provider {p!r}. use one of: {', '.join(sorted(_STRATEGIES))}, or 'auto'."
        )

    if p == "litellm":
        if callable(client) and not hasattr(client, "chat"):
            client = _OpenAIShim(client)  # wrap the completion callable

    return client, strat


class _OpenAIShim:
    """Wrap a `litellm.completion`-style callable as an OpenAI-shaped client."""

    def __init__(self, fn: Callable[..., Any]):
        self.chat = _ShimChat(fn)


class _ShimChat:
    def __init__(self, fn): self.completions = _ShimCompletions(fn)


class _ShimCompletions:
    def __init__(self, fn): self._fn = fn
    def create(self, **kwargs): return self._fn(**kwargs)


# ─── strategies ────────────────────────────────────────────────────────────


class _Strategy:
    """Per-provider call shape. One instance per provider, stateless."""

    def extract_schemas(self, tools): raise NotImplementedError
    def create(self, client, model, messages, tools, kwargs): raise NotImplementedError
    def parse(self, resp): raise NotImplementedError  # -> (assistant_msg, calls, text)
    def tool_result_msgs(self, outcomes): raise NotImplementedError
    def repair(self, client, model, messages, tools, outcomes, kwargs): raise NotImplementedError
    def apply_correction(self, assistant_msg, call_id, new_args): pass  # rewrite persisted args

    # async twins (used by arun) — same shapes, awaited client calls
    async def acreate(self, client, model, messages, tools, kwargs): raise NotImplementedError
    async def arepair(self, client, model, messages, tools, outcomes, kwargs): raise NotImplementedError


class _OpenAIStrategy(_Strategy):
    """OpenAI / Azure OpenAI / LiteLLM-as-client (all OpenAI response-shaped)."""

    def extract_schemas(self, tools):
        from cruxial.adapters.openai import extract_schemas
        return extract_schemas(tools)

    def create(self, client, model, messages, tools, kwargs):
        return client.chat.completions.create(
            model=model, messages=messages, tools=tools, **kwargs
        )

    def parse(self, resp):
        msg = resp.choices[0].message
        text = getattr(msg, "content", None)
        raw_calls = getattr(msg, "tool_calls", None) or []
        calls = []
        tc_array = []
        for tc in raw_calls:
            fn = tc.function
            try:
                args = json.loads(fn.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append({"id": tc.id, "name": fn.name, "args": args})
            tc_array.append({
                "id": tc.id, "type": "function",
                "function": {"name": fn.name, "arguments": fn.arguments},
            })
        if tc_array:
            assistant_msg = {"role": "assistant", "content": text, "tool_calls": tc_array}
        else:
            assistant_msg = {"role": "assistant", "content": text}
        return assistant_msg, calls, text

    def tool_result_msgs(self, outcomes):
        return [
            {"role": "tool", "tool_call_id": o.id, "content": o.result_content()}
            for o in outcomes
        ]

    def apply_correction(self, assistant_msg, call_id, new_args):
        for tc in assistant_msg.get("tool_calls") or []:
            if tc.get("id") == call_id:
                tc["function"]["arguments"] = json.dumps(new_args)
                return

    def repair(self, client, model, messages, tools, outcomes, kwargs):
        from cruxial.adapters.openai import auto_repair_batch
        payload = [
            {
                "tool_call_id": o.id, "name": o.name, "ok": o.ok,
                "value": o.value, "failure": o.failure, "repair_prompt": o.repair_prompt,
            }
            for o in outcomes
        ]
        return auto_repair_batch(
            client, model=model, messages=messages, tools=tools, tool_call_outcomes=payload,
        )

    async def acreate(self, client, model, messages, tools, kwargs):
        return await client.chat.completions.create(
            model=model, messages=messages, tools=tools, **kwargs)

    async def arepair(self, client, model, messages, tools, outcomes, kwargs):
        from cruxial.adapters.openai import auto_repair_batch_async
        payload = [
            {"tool_call_id": o.id, "name": o.name, "ok": o.ok,
             "value": o.value, "failure": o.failure, "repair_prompt": o.repair_prompt}
            for o in outcomes
        ]
        return await auto_repair_batch_async(
            client, model=model, messages=messages, tools=tools, tool_call_outcomes=payload)


class _AnthropicStrategy(_Strategy):
    """Anthropic Messages API — input_schema + tool_use / tool_result blocks."""

    def extract_schemas(self, tools):
        from cruxial.adapters.anthropic import extract_schemas
        return extract_schemas(tools)

    def create(self, client, model, messages, tools, kwargs):
        kw = dict(kwargs)
        kw.setdefault("max_tokens", 1024)  # Anthropic requires it
        return client.messages.create(model=model, messages=messages, tools=tools, **kw)

    def parse(self, resp):
        content = getattr(resp, "content", []) or []
        text_parts = []
        calls = []
        blocks = []  # normalize SDK block objects → plain dicts (round-trip safe)
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text":
                t = getattr(block, "text", "")
                text_parts.append(t)
                blocks.append({"type": "text", "text": t})
            elif btype == "tool_use":
                inp = getattr(block, "input", None)
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except json.JSONDecodeError:
                        inp = {}
                inp = inp if isinstance(inp, dict) else {}
                bid, bname = getattr(block, "id", ""), getattr(block, "name", "")
                calls.append({"id": bid, "name": bname, "args": inp})
                blocks.append({"type": "tool_use", "id": bid, "name": bname, "input": inp})
        assistant_msg = {"role": "assistant", "content": blocks}
        text = "".join(text_parts) or None
        return assistant_msg, calls, text

    def apply_correction(self, assistant_msg, call_id, new_args):
        for b in assistant_msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == call_id:
                b["input"] = new_args
                return

    def tool_result_msgs(self, outcomes):
        # Anthropic: a single user turn carrying one tool_result block per call.
        blocks = []
        for o in outcomes:
            block = {"type": "tool_result", "tool_use_id": o.id, "content": o.result_content()}
            if not o.ok:
                block["is_error"] = True
            blocks.append(block)
        return [{"role": "user", "content": blocks}]

    def _build_repair(self, messages, outcomes):
        """Build the ephemeral repair exchange + id back-match table. Pure;
        shared by repair()/arepair(). Returns None if nothing to repair."""
        failed = [o for o in outcomes if not o.ok]
        if not failed:
            return None
        # Feed every prior tool_use a tool_result (failures flagged is_error),
        # then ask the model to re-emit.
        result_blocks = []
        for o in outcomes:
            blk = {
                "type": "tool_result", "tool_use_id": o.id,
                "content": (o.repair_prompt if not o.ok else o.result_content()),
            }
            if not o.ok:
                blk["is_error"] = True
            result_blocks.append(blk)
        summary = "; ".join(
            f"{o.name} ({getattr(o.failure, 'category', 'error')})" for o in failed
        )
        repair_messages = list(messages) + [
            {"role": "user", "content": result_blocks
                + [{"type": "text", "text":
                    f"Re-emit corrected tool call(s) for: {summary}. "
                    f"Emit exactly {len(failed)} tool call(s)."}]},
        ]
        remaining: dict[str, list[str]] = {}
        for o in failed:
            remaining.setdefault(o.name, []).append(o.id)
        return repair_messages, remaining

    def _parse_corrected(self, resp, remaining):
        """Back-match re-emitted tool_use blocks to failed ids by name, in order."""
        corrected: dict[str, dict[str, Any]] = {}
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = getattr(block, "name", None)
            if not name or not remaining.get(name):
                continue
            inp = getattr(block, "input", None)
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except json.JSONDecodeError:
                    continue
            if isinstance(inp, dict):
                corrected[remaining[name].pop(0)] = inp
        return corrected

    def repair(self, client, model, messages, tools, outcomes, kwargs):
        built = self._build_repair(messages, outcomes)
        if built is None:
            return {}
        repair_messages, remaining = built
        kw = dict(kwargs); kw.setdefault("max_tokens", 1024)
        resp = client.messages.create(model=model, messages=repair_messages, tools=tools, **kw)
        return self._parse_corrected(resp, remaining)

    async def acreate(self, client, model, messages, tools, kwargs):
        kw = dict(kwargs); kw.setdefault("max_tokens", 1024)
        return await client.messages.create(model=model, messages=messages, tools=tools, **kw)

    async def arepair(self, client, model, messages, tools, outcomes, kwargs):
        built = self._build_repair(messages, outcomes)
        if built is None:
            return {}
        repair_messages, remaining = built
        kw = dict(kwargs); kw.setdefault("max_tokens", 1024)
        resp = await client.messages.create(model=model, messages=repair_messages, tools=tools, **kw)
        return self._parse_corrected(resp, remaining)


_OPENAI = _OpenAIStrategy()
_ANTHROPIC = _AnthropicStrategy()

_STRATEGIES: dict[str, _Strategy] = {
    "openai": _OPENAI,
    "azure": _OPENAI,
    "anthropic": _ANTHROPIC,
    "litellm": _OPENAI,  # litellm responses are OpenAI-shaped (callable is shimmed)
}


def _safe_json(value: Any, max_chars: int = 600) -> str:
    try:
        s = json.dumps(value, default=str)
        return s if len(s) <= max_chars else json.dumps({"ok": value.get("ok", True), "result": f"<truncated {len(s)} chars>"})
    except Exception:
        return json.dumps({"ok": False, "error": "unserialisable result"})
