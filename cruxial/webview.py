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
        "note": op.note, "claim": op.claim,
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
        "protection": led.protection(),
    }


def _read(db_path: Path, fn):
    """Run `fn(ledger)` against a FRESH sqlite reader, then close it.

    The viewer must open the ledger per request, not hold one connection: an
    agent run that replaces the db file (e.g. a fresh run truncates/recreates
    it) leaves a long-lived connection pinned to the old, unlinked inode — so
    the dashboard would silently keep serving stale data. Re-opening each
    request is cheap for a local tool and always reflects the current file.
    Fail-open: a transient read error (file mid-swap) yields None.
    """
    sink = None
    try:
        sink = SqliteSink(db_path)
        return fn(Ledger(sink))
    except Exception:
        return None
    finally:
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass


_EMPTY_STATE = {"db": "", "counts": {"total": 0, "posted": 0, "unknown": 0,
                                     "needs_review": 0, "failed": 0},
                "operations": [], "protection": []}


def _make_handler(db_path: Path):
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
                payload = _read(db_path, lambda led: _state_payload(led, db_path))
                self._json(payload if payload is not None else {**_EMPTY_STATE, "db": str(db_path)})
            elif path.startswith("/api/op/"):
                op_id = path[len("/api/op/"):]
                op = _read(db_path, lambda led: led.get(op_id))
                self._json(_op_to_dict(op) if op else {"error": "not found"}, 200 if op else 404)
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args: Any) -> None:  # silence per-request logging
            pass

    return Handler


def _bind(handler_cls: type, port: int, tries: int = 20) -> "ThreadingHTTPServer | None":
    """Bind 127.0.0.1 starting at `port`, stepping to the next port when one is
    in use — so a second `cruxial view --web` finds a free port instead of
    crashing on EADDRINUSE. Returns the server, or None if the whole range is
    taken. The actual port is on `server.server_address[1]`."""
    for candidate in range(port, port + tries):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler_cls)
        except (OSError, OverflowError):  # in use, or stepped past 65535
            continue
    return None


def serve(db_path: Path, port: int = 7878, open_browser: bool = True) -> int:
    if not db_path.exists():
        print(f"error: no telemetry database at {db_path}")
        return 1
    httpd = _bind(_make_handler(db_path), port)
    if httpd is None:
        print(f"error: no free port in {port}..{port + 19}. Is another `cruxial view --web` already running?")
        return 1
    bound = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound}"
    if bound != port:
        print(f"note: port {port} was busy, using {bound}.", flush=True)
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
    return 0


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cruxial · action ledger</title>
<style>
:root{
 --bg:#08090b; --surface:#101114; --surface-2:#15171b; --hover:#1a1d22;
 --border:rgba(255,255,255,.06); --border-2:rgba(255,255,255,.10);
 --text:#eceef2; --muted:#8b8f99; --faint:#5b606b;
 --green:#3ecf8e; --green-t:rgba(62,207,142,.11); --green-b:rgba(62,207,142,.24);
 --red:#f1577a; --red-t:rgba(241,87,122,.10); --red-b:rgba(241,87,122,.26);
 --amber:#f5b53d; --amber-t:rgba(245,181,61,.10); --amber-b:rgba(245,181,61,.26);
 --mono:ui-monospace,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{background:var(--bg);color:var(--text);
 font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,sans-serif;
 background-image:radial-gradient(900px 360px at 50% -120px,rgba(62,207,142,.06),transparent 70%);}
.bar{position:sticky;top:0;z-index:20;background:rgba(8,9,11,.72);backdrop-filter:blur(12px);
 border-bottom:1px solid var(--border)}
.bar-in,.wrap{max-width:1080px;margin:0 auto;padding:0 26px}
.bar-in{height:56px;display:flex;align-items:center;justify-content:space-between}
.brand{font-size:14.5px;font-weight:680;letter-spacing:-.01em}
.brand span{color:var(--green)}
.brand .dot{color:var(--faint);margin:0 8px;font-weight:400}
.live{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);
 background:var(--surface);border:1px solid var(--border);padding:5px 11px;border-radius:999px}
.live i{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(62,207,142,.5);
 animation:pulse 2s infinite}
.live.off i{background:var(--faint);animation:none;box-shadow:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(62,207,142,.45)}70%{box-shadow:0 0 0 6px rgba(62,207,142,0)}100%{box-shadow:0 0 0 0 rgba(62,207,142,0)}}
.wrap{padding-top:30px;padding-bottom:90px}
.hero{font-size:19px;font-weight:600;letter-spacing:-.015em;margin:2px 2px 22px;line-height:1.45;min-height:1px}
.hero .em{font-weight:780}
.hero.ok .em{color:var(--green)} .hero.bad .em{color:var(--red)} .hero.warn .em{color:var(--amber)}
.hero .sub2{display:block;font-size:13px;font-weight:400;color:var(--muted);margin-top:5px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:30px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px;
 transition:border-color .15s,transform .15s,background .15s}
.card:hover{border-color:var(--border-2);transform:translateY(-1px)}
.card .n{font-size:30px;font-weight:680;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1}
.card .l{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);
 text-transform:uppercase;letter-spacing:.07em;margin-top:11px;font-weight:560}
.card.ok .n{color:var(--green)} .card.rv .n{color:var(--amber)}
.card.sf{border-color:var(--red-b);background:linear-gradient(180deg,rgba(241,87,122,.05),transparent 60%),var(--surface)}
.card.sf .n{color:var(--red)} .card.sf .l{color:#c97b8c}
.card[data-filter]{cursor:pointer}
.card.active{border-color:var(--border-2);box-shadow:inset 0 0 0 1px var(--border-2)}
.card.sf.active{border-color:var(--red);box-shadow:inset 0 0 0 1px rgba(241,87,122,.45)}
.afail{color:#ff8aa1;font-weight:560} .link{color:var(--green);cursor:pointer}
tr.fail-row td:first-child{box-shadow:inset 3px 0 0 var(--red)}
.toolbar{display:flex;align-items:baseline;justify-content:space-between;margin:0 2px 10px}
.toolbar h2{font-size:13px;font-weight:600;letter-spacing:-.01em}
.toolbar .sub{font-size:12px;color:var(--faint)}
.panel-tbl{background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden}
table{width:100%;border-collapse:collapse}
thead th{text-align:left;font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em;
 font-weight:600;padding:12px 18px;background:var(--surface-2);border-bottom:1px solid var(--border)}
tbody td{padding:13px 18px;border-bottom:1px solid var(--border);font-size:13.5px;vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr.row{cursor:pointer;transition:background .12s}
tbody tr.row:hover{background:var(--hover)}
td.tool{font-weight:550;letter-spacing:-.005em}
.mono{font-family:var(--mono);font-size:12.5px;letter-spacing:-.01em}
.muted{color:var(--muted)} .dash{color:var(--faint)}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:560;
 padding:3px 10px 3px 9px;border-radius:999px;border:1px solid transparent;white-space:nowrap}
.badge i{width:6px;height:6px;border-radius:50%}
.badge.g{color:#74e3ad;background:var(--green-t);border-color:var(--green-b)} .badge.g i{background:var(--green)}
.badge.r{color:#ff8aa1;background:var(--red-t);border-color:var(--red-b)} .badge.r i{background:var(--red)}
.badge.a{color:#f6c869;background:var(--amber-t);border-color:var(--amber-b)} .badge.a i{background:var(--amber)}
.tag{display:inline-block;font-size:11.5px;font-weight:560;padding:2px 9px;border-radius:6px;border:1px solid transparent}
.tag.act{color:#74e3ad;background:var(--green-t);border-color:var(--green-b)}
.tag.ro{color:var(--muted);background:var(--surface-2);border-color:var(--border)}
.rcpt{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:540}
.rcpt.ok{color:#74e3ad} .rcpt.warn{color:#f6c869}
.chk{display:inline-block;font-size:11.5px;color:var(--text);background:var(--surface-2);
 border:1px solid var(--border);padding:1px 8px;border-radius:6px;margin:1px 4px 1px 0}
.empty{padding:54px;text-align:center;color:var(--muted)}
.empty code{font-family:var(--mono);color:var(--text);background:var(--surface-2);padding:2px 7px;border-radius:6px}
.foot{margin-top:18px;font-size:11.5px;color:var(--faint);font-family:var(--mono)}
#backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .18s;z-index:30}
#backdrop.open{opacity:1;pointer-events:auto}
#panel{position:fixed;top:0;right:0;height:100%;width:460px;max-width:94vw;z-index:31;
 background:var(--surface);border-left:1px solid var(--border-2);box-shadow:-24px 0 60px rgba(0,0,0,.4);
 transform:translateX(100%);transition:transform .2s cubic-bezier(.4,0,.2,1);overflow:auto}
#panel.open{transform:none}
.ph{display:flex;align-items:center;justify-content:space-between;padding:20px 24px;border-bottom:1px solid var(--border)}
.ph .t{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}
.ph .id{font-family:var(--mono);font-size:13px;margin-top:3px;display:flex;align-items:center;gap:8px}
.ph .x{cursor:pointer;color:var(--muted);font-size:18px;line-height:1;padding:4px;border-radius:6px}
.ph .x:hover{color:var(--text);background:var(--surface-2)}
.copy{cursor:pointer;font-size:10.5px;color:var(--muted);background:var(--surface-2);border:1px solid var(--border);
 padding:1px 7px;border-radius:6px;font-family:system-ui,sans-serif}
.copy:hover{color:var(--text);border-color:var(--border-2)}
.pb{padding:22px 24px}
.kv{display:grid;grid-template-columns:84px 1fr;gap:13px 14px;align-items:center;font-size:13px}
.kv .k{color:var(--muted);font-size:12px}
.kv .v{color:var(--text)}
.note{margin-top:22px;padding:14px 16px;background:var(--amber-t);border:1px solid var(--amber-b);
 border-radius:10px;font-size:12.5px;line-height:1.5;color:#f3cf8a}
.note.r{background:var(--red-t);border-color:var(--red-b);color:#ffaabb}
.fix{margin-top:22px}
.fix-h{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--muted);margin-bottom:9px}
.code{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:14px 16px;
 font-family:var(--mono);font-size:12px;line-height:1.65;color:var(--text);white-space:pre;overflow-x:auto}
.sd{margin-top:20px;border:1px solid var(--border);border-radius:10px;overflow:hidden}
.sd-row{display:flex;gap:14px;padding:12px 15px;font-size:13px;align-items:center}
.sd-row+.sd-row{border-top:1px solid var(--border)}
.sd-k{flex:0 0 50px;color:var(--muted);font-size:12px}
.sd-v.said{color:#f6c869}
@media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="bar"><div class="bar-in">
 <div class="brand">crux<span>ial</span><span class="dot">·</span>action ledger</div>
 <span class="live" id="live"><i></i>live</span>
</div></div>
<div class="wrap">
 <div class="hero" id="hero"></div>
 <div class="cards">
  <div class="card ok" data-filter="confirmed"><div class="n" id="c-posted">—</div><div class="l" title="actions backed by a real receipt">Confirmed · receipt</div></div>
  <div class="card sf" data-filter="unknown"><div class="n" id="c-unknown">—</div><div class="l" title="claimed or expected, but no receipt — the agent's 'done' couldn't be proven">⚠ Silent failures</div></div>
  <div class="card rv" data-filter="needs_review"><div class="n" id="c-review">—</div><div class="l" title="a verify rule halted the action for human review">Needs review</div></div>
  <div class="card" data-filter="all"><div class="n" id="c-total">—</div><div class="l" title="total side-effecting operations recorded">Operations</div></div>
 </div>
 <div id="prot-section">
  <div class="toolbar" style="margin-top:2px"><h2>Protection</h2><span class="sub" id="psub"></span></div>
  <div class="panel-tbl" style="margin-bottom:28px">
   <table><thead><tr><th>Tool</th><th>Type</th><th>Receipt</th><th>Checks</th><th>Activity</th></tr></thead>
   <tbody id="prot"></tbody></table>
  </div>
 </div>
 <div class="toolbar"><h2>Operations</h2><span class="sub" id="sub"></span></div>
 <div class="panel-tbl">
  <table><thead><tr><th>Tool</th><th>State</th><th>Receipt</th><th>Actor</th><th>When</th></tr></thead>
  <tbody id="rows"></tbody></table>
  <div class="empty" id="empty" style="display:none">No operations yet — mark a tool
   <code>@cruxial.action</code> and run your app.</div>
 </div>
 <div class="foot" id="foot"></div>
</div>
<div id="backdrop"></div>
<aside id="panel"><div id="panel-body"></div></aside>
<script>
const LBL={posted:'posted',sent:'sent',queued:'queued',failed:'failed',needs_review:'needs review',unknown:'unknown'};
function grp(s){return(s==='posted'||s==='sent'||s==='queued')?'g':(s==='needs_review')?'a':'r';}
function esc(s){return(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function badge(s){return '<span class="badge '+grp(s)+'"><i></i>'+(LBL[s]||esc(s))+'</span>';}
function ago(iso){if(!iso)return'—';const d=(Date.now()-new Date(iso).getTime())/1000|0;
 if(d<5)return'just now';if(d<60)return d+'s ago';if(d<3600)return(d/60|0)+'m ago';
 if(d<86400)return(d/3600|0)+'h ago';return(d/86400|0)+'d ago';}
function when(iso){if(!iso)return'—';try{return new Date(iso).toLocaleString();}catch(e){return iso;}}
function c(id,v){document.getElementById(id).textContent=v;}
let LAST=null, FILTER=null, FIXSNIP='';
function matchFilter(st){if(!FILTER)return true;
 if(FILTER==='confirmed')return st==='posted'||st==='sent'||st==='queued';return st===FILTER;}
function setFilter(f){FILTER=f;render();}
function render(){
 if(!LAST)return; const d=LAST;
 c('c-posted',d.counts.posted);c('c-unknown',d.counts.unknown);
 c('c-review',d.counts.needs_review);c('c-total',d.counts.total);
 (function(){const h=document.getElementById('hero'),t=d.counts.total,p=d.counts.posted,
  u=d.counts.unknown,rv=d.counts.needs_review;
  if(!t){h.className='hero';h.innerHTML='';}
  else if(u>0){h.className='hero bad';
   h.innerHTML='<span class="em">'+u+'</span> of '+t+' agent action'+(t===1?'':'s')+" weren't confirmed."
    +'<span class="sub2">said \\u201cdone\\u201d — no receipt to prove it · '+p+' confirmed'
    +(rv?' · '+rv+' needs review':'')+'</span>';}
  else if(rv>0){h.className='hero warn';
   h.innerHTML='<span class="em">'+rv+'</span> action'+(rv===1?'':'s')+' need review · '+p+' confirmed.'
    +'<span class="sub2">no silent failures</span>';}
  else{h.className='hero ok';
   h.innerHTML='<span class="em">All '+t+' action'+(t===1?'':'s')+' confirmed</span> by a receipt.'
    +'<span class="sub2">no silent failures — every \\u201cdone\\u201d is backed by proof</span>';}})();
 document.querySelectorAll('.card[data-filter]').forEach(el=>
  el.classList.toggle('active', !!FILTER && el.dataset.filter===FILTER));
 // per-tool activity — cross-links Protection ↔ Operations
 const act={};
 (d.operations||[]).forEach(o=>{const a=act[o.tool]||(act[o.tool]={calls:0,unknown:0});
  a.calls++; if(o.state==='unknown')a.unknown++;});
 const ops=(d.operations||[]).filter(o=>matchFilter(o.state));
 const rows=document.getElementById('rows'),empty=document.getElementById('empty');
 if(!d.operations.length){empty.style.display='block';
  empty.innerHTML='No operations yet — mark a tool <code>@cruxial.action</code> and run your app.';rows.innerHTML='';}
 else if(!ops.length){empty.style.display='block';
  empty.innerHTML='No operations match this filter. <span class="link" onclick="setFilter(null)">clear</span>';rows.innerHTML='';}
 else{empty.style.display='none';rows.innerHTML=ops.map(o=>
  '<tr class="row" data-op="'+esc(o.op_id)+'">'
  +'<td class="tool">'+esc(o.tool)+'</td><td>'+badge(o.state)+'</td>'
  +'<td class="mono">'+(o.receipt&&o.receipt.id?esc(o.receipt.id):'<span class="dash">—</span>')+'</td>'
  +'<td class="mono muted">'+esc(o.actor||'—')+'</td>'
  +'<td class="mono muted" title="'+esc(when(o.ts_intent))+'">'+ago(o.ts_intent)+'</td></tr>').join('');}
 document.getElementById('sub').textContent = d.operations.length
   ? (FILTER? ops.length+' of '+d.operations.length : d.operations.length+' shown') : '';
 // protection + activity + failing highlight
 const prot=document.getElementById('prot');
 const hasProt=d.protection&&d.protection.length;
 document.getElementById('prot-section').style.display=hasProt?'':'none';
 if(hasProt){
  document.getElementById('psub').textContent=d.protection.length+' tools';
  prot.innerHTML=d.protection.map(p=>{
   const a=act[p.tool];
   const activity=!a?'<span class="muted">—</span>'
    : a.calls+' call'+(a.calls===1?'':'s')+(a.unknown?' · <span class="afail">'+a.unknown+' unknown</span>':'');
   if(!p.is_action)return '<tr><td class="tool">'+esc(p.tool)+'</td>'
     +'<td><span class="tag ro">read-only</span></td><td class="muted">—</td>'
     +'<td class="muted">—</td><td>'+activity+'</td></tr>';
   const fail=!p.has_receipt && a && a.unknown>0;
   const rc=p.has_receipt?'<span class="rcpt ok">✓ adapter</span>':'<span class="rcpt warn">⚠ no adapter</span>';
   let chk;
   if(p.verify_labels&&p.verify_labels.length)
     chk=p.verify_labels.map(l=>'<span class="chk">'+esc(l)+'</span>').join('');
   else if(p.verify_count>0)
     chk='<span class="mono muted">'+p.verify_count+' check'+(p.verify_count===1?'':'s')+'</span>';
   else chk='<span class="muted">none</span>';
   return '<tr'+(fail?' class="fail-row"':'')+'><td class="tool">'+esc(p.tool)+'</td>'
     +'<td><span class="tag act">action</span></td><td>'+rc+'</td><td>'+chk+'</td><td>'+activity+'</td></tr>';
  }).join('');
 }else{prot.innerHTML='';document.getElementById('psub').textContent='';}
 document.getElementById('foot').textContent='db · '+d.db;
}
async function refresh(){try{
 LAST=await(await fetch('/api/state')).json();
 document.getElementById('live').className='live'; render();
}catch(e){document.getElementById('live').className='live off';}}
document.querySelectorAll('.card[data-filter]').forEach(el=>el.addEventListener('click',()=>{
 const f=el.dataset.filter; FILTER=(f==='all')?null:(FILTER===f?null:f); render();}));
document.getElementById('rows').addEventListener('click',e=>{
 const tr=e.target.closest('tr[data-op]');if(tr)openOp(tr.dataset.op);});
async function openOp(id){const o=await(await fetch('/api/op/'+id)).json();
 const rid=o.receipt&&o.receipt.id?esc(o.receipt.id):'<span class="dash">—</span>';
 const nr=grp(o.state)==='r';
 const prot=(LAST&&LAST.protection||[]).find(p=>p.tool===o.tool);
 const needFix=o.state==='unknown'&&prot&&prot.is_action&&!prot.has_receipt;
 let extra='';
 if(needFix){
  FIXSNIP='@cruxial.receipt("'+o.tool+'")\\ndef _(raw):\\n    return cruxial.Receipt(ok=True, id=raw["id"])  # read the real proof';
  extra='<div class="fix"><div class="fix-h">Fix · add a receipt adapter'
   +'<span class="copy" onclick="copyFix(this)">copy</span></div>'
   +'<div class="code">'+esc(FIXSNIP)+'</div></div>';
 }else if(o.note){extra='<div class="note'+(nr?' r':'')+'">'+esc(o.note)+'</div>';}
 const sd=o.claim?('<div class="sd">'
  +'<div class="sd-row"><span class="sd-k">🗣 said</span><span class="sd-v said">'+esc(o.claim)+'</span></div>'
  +'<div class="sd-row"><span class="sd-k">🔎 did</span><span class="sd-v">'+badge(o.state)
   +(o.receipt&&o.receipt.id?' · receipt '+esc(o.receipt.id):' · no receipt')+'</span></div></div>'):'';
 document.getElementById('panel-body').innerHTML=
  '<div class="ph"><div><div class="t">Operation</div>'
  +'<div class="id">'+esc(o.op_id)+'<span class="copy" id="cp" onclick="cp(\\''+esc(o.op_id)+'\\')">copy</span></div></div>'
  +'<div class="x" onclick="closePanel()">✕</div></div>'
  +'<div class="pb"><div class="kv">'
  +'<div class="k">tool</div><div class="v">'+esc(o.tool)+'</div>'
  +'<div class="k">state</div><div class="v">'+badge(o.state)+'</div>'
  +'<div class="k">actor</div><div class="v mono">'+esc(o.actor||'—')+'</div>'
  +'<div class="k">intent</div><div class="v mono muted">'+when(o.ts_intent)+'</div>'
  +'<div class="k">resolved</div><div class="v mono muted">'+when(o.ts_resolved)+'</div>'
  +'<div class="k">policy</div><div class="v mono muted">'+esc(o.policy||'—')+'</div>'
  +'<div class="k">receipt</div><div class="v mono">'+rid+'</div>'
  +'</div>'+sd+extra+'</div>';
 document.getElementById('panel').classList.add('open');
 document.getElementById('backdrop').classList.add('open');}
function closePanel(){document.getElementById('panel').classList.remove('open');
 document.getElementById('backdrop').classList.remove('open');}
function cp(id){try{navigator.clipboard.writeText(id);const b=document.getElementById('cp');
 b.textContent='copied';setTimeout(()=>b.textContent='copy',1200);}catch(e){}}
function copyFix(el){try{navigator.clipboard.writeText(FIXSNIP);el.textContent='copied';
 setTimeout(()=>el.textContent='copy',1200);}catch(e){}}
document.getElementById('backdrop').addEventListener('click',closePanel);
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel();});
refresh();setInterval(refresh,2000);
</script></body></html>"""
