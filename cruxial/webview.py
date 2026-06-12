"""Local web viewer for the action ledger — `cruxial view --web`.

Zero new dependencies: Python's stdlib http.server renders a single
self-contained page over the same sqlite ledger that `cruxial view` reads. The
page polls a small JSON API so it stays live while your agent runs. Binds to
127.0.0.1 only, read-only, no auth — a local dev tool, not a hosted service.
"""

from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cruxial.ledger import Ledger
from cruxial.telemetry import SqliteSink


def _op_to_dict(op: Any) -> dict:
    r = op.receipt
    return {
        "op_id": op.op_id, "tool": op.tool, "state": op.state,
        "actor": op.actor, "ts_intent": op.ts_intent, "ts_resolved": op.ts_resolved,
        "note": op.note,
        "policy": (op.policy or {}).get("decision"),
        "receipt": ({"ok": r.ok, "id": r.id, "kind": r.kind} if r else None),
    }


def _state_payload(led: Ledger, db_path: Path) -> dict:
    counts = led.state_counts()
    posted = counts.get("posted", 0) + counts.get("sent", 0) + counts.get("queued", 0)
    return {
        "db": str(db_path),
        "counts": {
            "total": sum(counts.values()),
            "posted": posted,
            "unknown": counts.get("unknown", 0),
            "needs_review": counts.get("needs_review", 0),
            "failed": counts.get("failed", 0),
        },
        "operations": [_op_to_dict(o) for o in led.recent(200)],
    }


def _make_handler(led: Ledger, db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(_state_payload(led, db_path))
            elif path.startswith("/api/op/"):
                op = led.get(path[len("/api/op/"):])
                self._json(_op_to_dict(op) if op else {"error": "not found"}, 200 if op else 404)
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args: Any) -> None:  # silence per-request logging
            pass

    return Handler


def serve(db_path: Path, port: int = 7878, open_browser: bool = True) -> int:
    if not db_path.exists():
        print(f"error: no telemetry database at {db_path}")
        return 1
    sink = SqliteSink(db_path)
    led = Ledger(sink)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(led, db_path))
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"cruxial · action ledger → {url}   (ctrl-c to stop)", flush=True)
    print(f"  db: {db_path}", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
        sink.close()
    return 0


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>cruxial · action ledger</title>
<style>
:root{--bg:#0b0e14;--panel:#11151f;--border:#1e2533;--text:#c9d3e6;--muted:#6b7689;
--green:#34d39a;--amber:#f5b53d;--red:#f0728f;--accent:#34d39a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 22px 80px}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:22px}
h1{font-size:18px;margin:0;font-weight:800;letter-spacing:.04em}h1 span{color:var(--accent)}
.live{font-size:11px;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
.card .n{font-size:28px;font-weight:800}.card .l{font-size:11px;color:var(--muted);
text-transform:uppercase;letter-spacing:.08em;margin-top:4px}
.card.sf{border-color:rgba(240,114,143,.35)}.card.sf .n{color:var(--red)}
.card.ok .n{color:var(--green)}.card.rv .n{color:var(--amber)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-size:10.5px;text-transform:uppercase;
letter-spacing:.06em;padding:8px 10px;border-bottom:1px solid var(--border)}
td{padding:9px 10px;border-bottom:1px solid rgba(30,37,51,.6)}
tr.row{cursor:pointer}tr.row:hover{background:rgba(255,255,255,.02)}
.pill{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px}
.s-posted,.s-sent,.s-queued{background:rgba(52,211,154,.12);color:var(--green)}
.s-unknown,.s-failed{background:rgba(240,114,143,.12);color:var(--red)}
.s-needs_review{background:rgba(245,181,61,.12);color:var(--amber)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
.empty{color:var(--muted);padding:40px;text-align:center}
#panel{position:fixed;top:0;right:0;height:100%;width:430px;max-width:92vw;background:var(--panel);
border-left:1px solid var(--border);transform:translateX(100%);transition:transform .18s;padding:24px;overflow:auto}
#panel.open{transform:none}#panel h2{font-size:14px;margin:0 0 16px;word-break:break-all}
#panel .x{position:absolute;top:18px;right:20px;cursor:pointer;color:var(--muted)}
.kv{display:grid;grid-template-columns:84px 1fr;gap:7px 10px;font-size:13px}
.kv .k{color:var(--muted)}.note{margin-top:16px;padding:12px;background:var(--bg);
border-radius:8px;font-size:12.5px;color:var(--amber);line-height:1.45}
footer{margin-top:20px;font-size:11px;color:var(--muted)}
</style></head><body><div class="wrap">
<header><h1>crux<span>ial</span> · action ledger</h1><span class="live" id="live">● live</span></header>
<div class="cards">
<div class="card ok"><div class="n" id="c-posted">—</div><div class="l">confirmed (receipt)</div></div>
<div class="card sf"><div class="n" id="c-unknown">—</div><div class="l">⚠ silent failures</div></div>
<div class="card rv"><div class="n" id="c-review">—</div><div class="l">needs review</div></div>
<div class="card"><div class="n" id="c-total">—</div><div class="l">operations</div></div>
</div>
<table><thead><tr><th>tool</th><th>state</th><th>receipt</th><th>actor</th><th>when</th></tr></thead>
<tbody id="rows"></tbody></table>
<div class="empty" id="empty" style="display:none">no operations yet — mark a tool
<span class="mono">@cruxial.action</span> and run your app.</div>
<footer id="foot"></footer></div>
<div id="panel"><span class="x" onclick="closePanel()">✕</span><div id="panel-body"></div></div>
<script>
const LBL={posted:'posted ✓',sent:'sent ✓',queued:'queued',failed:'failed ✗',
needs_review:'needs review ⚠',unknown:'unknown — not confirmed ✗'};
function ago(iso){if(!iso)return'—';const d=(Date.now()-new Date(iso).getTime())/1000|0;
if(d<60)return d+'s ago';if(d<3600)return(d/60|0)+'m ago';if(d<86400)return(d/3600|0)+'h ago';return(d/86400|0)+'d ago';}
function esc(s){return(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function pill(s){return '<span class="pill s-'+esc(s)+'">'+(LBL[s]||esc(s))+'</span>';}
async function refresh(){try{const d=await(await fetch('/api/state')).json();
c('c-posted',d.counts.posted);c('c-unknown',d.counts.unknown);c('c-review',d.counts.needs_review);c('c-total',d.counts.total);
const rows=document.getElementById('rows'),empty=document.getElementById('empty');
if(!d.operations.length){empty.style.display='block';rows.innerHTML='';}
else{empty.style.display='none';rows.innerHTML=d.operations.map(o=>'<tr class="row" onclick="openOp(\\''+o.op_id+'\\')">'
+'<td>'+esc(o.tool)+'</td><td>'+pill(o.state)+'</td><td class="mono">'+(o.receipt&&o.receipt.id?esc(o.receipt.id):'—')
+'</td><td class="mono">'+esc(o.actor||'—')+'</td><td class="mono">'+ago(o.ts_intent)+'</td></tr>').join('');}
document.getElementById('foot').textContent='db: '+d.db;document.getElementById('live').textContent='● live';
}catch(e){document.getElementById('live').textContent='○ disconnected';}}
function c(id,v){document.getElementById(id).textContent=v;}
async function openOp(id){const o=await(await fetch('/api/op/'+id)).json();
const rid=o.receipt&&o.receipt.id?esc(o.receipt.id):'—';
document.getElementById('panel-body').innerHTML='<h2>operation '+esc(o.op_id)+'</h2><div class="kv">'
+'<div class="k">tool</div><div>'+esc(o.tool)+'</div>'
+'<div class="k">state</div><div>'+pill(o.state)+'</div>'
+'<div class="k">actor</div><div class="mono">'+esc(o.actor||'—')+'</div>'
+'<div class="k">intent</div><div class="mono">'+esc(o.ts_intent||'—')+'</div>'
+'<div class="k">resolved</div><div class="mono">'+esc(o.ts_resolved||'—')+'</div>'
+'<div class="k">policy</div><div class="mono">'+esc(o.policy||'—')+'</div>'
+'<div class="k">receipt</div><div class="mono">'+rid+'</div></div>'
+(o.note?'<div class="note">'+esc(o.note)+'</div>':'');
document.getElementById('panel').classList.add('open');}
function closePanel(){document.getElementById('panel').classList.remove('open');}
refresh();setInterval(refresh,2000);
</script></body></html>"""
