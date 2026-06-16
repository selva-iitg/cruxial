"""Tool-bypass detection — the local `SUSPECT()` pre-filter.

The failure mode validators can't catch: the assistant's *text* asserts an
action was performed ("I've sent the email") but it emitted **no matching tool
call**. There's nothing to schema-validate — the failure is the gap between the
prose claim and the absent call.

This module is the **detection** step, and it is deliberately:
  - **Local — zero model calls.** It reads the assistant text you already have.
  - **Deterministic in effect.** A flagged turn is recorded as an `unknown`
    operation — Cruxial never re-prompts the model to confirm (a confident model
    just re-affirms a false claim; the receipt's absence is the oracle). Because
    there is no re-prompt safety net, the detector is **precision-first**: a
    false flag mislabels one operation `unknown`, so the attribution/negation
    guards below are load-bearing. Recall is bounded on purpose (see below).

Key idea that does most of the work: we require **completion-form verbs**
("sent", "created", "updated") — not base forms ("send", "create"). That single
rule excludes future intent ("I'll send"), refusals ("I can't send"), and
questions ("should I send?") for free, because none of those use the
completion form.

A turn is SUSPECT only when ALL hold:
  1. no tool call was emitted this turn (it's a candidate final reply),
  2. the text makes a completion assertion mapped to a known action verb,
  3. a **side-effecting** tool matching that action exists AND was never called
     anywhere in the conversation.

Known limitation (by design): the trigger is a completion-FORM verb. A claim
with no such verb — a purely idiomatic completion like "email's out" or "all
set" — is NOT flagged. We accept that ceiling rather than special-case idioms,
because precision (never recording a clean turn as `unknown`) is the load-bearing
property and the verb vocabulary grows from real misses. New verbs/synonyms are
cheap to add; verb-less idioms are not, and chasing them risks the precision moat.
The durable catch for verb-less claims is the **receipt** (a declared action with
no receipt is `unknown` regardless of the prose), not a smarter prose reader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


# ─── public types ──────────────────────────────────────────────────────────


@dataclass
class BypassSuspicion:
    """A turn flagged as possibly claiming an action it never took."""

    tool: str          # the side-effecting tool we believe was claimed-but-not-called
    action: str        # canonical action verb (e.g. "send", "create")
    evidence: str      # the completion phrase that triggered it
    reason: str        # human-readable explanation


# ─── verb vocabularies ──────────────────────────────────────────────────────

# completion-form word (past tense / past participle)  →  canonical action
_COMPLETION_TO_CANON: dict[str, str] = {
    "sent": "send", "emailed": "send", "mailed": "send",
    "messaged": "message", "dmed": "message", "pinged": "message",
    "notified": "notify", "alerted": "notify",
    "created": "create", "made": "create", "added": "create", "inserted": "create",
    "generated": "create", "registered": "create", "filed": "create",
    "updated": "update", "changed": "update", "modified": "update", "edited": "update",
    "patched": "update",
    "saved": "save", "stored": "save", "written": "save", "wrote": "save", "logged": "save",
    "deleted": "delete", "removed": "delete", "destroyed": "delete",
    "cancelled": "cancel", "canceled": "cancel",
    "scheduled": "schedule", "booked": "schedule",
    "posted": "post", "published": "post", "submitted": "post", "shared": "post",
    "uploaded": "upload",
    "charged": "charge", "paid": "charge", "billed": "charge",
    "transferred": "transfer", "refunded": "refund",
    "assigned": "assign", "invited": "invite", "approved": "approve",
    "closed": "close", "merged": "merge", "deployed": "deploy", "pushed": "deploy", "shipped": "deploy",
    "ordered": "order", "purchased": "order", "placed": "order",
}

# base-form verb (as it appears in tool NAMES)  →  canonical action
_VERB_TO_CANON: dict[str, str] = {
    "send": "send", "email": "send", "mail": "send",
    "message": "message", "dm": "message", "notify": "notify", "alert": "notify",
    "create": "create", "add": "create", "insert": "create", "make": "create",
    "generate": "create", "register": "create", "new": "create", "file": "create",
    "update": "update", "change": "update", "modify": "update", "edit": "update",
    "patch": "update", "set": "update",
    "save": "save", "store": "save", "write": "save", "log": "save",
    "delete": "delete", "remove": "delete", "destroy": "delete", "cancel": "cancel",
    "schedule": "schedule", "book": "schedule",
    "post": "post", "publish": "post", "submit": "post", "share": "post",
    "upload": "upload",
    "charge": "charge", "pay": "charge", "bill": "charge", "transfer": "transfer",
    "refund": "refund",
    "assign": "assign", "invite": "invite", "approve": "approve", "close": "close",
    "merge": "merge", "deploy": "deploy", "push": "deploy", "ship": "deploy",
    "order": "order", "purchase": "order",
}

# leading verbs that mark a tool as read-only (never a meaningful "bypass")
_READONLY_VERBS = {
    "get", "list", "search", "fetch", "read", "find", "lookup", "query",
    "retrieve", "view", "describe", "count", "show", "load", "scan", "browse",
    "check", "inspect",
}

_WORD_RE = re.compile(r"[a-zA-Z']+")


# ─── side-effect inference ──────────────────────────────────────────────────


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^a-zA-Z]+", name) if t]


def is_side_effecting(name: str, description: str = "") -> bool:
    """Heuristic: does this tool change state (vs read-only)?

    Recall-first: a tool is read-only only if its leading verb is clearly a
    read verb; everything else is treated as side-effecting so we don't miss a
    bypass. Users can override with `side_effecting=[...]`.
    """
    tokens = _name_tokens(name)
    if not tokens:
        return True
    lead = tokens[0]
    if lead in _READONLY_VERBS:
        return False
    if lead in _VERB_TO_CANON:
        return True
    # Unknown leading verb — default to side-effecting (recall-first).
    return True


def _tool_actions(name: str) -> set[str]:
    """Canonical action verbs a tool name implies (e.g. send_email → {send})."""
    out: set[str] = set()
    for tok in _name_tokens(name):
        canon = _VERB_TO_CANON.get(tok)
        if canon:
            out.add(canon)
    return out


# ─── assertion extraction ───────────────────────────────────────────────────


# Markers that, just before a completion-form verb, signal it is NOT a
# completed action: future ("will be sent", "to be created", "should be
# posted") or negation ("not sent", "never created").
_NON_COMPLETION_MARKERS = {
    "will", "ll", "to", "should", "shall", "would", "could", "can", "cannot",
    "may", "might", "must", "going", "not", "never", "yet", "cant", "wont",
    # contracted negations: "I haven't sent", "didn't create", "won't post"
    "haven't", "havent", "hasn't", "hasnt", "hadn't", "hadnt",
    "don't", "dont", "doesn't", "doesnt", "didn't", "didnt",
    "isn't", "isnt", "aren't", "arent", "wasn't", "wasnt", "weren't", "werent",
    "won't", "wouldn't", "wouldnt", "shouldn't", "shouldnt", "couldn't", "couldnt", "can't",
}

# Second-person subjects just before the verb → the USER did it, not the
# assistant ("the task you created", "the email you've sent").
_SECOND_PERSON = {"you", "you've", "youve", "your", "u", "you're", "youre"}

# Action attributed to automation or a non-assistant system anywhere in the
# text → not the assistant claiming it performed the action this turn.
_THIRD_PARTY_RE = re.compile(r"\b(scheduler|automatically|automated|automation|cron)\b")
_THIRD_PARTY_PHRASES = ("by the system", "by a system", "by an automated", "by the bot")

# Third-party SUBJECT performed the action — "the system emailed…", "the cron
# job created…". Distinct from the passive/agent forms above: here a
# non-assistant is the grammatical subject, directly before the verb. We
# suppress only the clear "{determiner} {subject} [aux] {verb}" pattern, so
# "Server updated" (object-fronted) and "the system prompt I created"
# (assistant is the subject) still fire.
_THIRD_PARTY_SUBJECTS = {
    # systems / automation
    "system", "scheduler", "service", "cron", "bot", "webhook", "pipeline",
    "server", "platform", "daemon", "worker", "backend", "integration",
    # people / roles / orgs — a non-assistant human did it
    "colleague", "coworker", "teammate", "intern", "manager", "pm", "lead",
    "admin", "client", "customer", "vendor", "contractor", "team", "user",
    "person", "staff", "engineer", "operator",
}
# bare pronoun subjects need no determiner ("they created", "someone deleted")
_THIRD_PARTY_PRONOUNS = {"they", "he", "she", "someone", "somebody", "everyone"}
_DETERMINERS = {"the", "a", "an", "our", "this", "that", "its", "their", "my", "his", "her"}
_THIRD_PARTY_AUX = {"has", "had", "just", "already", "also", "then", "successfully"}

# Communication actions one tool often satisfies interchangeably: a claim of
# "notified the team" is legitimately served by a send_email / message tool, so
# the canonical action of the claim need not match the tool's verb exactly.
_ACTION_ALIASES: dict[str, frozenset[str]] = {
    "send": frozenset({"notify", "message"}),
    "notify": frozenset({"send", "message"}),
    "message": frozenset({"send", "notify"}),
}


# coordinating words that chain verbs onto one subject ("approved and charged")
_COORD = {"and", "or", "then", "also", "plus", "but"}


def _governing_subject(tokens: list[str], i: int) -> tuple[str, int]:
    """Walk back from the verb at index i past coordinating conjunctions, other
    completion verbs, and auxiliaries to the token that governs it — the subject
    in a compound predicate like "the manager approved and charged". Returns
    (subject, its index); ("", -1) if none is within reach.
    """
    j, steps = i - 1, 0
    while j >= 0 and steps < 6:
        w = tokens[j]
        if w in _COORD or w in _COMPLETION_TO_CANON or w in _THIRD_PARTY_AUX:
            j -= 1
            steps += 1
            continue
        return w, j
    return "", -1


def _np_determiner_led(tokens: list[str], subj_idx: int) -> bool:
    """True if a determiner (incl. a possessive second-person like "your")
    introduces the noun phrase headed by tokens[subj_idx], allowing intervening
    modifier tokens — adjectives or hyphen-split fragments. "the on-call
    engineer" tokenizes to the/on/call/engineer, so the determiner sits 3 tokens
    back from the subject, not adjacent. Stops at anything that breaks a noun
    phrase (a verb, a coordinator, a future/negation marker, a pronoun).
    """
    j, steps = subj_idx - 1, 0
    while j >= 0 and steps < 4:
        w = tokens[j]
        if w in _DETERMINERS or w in _SECOND_PERSON:
            return True
        if (w in _COMPLETION_TO_CANON or w in _COORD
                or w in _NON_COMPLETION_MARKERS or w in _THIRD_PARTY_PRONOUNS):
            return False
        j -= 1
        steps += 1
    return False


def asserted_actions(text: str) -> dict[str, str]:
    """Canonical actions the text claims THE ASSISTANT completed → matched word.

    Requires completion-form verbs (past tense / participle) — that alone
    excludes intent ("I'll send"), refusal ("I can't send"), and questions.
    Plus attribution guards so we only credit the ASSISTANT, not the user or an
    automated system:
      - future/negation look-back  → "will be sent", "not created"
      - second-person look-back     → "the task YOU created"
      - third-party/automation      → "posted by the scheduler", "done automatically"
      - third-party subject          → "the system emailed", "the cron job created"
    """
    low = text.lower()
    if _THIRD_PARTY_RE.search(low) or any(p in low for p in _THIRD_PARTY_PHRASES):
        return {}  # the action is attributed to automation / another system
    tokens = _WORD_RE.findall(low)
    out: dict[str, str] = {}
    for i, word in enumerate(tokens):
        canon = _COMPLETION_TO_CANON.get(word)
        if not canon or canon in out:
            continue
        window = tokens[max(0, i - 3):i]
        if any(w in _NON_COMPLETION_MARKERS for w in window):
            continue
        if any(w in _SECOND_PERSON for w in window):
            continue  # "you created" — the user did it, not the assistant
        prev1 = tokens[i - 1] if i >= 1 else ""
        prev2 = tokens[i - 2] if i >= 2 else ""
        prev3 = tokens[i - 3] if i >= 3 else ""
        if prev1 in _THIRD_PARTY_PRONOUNS:
            continue  # "they created", "someone deleted" — not the assistant
        if prev1 in _THIRD_PARTY_AUX and prev2 in _THIRD_PARTY_PRONOUNS:
            continue  # "they just created", "someone already deleted"
        if prev1 in _THIRD_PARTY_SUBJECTS and prev2 in _DETERMINERS:
            continue  # "the system emailed", "my colleague created" — someone else did it
        if prev1 in _THIRD_PARTY_AUX and prev2 in _THIRD_PARTY_SUBJECTS and prev3 in _DETERMINERS:
            continue  # "the system has emailed", "my colleague already created"
        # compound predicate: find the governing subject past "and"/coordinated verbs
        subj, subj_idx = _governing_subject(tokens, i)
        if subj in _SECOND_PERSON or subj in _THIRD_PARTY_PRONOUNS:
            continue  # "your manager approved and charged", "she created and sent"
        if subj in _THIRD_PARTY_SUBJECTS and _np_determiner_led(tokens, subj_idx):
            continue  # "the on-call engineer posted", "my colleague created and sent"
        out[canon] = word
    return out


# ─── the detector ───────────────────────────────────────────────────────────


def detect_bypass(
    assistant_text: str | None,
    *,
    tool_calls_this_turn: Iterable[str],
    tool_names: Iterable[str],
    called_tools: Iterable[str],
    side_effecting: Iterable[str] | None = None,
    descriptions: dict[str, str] | None = None,
) -> BypassSuspicion | None:
    """Return a BypassSuspicion if this looks like claim-without-call, else None.

    Args:
        assistant_text: the assistant's natural-language output this turn.
        tool_calls_this_turn: names of tools the model actually called THIS turn.
        tool_names: all tool names registered/available.
        called_tools: names of tools called ANYWHERE in the conversation so far
            (so "I've sent it" after a real send_email earlier is NOT a bypass).
        side_effecting: explicit override of which tools change state. If None,
            inferred from names (+ descriptions).
        descriptions: optional {name: description} to aid side-effect inference.

    Never raises — returns None on anything it can't analyse (fail-open).
    """
    try:
        # 1. A tool was called this turn → nothing was bypassed.
        if any(True for _ in tool_calls_this_turn):
            return None
        if not assistant_text or not assistant_text.strip():
            return None

        # 2. What completed actions does the text claim?
        asserted = asserted_actions(assistant_text)
        if not asserted:
            return None

        # 3. Which tools are side-effecting?
        descriptions = descriptions or {}
        names = list(tool_names)
        if side_effecting is not None:
            se = set(side_effecting)
        else:
            se = {n for n in names if is_side_effecting(n, descriptions.get(n, ""))}

        called = set(called_tools)

        # Actions already satisfied by a tool that ACTUALLY ran (incl. aliases).
        # A claim of "sent the email" is backed by a real send_email call even
        # if a sibling comms tool (send_sms) shares the same canonical "send"
        # action — without this, the sibling gets flagged as a false bypass.
        covered: set[str] = set()
        for name in called:
            for a in _tool_actions(name):
                covered.add(a)
                covered |= _ACTION_ALIASES.get(a, frozenset())

        # 4. A side-effecting tool whose action was claimed but never called —
        #    and whose action no called tool already covers.
        for name in names:
            if name not in se or name in called:
                continue
            actions = _tool_actions(name)
            # Match an asserted action to this tool — directly, or via a
            # communication-cluster alias (notify ≈ send ≈ message), so
            # "notified the team" matches a send_email tool.
            for action in sorted(asserted):
                if action in covered:
                    continue  # a tool that ran already satisfies this claim
                if action in actions or (_ACTION_ALIASES.get(action, frozenset()) & actions):
                    return BypassSuspicion(
                        tool=name,
                        action=action,
                        evidence=asserted[action],
                        reason=(
                            f"assistant said {asserted[action]!r} (a completed {action!r} "
                            f"action) but never called {name!r}"
                        ),
                    )
        return None
    except Exception:
        return None  # fail-open: detection must never break the host
