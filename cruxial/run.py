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

from cruxial.core import Cruxial, GuardConfig, guard as _guard
from cruxial.errors import ProviderUnsupported


@dataclass
class RunResult:
    """The outcome of one managed turn.

    Attributes:
        messages:   Your conversation with this turn appended (assistant turn +
                    tool results, in the provider's format). Pass it straight
                    back into the next ``run()`` call.
        text:       The assistant's natural-language text this turn, if any.
                    Usually None on a turn where the model only called tools;
                    populated on the final wrap-up turn.
        tool_calls: One entry per tool call this turn:
                    {name, args, ok, value, failure, repaired}.
        finished:   True when the model returned no tool calls — your cue to
                    stop the outer loop.
        stats:      {passed, intercepted, repaired} counts for this turn.
    """

    messages: list[Any]
    text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    stats: dict[str, int] = field(default_factory=dict)


def run(
    client: Any,
    *,
    model: str,
    messages: list[Any],
    tools: list[dict[str, Any]],
    executors: Mapping[str, Callable[..., Any]],
    repair: bool = True,
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

    # 3. No tool calls → the turn is the answer. Done.
    if not calls:
        return RunResult(
            messages=new_messages, text=text, tool_calls=[], finished=True,
            stats={"passed": 0, "intercepted": 0, "repaired": 0},
        )

    # 4. Validate + execute each call.
    outcomes: list[_Outcome] = []
    for c in calls:
        res = cx.execute(c["name"], c["args"])
        outcomes.append(_Outcome(
            id=c["id"], name=c["name"], args=c["args"],
            ok=res.ok, value=res.value if res.ok else None,
            failure=res.failure if not res.ok else None,
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
                # Keep history coherent: the persisted assistant turn should
                # show the args that ACTUALLY ran, not the rejected ones.
                strat.apply_correction(assistant_msg, o.id, new_args)
            else:
                o.failure = retry.failure  # still bad after one shot

    # 6. Append tool results (provider format), carrying the final outcomes.
    new_messages += strat.tool_result_msgs(outcomes)

    stats = {
        "passed": sum(1 for o in outcomes if o.ok and not o.repaired),
        "intercepted": sum(1 for o in outcomes if o.repaired or not o.ok),
        "repaired": sum(1 for o in outcomes if o.repaired),
    }
    return RunResult(
        messages=new_messages,
        text=text,
        tool_calls=[o.public() for o in outcomes],
        finished=False,
        stats=stats,
    )


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

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "args": self.args, "ok": self.ok,
            "value": self.value, "failure": self.failure, "repaired": self.repaired,
        }

    def result_content(self) -> str:
        if self.ok:
            return _safe_json({"ok": True, "result": self.value})
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

    def repair(self, client, model, messages, tools, outcomes, kwargs):
        failed = [o for o in outcomes if not o.ok]
        if not failed:
            return {}
        # Ephemeral repair exchange: feed every prior tool_use a tool_result
        # (failures flagged is_error), then ask the model to re-emit.
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
        kw = dict(kwargs)
        kw.setdefault("max_tokens", 1024)
        resp = client.messages.create(
            model=model, messages=repair_messages, tools=tools, **kw
        )
        # Back-match re-emitted tool_use blocks to failed ids by name, in order.
        remaining: dict[str, list[str]] = {}
        for o in failed:
            remaining.setdefault(o.name, []).append(o.id)
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
