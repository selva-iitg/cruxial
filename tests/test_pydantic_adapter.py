"""Pydantic adapter — define tools as Pydantic models, validate via cruxial.

Requires Pydantic v2 (optional `cruxial[pydantic]` extra); skipped if absent.
"""
from __future__ import annotations

import pytest

pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel, Field  # noqa: E402
from enum import Enum  # noqa: E402
from typing import List, Optional  # noqa: E402

from cruxial import GuardConfig  # noqa: E402
from cruxial.adapters.pydantic import (  # noqa: E402
    extract_schemas,
    guard_models,
    register_models,
    tool_schema,
)

_NULL = GuardConfig(sinks=("null",))


class Priority(str, Enum):
    low = "low"
    high = "high"


class Attachment(BaseModel):
    filename: str
    size_kb: int = Field(ge=0, le=10_000)


class SendEmail(BaseModel):
    """Send an email to a recipient."""
    to: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=1, max_length=200)
    body: str
    cc: List[str] = []
    priority: Priority = Priority.low
    attachment: Optional[Attachment] = None


# ─── extract_schemas: the three input forms ──────────────────────────────────

def test_extract_single_model():
    s = extract_schemas(SendEmail)
    assert set(s) == {"SendEmail"}
    assert s["SendEmail"]["type"] == "object"
    assert "$defs" in s["SendEmail"]  # nested model + enum land here


def test_extract_list_and_dict_naming():
    assert set(extract_schemas([SendEmail, Attachment])) == {"SendEmail", "Attachment"}
    renamed = extract_schemas({"send_email": SendEmail})   # snake_case override
    assert set(renamed) == {"send_email"}


def test_extract_rejects_non_model():
    with pytest.raises(TypeError):
        extract_schemas({"bad": dict})


def test_extract_rejects_instance_with_clear_error():
    # passing SendEmail(...) instead of SendEmail is a common mistake
    with pytest.raises(TypeError, match="instance"):
        extract_schemas(SendEmail(to="a@b.com", subject="Hi", body="yo"))


def test_extract_rejects_iterable_with_non_model():
    with pytest.raises(TypeError):
        extract_schemas([SendEmail, "not a model"])


def test_extract_rejects_duplicate_model_names():
    # two distinct classes that share __name__ must not silently overwrite
    Dup = type("Attachment", (BaseModel,), {"__annotations__": {"z": int}})
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        extract_schemas([Attachment, Dup])


# ─── rich Pydantic types validate end-to-end (no false failures) ─────────────

def test_rich_types_round_trip():
    from datetime import datetime, date  # noqa
    from uuid import UUID  # noqa
    from typing import Union, Dict, Literal

    class Rich(BaseModel):
        when: datetime
        uid: UUID
        mode: Literal["fast", "slow"]
        either: Union[int, str]
        meta: Dict[str, int]
        score: float = Field(ge=0, le=1)

    g = guard_models({"r": Rich}, executors={"r": lambda **k: "ok"}, config=_NULL)
    ok = {"when": "2026-06-07T10:00:00Z", "uid": "12345678-1234-5678-1234-567812345678",
          "mode": "fast", "either": 5, "meta": {"k": 1}, "score": 0.5}
    assert g.execute("r", ok).ok
    assert not g.execute("r", {**ok, "when": "2026-13-99"}).ok       # bad datetime
    assert not g.execute("r", {**ok, "score": 5.0}).ok               # out of range
    assert not g.execute("r", {**ok, "mode": "medium"}).ok           # bad literal


def test_recursive_model_validates():
    from typing import Optional

    class Node(BaseModel):
        val: int
        child: Optional["Node"] = None
    Node.model_rebuild()

    g = guard_models({"n": Node}, executors={"n": lambda **k: "ok"}, config=_NULL)
    assert g.execute("n", {"val": 1, "child": {"val": 2}}).ok
    assert not g.execute("n", {"val": 1, "child": {"val": "x"}}).ok  # bad nested type


def test_strict_properties_closes_nested_model():
    class Inner(BaseModel):
        a: int

    class Outer(BaseModel):
        inner: Inner

    g = guard_models({"t": Outer}, executors={"t": lambda **k: "ok"},
                     config=GuardConfig(strict_properties=True, sinks=("null",)))
    r = g.execute("t", {"inner": {"a": 1, "evil": 2}})
    assert not r.ok and r.failure.category == "extra_field"


# ─── end-to-end validation through a guard built from models ─────────────────

def _g():
    return guard_models(
        {"send_email": SendEmail},
        executors={"send_email": lambda **k: "sent"},
        config=_NULL,
    )


def test_valid_payload_executes():
    r = _g().execute("send_email", {"to": "a@b.com", "subject": "Hi", "body": "yo"})
    assert r.ok and r.value == "sent"


@pytest.mark.parametrize("args,category", [
    ({"subject": "Hi", "body": "yo"}, "missing_required"),                 # no 'to'
    ({"to": "nope", "subject": "Hi", "body": "yo"}, "format_violation"),   # bad pattern
    ({"to": "a@b.com", "subject": "x" * 201, "body": "yo"}, "constraint_violation"),
    ({"to": "a@b.com", "subject": "Hi", "body": "yo", "priority": "urgent"}, "enum_violation"),
])
def test_violations_caught(args, category):
    r = _g().execute("send_email", args)
    assert not r.ok and r.failure.category == category


@pytest.mark.parametrize("attachment", [
    {"filename": "x", "size_kb": 99999},   # nested constraint (> max) via $ref
    {"size_kb": 10},                       # nested missing required 'filename'
])
def test_nested_ref_violations_caught(attachment):
    r = _g().execute("send_email", {"to": "a@b.com", "subject": "Hi", "body": "yo",
                                    "attachment": attachment})
    assert not r.ok


def test_nested_ref_valid_passes():
    r = _g().execute("send_email", {"to": "a@b.com", "subject": "Hi", "body": "yo",
                                    "attachment": {"filename": "x.pdf", "size_kb": 50}})
    assert r.ok


def test_register_models_into_existing_guard():
    cx = guard_models({"send_email": SendEmail}, config=_NULL)
    assert register_models(cx, {"attach": Attachment}) == 1
    assert "attach" in cx.schemas


# ─── tool_schema: provider tool-call definitions from the same model ─────────

def test_tool_schema_openai():
    t = tool_schema(SendEmail, name="send_email")
    assert t["type"] == "function"
    assert t["function"]["name"] == "send_email"
    assert t["function"]["parameters"]["type"] == "object"
    assert "email" in t["function"]["description"].lower()  # from docstring


def test_tool_schema_anthropic():
    t = tool_schema(SendEmail, name="send_email", provider="anthropic")
    assert t["name"] == "send_email"
    assert t["input_schema"]["type"] == "object"
    assert "description" in t


def test_tool_schema_unknown_provider():
    with pytest.raises(ValueError):
        tool_schema(SendEmail, provider="cohere")


def test_lazy_import_does_not_taint_core():
    # importing cruxial must never require pydantic
    import importlib, sys
    assert "cruxial" in sys.modules
    importlib.import_module("cruxial.adapters.pydantic")  # importable; funcs lazy-load
