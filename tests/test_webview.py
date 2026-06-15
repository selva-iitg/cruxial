"""`cruxial view --web` — the zero-dep local web dashboard over the ledger.

Starts the real stdlib server on an ephemeral port and hits it over HTTP, plus
unit-checks the JSON payload builders. No new dependencies.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from cruxial.actions import ActionRegistry
from cruxial.core import guard
from cruxial.ledger import Ledger
from cruxial.receipts import ReceiptRegistry, id_field
from cruxial.telemetry import SqliteSink
from cruxial.webview import _make_handler, _op_to_dict, _state_payload, _PAGE


def _ledger_db(tmp_path: Path) -> Path:
    db = tmp_path / "t.sqlite"
    ar = ActionRegistry(); ar.mark_action("send_email")
    rr = ReceiptRegistry(); rr.register("send_email", id_field("message_id", kind="email"))
    guard({"send_email": {"type": "object"}},
          {"send_email": lambda **k: {"message_id": "m1"}},
          sink=SqliteSink(db), receipt_registry=rr, action_registry=ar,
          config=__import__("cruxial").GuardConfig(actor="bot")).execute("send_email", {"to": "a@b"})
    ar2 = ActionRegistry(); ar2.mark_action("charge")
    guard({"charge": {"type": "object"}},
          {"charge": lambda **k: {"queued": True}},
          sink=SqliteSink(db), receipt_registry=ReceiptRegistry(),
          action_registry=ar2).execute("charge", {"amt": 5})
    return db


def test_state_payload(tmp_path):
    led = Ledger(SqliteSink(_ledger_db(tmp_path)))
    p = _state_payload(led, Path("/tmp/x"))
    assert p["counts"]["total"] == 2
    assert p["counts"]["posted"] == 1 and p["counts"]["unknown"] == 1
    tools = {o["tool"] for o in p["operations"]}
    assert tools == {"send_email", "charge"}
    # protection registry persisted at guard() construction
    prot = {x["tool"]: x for x in p["protection"]}
    assert prot["send_email"]["is_action"] and prot["send_email"]["has_receipt"]
    assert prot["charge"]["is_action"] and not prot["charge"]["has_receipt"]


def test_op_to_dict_shapes_receipt():
    from cruxial.types import Operation, Receipt
    op = Operation(op_id="op_1", tool="send_email", state="posted", ts_intent="t",
                   receipt=Receipt(ok=True, id="m1", kind="email"))
    d = _op_to_dict(op)
    assert d["receipt"] == {"ok": True, "id": "m1", "kind": "email"}


def test_page_has_api_hooks():
    assert "action ledger" in _PAGE
    assert "/api/state" in _PAGE and "/api/op/" in _PAGE
    assert "silent failures" in _PAGE.lower()
    assert "Protection" in _PAGE


def test_server_serves_page_and_api(tmp_path):
    db = _ledger_db(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(db))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/", timeout=3).read().decode()
        assert "cruxial" in page and "action ledger" in page

        state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=3).read())
        assert state["counts"]["unknown"] >= 1
        assert any(o["tool"] == "charge" and o["state"] == "unknown" for o in state["operations"])

        op_id = state["operations"][0]["op_id"]
        op = json.loads(urllib.request.urlopen(base + f"/api/op/{op_id}", timeout=3).read())
        assert op["op_id"] == op_id

        # unknown op id → 404
        try:
            urllib.request.urlopen(base + "/api/op/nope", timeout=3)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_server_reflects_replaced_db(tmp_path):
    # Regression: the viewer re-opens the db per request. A fresh agent run that
    # removes + recreates the file must NOT leave the server pinned to the old,
    # unlinked inode (which would silently serve stale data).
    db = _ledger_db(tmp_path)                                   # 2 ops
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(db))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        s1 = json.loads(urllib.request.urlopen(base + "/api/state", timeout=3).read())
        assert s1["counts"]["total"] == 2

        # simulate a fresh run: replace the file in place with a single op
        db.unlink()
        ar = ActionRegistry(); ar.mark_action("send_email")
        rr = ReceiptRegistry(); rr.register("send_email", id_field("message_id", kind="email"))
        guard({"send_email": {"type": "object"}},
              {"send_email": lambda **k: {"message_id": "m9"}},
              sink=SqliteSink(db), receipt_registry=rr, action_registry=ar,
              config=__import__("cruxial").GuardConfig(actor="bot")).execute("send_email", {"to": "x@y"})

        s2 = json.loads(urllib.request.urlopen(base + "/api/state", timeout=3).read())
        assert s2["counts"]["total"] == 1      # fresh read of the new file, not the stale 2
    finally:
        httpd.shutdown()
        httpd.server_close()
