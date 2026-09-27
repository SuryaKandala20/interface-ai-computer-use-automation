"""Operator console. I kept this part of the handoff minimal on purpose.

Real: the operator attaches to the same live browser the executor is using (over
CDP), sees it, clicks and types into it, and hands control back through the same
lease/intervention flow the executor follows. Only the operator who claimed the
intervention can send input.

Mocked: there's no real login (you just type a name), and instead of streaming the
screen it polls screenshots and forwards clicks by coordinates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from playwright.async_api import async_playwright
from pydantic import BaseModel

from .control import ControlError, HandoffBroker


class Act(BaseModel):
    operator: str
    x: Optional[float] = None
    y: Optional[float] = None
    text: Optional[str] = None
    key: Optional[str] = None
    mode: Optional[str] = None
    note: str = ""


class LiveSessions:
    """Keeps one CDP connection per executor browser."""

    def __init__(self) -> None:
        self._pw = None
        self._browsers: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def page(self, endpoint: str):
        async with self._lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
            b = self._browsers.get(endpoint)
            if b is None or not b.is_connected():
                b = await self._pw.chromium.connect_over_cdp(endpoint)
                self._browsers[endpoint] = b
        ctx = b.contexts[0]
        return ctx.pages[0]


def create_app(control_root: str = "runs/_control") -> FastAPI:
    app = FastAPI(title="CUA operator console")
    broker = HandoffBroker(control_root)
    live = LiveSessions()

    def held(iid: str, operator: str):
        try:
            intv = broker.get(iid)
        except KeyError:
            raise HTTPException(404, "no such intervention")
        if intv.status != "claimed" or intv.claimed_by != operator:
            raise HTTPException(409, f"take control first (status={intv.status}, holder={intv.claimed_by})")
        if not intv.live_endpoint:
            raise HTTPException(409, "intervention has no live session")
        return intv

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return CONSOLE_HTML

    @app.get("/api/interventions")
    def list_interventions() -> list[dict[str, Any]]:
        items = broker.list()
        items.sort(key=lambda i: i.created_at, reverse=True)
        return [i.model_dump() for i in items[:20]]

    @app.get("/api/i/{iid}/evidence.png")
    def evidence(iid: str):
        p = broker.get(iid).evidence.get("screenshot")
        if not p or not Path(p).exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/png")

    @app.get("/api/i/{iid}/live.png")
    async def live_png(iid: str, operator: str):
        intv = held(iid, operator)
        page = await live.page(intv.live_endpoint)
        return Response(await page.screenshot(), media_type="image/png")

    @app.post("/api/i/{iid}/claim")
    def claim(iid: str, a: Act):
        try:
            return broker.claim(iid, a.operator).model_dump()
        except (ControlError, KeyError) as e:
            raise HTTPException(409, str(e))

    @app.post("/api/i/{iid}/click")
    async def click(iid: str, a: Act):
        intv = held(iid, a.operator)
        page = await live.page(intv.live_endpoint)
        await page.mouse.click(a.x or 0, a.y or 0)
        broker.record_human_action(iid, {"source": "console", "type": "click", "x": a.x, "y": a.y})
        return {"ok": True}

    @app.post("/api/i/{iid}/click_text")
    async def click_text(iid: str, a: Act):
        """Click a visible button or link by its text. Handy for scripted operators."""
        intv = held(iid, a.operator)
        page = await live.page(intv.live_endpoint)
        for f in page.frames:
            for loc in (f.get_by_role("button", name=a.text or ""), f.get_by_role("link", name=a.text or "")):
                if await loc.count() == 1 and await loc.is_visible():
                    box = await loc.bounding_box()
                    await loc.click()
                    broker.record_human_action(iid, {"source": "console", "type": "click", "text": a.text, "box": box})
                    return {"ok": True}
        raise HTTPException(404, f"no unique visible control '{a.text}'")

    @app.post("/api/i/{iid}/type")
    async def type_text(iid: str, a: Act):
        intv = held(iid, a.operator)
        page = await live.page(intv.live_endpoint)
        await page.keyboard.type(a.text or "")
        broker.record_human_action(iid, {"source": "console", "type": "type", "chars": len(a.text or "")})
        return {"ok": True}

    @app.post("/api/i/{iid}/press")
    async def press(iid: str, a: Act):
        intv = held(iid, a.operator)
        page = await live.page(intv.live_endpoint)
        await page.keyboard.press(a.key or "Enter")
        broker.record_human_action(iid, {"source": "console", "type": "press", "key": a.key})
        return {"ok": True}

    @app.post("/api/i/{iid}/resolve")
    def resolve(iid: str, a: Act):
        try:
            return broker.resolve(iid, a.mode or "", a.operator, a.note).model_dump()
        except (ControlError, KeyError) as e:
            raise HTTPException(409, str(e))

    return app


CONSOLE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Operator Console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mute:#5d6b7c;--line:#dde2e8;--acc:#1d4ed8;--warn:#b45309;--bad:#b91c1c;--ok:#15803d}
body{margin:0;font:14px/1.45 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0}main{padding:16px;max-width:1400px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:16px;flex-wrap:wrap}.col{flex:1;min-width:280px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;background:#e8eefc;color:var(--acc)}
.badge.approval{background:#fdf1e3;color:var(--warn)}.badge.resolved{background:#e7f5ec;color:var(--ok)}
.mute{color:var(--mute)}img{max-width:100%;border:1px solid var(--line);border-radius:4px}
img.live{cursor:crosshair}button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:#fff;cursor:pointer}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}input{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px}
pre{white-space:pre-wrap;background:#f1f3f6;padding:8px;border-radius:6px;font-size:12px;max-height:180px;overflow:auto}
</style></head><body>
<header><h1>Operator Console</h1><span class="mute">Interventions raised by automation runs</span>
<label>Operator <input id="op" value="operator.jlee" size="16"></label></header>
<main id="list"><p class="mute">Loading...</p></main>
<script>
const op=()=>document.getElementById('op').value.trim();
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operator:op(),...b})}).then(async r=>{if(!r.ok)alert((await r.json()).detail)});
let liveOn={};
function card(i){
 const held=i.status==='claimed'&&i.claimed_by===op();
 const res=(i.allowed_resolutions||[]).map(m=>`<button onclick="resolve('${i.id}','${m}')">${m}</button>`).join(' ');
 return `<div class="card"><div class="row"><div class="col">
  <span class="badge ${i.kind}">${i.kind}</span> <span class="badge ${i.status==='resolved'?'resolved':''}">${i.status}</span>
  <b> ${i.id}</b><div class="mute">${i.subject} &middot; step ${i.step_id||'-'} &middot; ${i.created_at}</div>
  <p><b>${i.reason_code}</b>: ${i.reason}</p>${i.step_intent?`<p class="mute">Step intent: ${i.step_intent}</p>`:''}
  <pre>${JSON.stringify(i.observed,null,1)}</pre>
  ${i.status==='open'?`<button class="primary" onclick="claim('${i.id}')">Take control</button>`:''}
  ${held?`<p><input id="t-${i.id}" placeholder="text to type"> <button onclick="typ('${i.id}')">Type</button> <button onclick="key('${i.id}')">Enter</button></p>
   <p><input id="n-${i.id}" placeholder="note for the log" size="30"> ${res}</p>`:''}
  ${i.status==='resolved'?`<p>Resolved: <b>${i.resolution.mode}</b> by ${i.resolution.operator}</p>`:''}
  <p class="mute">${(i.human_actions||[]).length} human action(s) recorded</p></div>
  <div class="col">${held?`<div class="mute">Live session (click to interact)</div><img class="live" id="live-${i.id}" onclick="clk(event,'${i.id}')" src="/api/i/${i.id}/live.png?operator=${encodeURIComponent(op())}&t=${Date.now()}">`
   :(i.evidence&&i.evidence.screenshot?`<div class="mute">Screenshot at escalation (PII masked)</div><img src="/api/i/${i.id}/evidence.png">`:'')}</div></div></div>`}
async function refresh(){const items=await (await fetch('/api/interventions')).json();
 const active=document.activeElement&&document.activeElement.tagName==='INPUT'&&document.activeElement.id!=='op';
 if(!active)document.getElementById('list').innerHTML=items.length?items.map(card).join(''):'<p class="mute">No interventions.</p>';
 items.filter(i=>i.status==='claimed'&&i.claimed_by===op()).forEach(i=>{const im=document.getElementById('live-'+i.id);if(im)im.src=`/api/i/${i.id}/live.png?operator=${encodeURIComponent(op())}&t=${Date.now()}`});}
function claim(id){post(`/api/i/${id}/claim`,{}).then(refresh)}
function resolve(id,m){const n=document.getElementById('n-'+id);post(`/api/i/${id}/resolve`,{mode:m,note:n?n.value:''}).then(refresh)}
function typ(id){post(`/api/i/${id}/type`,{text:document.getElementById('t-'+id).value})}
function key(id){post(`/api/i/${id}/press`,{key:'Enter'})}
function clk(e,id){const r=e.target.getBoundingClientRect();const sx=e.target.naturalWidth/r.width,sy=e.target.naturalHeight/r.height;
 post(`/api/i/${id}/click`,{x:(e.clientX-r.left)*sx,y:(e.clientY-r.top)*sy})}
refresh();setInterval(refresh,1500);
</script></body></html>"""
