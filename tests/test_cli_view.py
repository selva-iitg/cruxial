"""CLI: `cruxial view` over the action ledger + the demo's action-layer section."""
from __future__ import annotations

from pathlib import Path

from cruxial.actions import ActionRegistry
from cruxial.cli import main
from cruxial.core import guard
from cruxial.ledger import Ledger
from cruxial.receipts import ReceiptRegistry, id_field
from cruxial.telemetry import SqliteSink


def _ledger_db(tmp_path: Path) -> Path:
    db = tmp_path / "t.sqlite"
    ar = ActionRegistry()
    ar.mark_action("send_email")
    rr = ReceiptRegistry()
    rr.register("send_email", id_field("message_id", kind="email"))
    guard(
        {"send_email": {"type": "object"}},
        {"send_email": lambda **k: {"message_id": "m1"}},
        sink=SqliteSink(db), receipt_registry=rr, action_registry=ar,
    ).execute("send_email", {"to": "a@b.com"})

    ar2 = ActionRegistry()
    ar2.mark_action("charge")
    guard(
        {"charge": {"type": "object"}},
        {"charge": lambda **k: {"queued": True}},  # ran, no receipt → unknown
        sink=SqliteSink(db), receipt_registry=ReceiptRegistry(), action_registry=ar2,
    ).execute("charge", {"amt": 5})
    return db


def test_view_missing_db_errors(tmp_path, capsys):
    rc = main(["view", "--db", str(tmp_path / "nope.sqlite")])
    assert rc == 1
    assert "no telemetry database" in capsys.readouterr().err


def test_view_dashboard(tmp_path, capsys):
    db = _ledger_db(tmp_path)
    rc = main(["view", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "action ledger" in out
    assert "silent failures" in out
    assert "send_email" in out and "charge" in out
    assert "posted" in out and "unknown" in out
    assert "protection" in out  # the per-tool coverage section


def test_view_single_op_trace(tmp_path, capsys):
    db = _ledger_db(tmp_path)
    unknown_op = next(o for o in Ledger(SqliteSink(db)).recent() if o.state == "unknown")
    rc = main(["view", unknown_op.op_id, "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert unknown_op.op_id in out and "charge" in out
    assert "cruxial.receipt" in out  # the guided hint, persisted in the op note


def test_view_empty_ledger(tmp_path, capsys):
    db = tmp_path / "empty.sqlite"
    SqliteSink(db).close()  # creates the schema, no operations
    rc = main(["view", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0 and "no operations recorded yet" in out


def test_demo_shows_action_layer(capsys):
    rc = main(["demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "action layer" in out
    assert "UNKNOWN" in out          # the no-receipt case
    assert "cruxial view" in out     # next-steps pointer
