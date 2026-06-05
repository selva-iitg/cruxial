"""Security/correctness hardening — locks the fixes for the 0.2.0 validator
review (findings #2, #4–#10, #13). The two hang findings (#1, #3) are in
test_robustness_redos_watchdog.py (subprocess-isolated).
"""

from __future__ import annotations

import socket
import warnings

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import hash_args
from cruxial.validator import close_open_objects, external_refs, validate

_NULL = GuardConfig(sinks=("null",))


def vfmt(fmt):
    return {"type": "object", "properties": {"v": {"type": "string", "format": fmt}},
            "required": ["v"]}


# ─── #2 — no network during validation (SSRF / file read) ────────────────────

def test_remote_ref_makes_no_network_call(monkeypatch):
    hits = []

    def spy(self, addr, *a, **k):
        hits.append(addr)
        raise OSError("blocked")

    monkeypatch.setattr(socket.socket, "connect", spy)
    # The external ref raises Unresolvable (no fetch). Bare validate() surfaces it;
    # in core it's caught by _fail_open_validate. The security property is: no network.
    with pytest.raises(Exception):
        validate("t", {"x": 1}, {"$ref": "http://169.254.169.254/latest/meta-data/"})
    assert hits == []


def test_remote_ref_through_guard_fails_open_no_network(monkeypatch):
    hits = []
    monkeypatch.setattr(socket.socket, "connect",
                        lambda self, addr, *a, **k: (hits.append(addr), (_ for _ in ()).throw(OSError()))[1])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        g = guard(schemas={"t": {"type": "object", "properties": {"x": {"$ref": "http://169.254.169.254/x"}}}},
                  executors={"t": lambda **k: "ran"}, config=_NULL)
        res = g.execute("t", {"x": 1})
    assert res.ok          # fail-open: unresolved ref doesn't break the host
    assert hits == []      # and never touched the network


def test_internal_ref_still_resolves():
    schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/pos"}},
              "required": ["x"], "$defs": {"pos": {"type": "integer", "minimum": 1}}}
    assert validate("t", {"x": 5}, schema).ok
    assert not validate("t", {"x": -1}, schema).ok


def test_external_refs_detected():
    schema = {"properties": {"a": {"$ref": "http://x/y"}, "b": {"$ref": "#/$defs/z"}}}
    assert external_refs(schema) == ["http://x/y"]


# ─── #4 — calendar / clock validity ──────────────────────────────────────────

@pytest.mark.parametrize("fmt,val", [
    ("date", "2026-02-30"), ("date", "2026-13-45"), ("date", "0000-00-00"),
    ("time", "25:61:61"), ("time", "12:60:00"),
    ("date-time", "2026-02-30T25:61:61Z"),
])
def test_impossible_temporal_rejected(fmt, val):
    assert not validate("t", {"v": val}, vfmt(fmt)).ok


@pytest.mark.parametrize("fmt,val", [
    ("date", "2024-02-29"),               # real leap day
    ("time", "23:59:60"),                 # leap second
    ("time", "12:30:00"), ("time", "12:30:00.5Z"),
    ("date-time", "2026-06-05T09:30:00Z"),
    ("date-time", "2026-06-05T09:30:00+05:30"),
])
def test_valid_temporal_accepted(fmt, val):
    assert validate("t", {"v": val}, vfmt(fmt)).ok


# ─── #5 — finite numbers only ────────────────────────────────────────────────

@pytest.mark.parametrize("v", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_number_rejected(v):
    s = {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]}
    assert not validate("t", {"n": v}, s).ok


def test_finite_numbers_still_accepted():
    s = {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]}
    assert validate("t", {"n": 3.14}, s).ok
    assert validate("t", {"n": 0}, s).ok
    i = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert validate("t", {"n": 5}, i).ok
    assert validate("t", {"n": 5.0}, i).ok          # integer-valued float ok
    assert not validate("t", {"n": 5.5}, i).ok
    assert not validate("t", {"n": True}, i).ok     # bool is not an integer


# ─── #6 — exact multipleOf ───────────────────────────────────────────────────

def test_multipleof_accepts_ieee754_valid():
    s = {"type": "object", "properties": {"n": {"type": "number", "multipleOf": 0.1}}}
    assert validate("t", {"n": 0.3}, s).ok
    assert validate("t", {"n": 0.7}, s).ok
    assert not validate("t", {"n": 0.05}, s).ok


# ─── #7 — strict_properties closes open objects ──────────────────────────────

def test_strict_properties_catches_extra_field():
    schema = {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}
    g = guard(schemas={"send": schema}, executors={"send": lambda **k: "sent"},
              config=GuardConfig(strict_properties=True, sinks=("null",)))
    r = g.execute("send", {"to": "a@b.com", "cc": "evil@x.com"})
    assert not r.ok and r.failure.category == "extra_field"


def test_strict_properties_off_by_default():
    schema = {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}
    g = guard(schemas={"send": schema}, executors={"send": lambda **k: "sent"}, config=_NULL)
    assert g.execute("send", {"to": "a@b.com", "cc": "x"}).ok


def test_close_open_objects_nested_and_preserves_open():
    schema = {"type": "object", "properties": {
        "outer": {"type": "object", "properties": {"inner": {"type": "string"}}},
        "bag": {"type": "object", "properties": {"k": {"type": "string"}},
                "additionalProperties": True},  # intentionally open — must stay open
    }}
    closed = close_open_objects(schema)
    assert closed["additionalProperties"] is False
    assert closed["properties"]["outer"]["additionalProperties"] is False
    assert closed["properties"]["bag"]["additionalProperties"] is True  # untouched


# ─── #8 — structural email ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["a@", "@b.com", "a b@c.com", "a@b@c.com", "a@b"])
def test_bad_email_rejected(bad):
    assert not validate("t", {"v": bad}, vfmt("email")).ok


@pytest.mark.parametrize("good", ["a@b.com", "first.last@sub.example.co"])
def test_good_email_accepted(good):
    assert validate("t", {"v": good}, vfmt("email")).ok


# ─── #9 — uri scheme deny + allowlist ────────────────────────────────────────

@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,x", "vbscript:x"])
def test_dangerous_uri_schemes_denied_by_default(bad):
    assert not validate("t", {"v": bad}, vfmt("uri")).ok


def test_file_scheme_allowed_by_default_but_excludable_via_allowlist():
    # file: is legitimate for file-handling tools → not denied by default
    # (precision first); the opt-in allowlist can still exclude it.
    s = vfmt("uri")
    assert validate("t", {"v": "file:///etc/passwd"}, s).ok
    assert not validate("t", {"v": "file:///etc/passwd"}, s, uri_schemes=("http", "https")).ok


def test_uri_allowlist_opt_in():
    s = vfmt("uri")
    assert not validate("t", {"v": "ftp://x"}, s, uri_schemes=("http", "https")).ok
    assert validate("t", {"v": "https://x.com"}, s, uri_schemes=("http", "https")).ok
    assert validate("t", {"v": "ftp://x"}, s).ok  # allowed when no allowlist


# ─── #10 — schema checked at construction (no silent disable) ─────────────────

_BAD_SCHEMA = {"type": "object", "properties": ["not", "a", "dict"]}


def test_invalid_schema_raises_under_strict():
    with pytest.raises(Exception):
        guard(schemas={"t": _BAD_SCHEMA}, executors={"t": lambda **k: "r"}, config=_NULL)


def test_invalid_schema_warns_under_non_strict():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        guard(schemas={"t": _BAD_SCHEMA}, executors={"t": lambda **k: "r"},
              config=GuardConfig(strict=False, sinks=("null",)))
    assert any("not a valid JSON Schema" in str(x.message) for x in w)


def test_external_ref_schema_warns():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        guard(schemas={"t": {"type": "object", "properties": {"x": {"$ref": "http://x/y"}}}},
              executors={"t": lambda **k: "r"}, config=_NULL)
    assert any("external $ref" in str(x.message) for x in w)


# ─── #13 — hash_args total on mixed-type keys ────────────────────────────────

def test_hash_args_mixed_type_keys_does_not_raise():
    h = hash_args({5: "x", "a": 1, (1, 2): "y"})
    assert isinstance(h, str) and len(h) == 16
