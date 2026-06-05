"""Hang findings (#1 ReDoS pattern, #3 uniqueItems O(n²)) from the 0.2.0 review.

Each repro is run in a SUBPROCESS with a hard timeout: a regression that
reintroduces the hang fails the test (TimeoutExpired) instead of hanging the
whole suite. The fixes are: re2 when installed else a static guard that refuses
to run catastrophic patterns (#1), and an O(n) set-based uniqueItems (#3).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_DEADLINE = 15  # generous; the fixed paths return in well under 0.2s


def _run(code: str) -> str:
    try:
        r = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            timeout=_DEADLINE, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"validation hung (> {_DEADLINE}s) — ReDoS/O(n²) regression")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("pattern,inp", [
    ("(a+)+$", "a" * 40 + "!"),       # nested quantifier
    ("(a*)*$", "a" * 40 + "!"),
    ("(a|aa)+$", "a" * 40 + "!"),     # alternation overlap
    ("(x|x)*$", "x" * 40 + "!"),
    ("(ab|a|b)+$", "a" * 40 + "!"),
    (r"(.*a){20}$", "a" * 40 + "!"),  # fixed-count brace (no comma)
    (r"(.*a){20,}$", "a" * 40 + "!"),
])
def test_redos_pattern_does_not_hang(pattern, inp):
    out = _run(f"""
        import warnings; warnings.simplefilter('ignore')
        from cruxial import guard, GuardConfig
        g = guard(schemas={{'lookup': {{'type':'object','properties':{{
                      'code':{{'type':'string','pattern':{pattern!r}}}}}}}}},
                  executors={{'lookup': lambda **k:'ran'}}, config=GuardConfig(sinks=('null',)))
        r = g.execute('lookup', {{'code': {inp!r}}})
        print('OK', r.ok)
    """)
    assert out.startswith("OK")


def test_unique_items_large_array_does_not_hang():
    out = _run("""
        from cruxial import guard, GuardConfig
        g = guard(schemas={'t': {'type':'object','properties':{
                      'a':{'type':'array','uniqueItems':True}}}},
                  executors={'t': lambda **k:'ran'}, config=GuardConfig(sinks=('null',)))
        r = g.execute('t', {'a':[{'i':i} for i in range(8000)]})
        print('OK', r.ok)
    """)
    assert out == "OK True"


def test_unique_items_large_array_still_catches_dup():
    out = _run("""
        from cruxial.validator import validate
        s = {'type':'object','properties':{'a':{'type':'array','uniqueItems':True}}}
        items = [{'i':i} for i in range(5000)] + [{'i':0}]  # dup of the first
        print('OK', validate('t', {'a': items}, s).ok)
    """)
    assert out == "OK False"
