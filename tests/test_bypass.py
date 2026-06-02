"""Unit tests for the tool-bypass detector (the local SUSPECT() pre-filter).

Locks the STABLE behaviors. Known recall gaps (colloquialisms, verb-synonym
mismatches) live in examples/bypass_eval.py, not here — those are the dial we
grow over time, not a contract.
"""

from __future__ import annotations

import pytest

from cruxial.bypass import asserted_actions, detect_bypass, is_side_effecting


def _flag(text, tools, this_turn=(), ever=(), **kw):
    return detect_bypass(text, tool_calls_this_turn=this_turn,
                         tool_names=tools, called_tools=ever, **kw)


# ─── catches real bypasses ──────────────────────────────────────────────────

@pytest.mark.parametrize("text,tool", [
    ("I've sent the email to a@b.com.", "send_email"),
    ("The task has been created.", "create_task"),
    ("I updated the contact record.", "update_contact"),
    ("The meeting is scheduled for 3pm.", "schedule_meeting"),
    ("I posted your message to the channel.", "post_message"),
    ("Email sent ✅", "send_email"),
])
def test_catches_claim_without_call(text, tool):
    sus = _flag(text, [tool])
    assert sus is not None and sus.tool == tool


# ─── must NOT flag: form/intent/refusal/question ────────────────────────────

@pytest.mark.parametrize("text", [
    "I'll send the email once you confirm.",        # future intent (base form)
    "Your email will be sent automatically at 9am.", # future passive
    "I can't send the email — no address.",          # refusal
    "The record was not created.",                    # negation
    "Should I send the email now?",                   # question
    "Here's a summary of your tasks.",                # no action verb
    "I've reviewed your code.",                        # untracked verb, no matching tool
])
def test_does_not_flag_non_completions(text):
    assert _flag(text, ["send_email", "create_task", "create_record", "update_record"]) is None


# ─── must NOT flag: the tool actually ran ───────────────────────────────────

def test_no_flag_when_called_this_turn():
    assert _flag("I'm sending the email.", ["send_email"], this_turn=["send_email"]) is None


def test_no_flag_when_called_earlier_in_conversation():
    assert _flag("I've sent the email.", ["send_email"], ever=["send_email"]) is None


# ─── must NOT flag: read-only tools ─────────────────────────────────────────

def test_read_only_tools_are_not_side_effecting():
    assert is_side_effecting("get_weather") is False
    assert is_side_effecting("list_tasks") is False
    assert is_side_effecting("search_records") is False
    assert is_side_effecting("send_email") is True
    assert is_side_effecting("create_task") is True


def test_no_flag_for_read_only_claim():
    assert _flag("I looked up the weather, it's sunny.", ["get_weather"]) is None


# ─── override + fail-open ───────────────────────────────────────────────────

def test_side_effecting_override():
    # Force a normally side-effecting tool to be treated as read-only → no flag.
    assert _flag("I've sent it.", ["send_email"], side_effecting=[]) is None


def test_fail_open_on_garbage_input():
    # Never raises; returns None on anything it can't analyse.
    assert detect_bypass(None, tool_calls_this_turn=[], tool_names=[], called_tools=[]) is None
    assert _flag(12345, ["send_email"]) is None  # non-str text


# ─── assertion extractor unit ───────────────────────────────────────────────

def test_asserted_actions_requires_completion_form():
    assert asserted_actions("I sent it") == {"send": "sent"}
    assert asserted_actions("I will send it") == {}          # base form
    assert asserted_actions("it will be sent") == {}          # future passive
    assert asserted_actions("it has been sent") == {"send": "sent"}  # completed passive
