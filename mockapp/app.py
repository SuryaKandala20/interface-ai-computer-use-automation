"""LedgerLine Core 7.x, a mock core-banking app that's old-fashioned on purpose.

This is the stand-in target for the take-home. It looks like the kind of app you
find at credit unions: server-rendered pages, a frameset, table layouts, <font>
tags for headings, no ids / test ids / <label> tags, and labels sitting in the
table cell next to the input. All the data is made up.

Two tenants run the same product with different settings:
  /t/fvcu    "First Valley CU"    (base setup)
  /t/harbor  "Harbor Point CU"    (different labels/buttons and branding)

You can inject runtime problems with POST /__chaos. That's only for testing; the
automation policy never allows that path. Modes:
  slow                  member pages respond after a 3.5s delay
  session_expired_once  next member page shows "session expired" + drops session
  notice_once           next member page is a "System Notice" interstitial
  app_error             member detail page returns an Oracle-style 500 error
  unknown_dialog_once   next member page shows an unexpected concurrency warning
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="LedgerLine Core (mock)")


# --- tenants ---
@dataclass(frozen=True)
class TenantConfig:
    slug: str
    brand: str
    color: str
    member_label: str = "Member No.:"
    search_button: str = "Search"
    select_link: str = "Select"
    summary_heading: str = "Member Account Summary"
    avail_header: str = "Available Balance"
    operators: dict = field(default_factory=dict)


TENANTS = {
    "fvcu": TenantConfig(
        slug="fvcu", brand="First Valley Credit Union", color="#1d3f72",
        operators={"teller01": "fv-demo-Pass!"},
    ),
    "harbor": TenantConfig(
        slug="harbor", brand="Harbor Point CU", color="#0f5e4a",
        member_label="Member #", search_button="Find", select_link="Open",
        summary_heading="Member Summary", avail_header="Avail Bal",
        operators={"hp_teller": "hp-demo-Pass!"},
    ),
}

# --- fake data ---
MEMBERS = {
    "10234": {
        "name": "DOE, JANE A", "ssn": "123-45-6789", "dob": "03/12/1984",
        "address": "12 ELM ST, SPRINGFIELD, OR 97477", "phone": "(541) 555-0142",
        "branch": "01 MAIN",
        "accounts": [
            ("S00", "Base Share", "OPEN", "25.00", "25.00"),
            ("S01", "Share Savings", "OPEN", "12,450.18", "12,425.18"),
            ("D10", "Share Draft Checking", "OPEN", "1,832.40", "1,832.40"),
        ],
    },
    "10235": {
        "name": "RIVERA, MARCUS", "ssn": "987-65-4321", "dob": "11/02/1979",
        "address": "400 HARBOR WAY, COOS BAY, OR 97420", "phone": "(541) 555-0199",
        "branch": "03 NORTH",
        "accounts": [
            ("S00", "Base Share", "OPEN", "5.00", "5.00"),
            ("S02", "Share Savings", "OPEN", "3,210.55", "3,210.55"),
            ("L40", "Auto Loan", "OPEN", "-8,904.12", "0.00"),
        ],
    },
    "10777": {"restricted": True},
}

SESSIONS: dict[str, dict] = {}
CHAOS: dict[str, Optional[str]] = {"mode": None}
SUBACCOUNTS: list[dict] = []


def tenant_or_404(slug: str) -> TenantConfig:
    t = TENANTS.get(slug)
    if not t:
        raise HTTPException(404)
    return t


# --- layout ---
def page(title: str, body: str, *, bg: str = "#ffffff") -> str:
    # Old-school markup on purpose: no semantic tags, tables for layout.
    return f"""<html><head><title>{html.escape(title)}</title>
<style>body{{font-family:Verdana,Arial;font-size:12px;margin:6px;background:{bg}}}
td{{font-size:12px}} .hdr td{{background:#c9d3e0;font-weight:bold}}
.err{{color:#a00000;font-weight:bold}} .btn{{font-size:12px}}</style></head>
<body>{body}</body></html>"""


def heading(text: str) -> str:
    return f'<font size="4"><b>{html.escape(text)}</b></font><br><br>'


def session_of(request: Request, t: TenantConfig) -> Optional[dict]:
    sid = request.cookies.get("LLSESSID")
    s = SESSIONS.get(sid or "")
    if s and s["tenant"] == t.slug:
        return s
    return None


def login_page(t: TenantConfig, message: str = "") -> str:
    msg = f'<tr><td colspan="2" class="err">{html.escape(message)}</td></tr>' if message else ""
    body = f"""
<table width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="{t.color}">
<font color="white" size="5"><b>&nbsp;{html.escape(t.brand)}</b></font><br>
<font color="#dddddd">&nbsp;LedgerLine Core 7.2 &mdash; Operator Sign On</font></td></tr></table>
<br><center>
<form method="post" action="/t/{t.slug}/login" target="_top">
<table border="1" cellpadding="6" cellspacing="0" bgcolor="#eef1f6">
<tr><td colspan="2" bgcolor="#c9d3e0"><b>Sign On</b></td></tr>
{msg}
<tr><td>Operator ID:</td><td><input type="text" name="opid" size="20"></td></tr>
<tr><td>Password:</td><td><input type="password" name="pwd" size="20"></td></tr>
<tr><td colspan="2" align="right"><input type="submit" value="Sign On" class="btn"></td></tr>
</table></form></center>"""
    return page(f"{t.brand} - Sign On", body)


def chaos_take(*modes: str) -> Optional[str]:
    mode = CHAOS["mode"]
    if mode in modes:
        if mode.endswith("_once"):
            CHAOS["mode"] = None
        return mode
    return None


# --- routes ---
@app.post("/__chaos")
async def set_chaos(request: Request):
    data = await request.json()
    CHAOS["mode"] = data.get("mode")
    return {"mode": CHAOS["mode"]}


@app.get("/t/{slug}/", response_class=HTMLResponse)
@app.get("/t/{slug}/login", response_class=HTMLResponse)
def login_get(slug: str):
    return login_page(tenant_or_404(slug))


@app.post("/t/{slug}/login")
def login_post(slug: str, opid: str = Form(""), pwd: str = Form("")):
    t = tenant_or_404(slug)
    if t.operators.get(opid) != pwd:
        return HTMLResponse(login_page(t, "SIGN ON FAILED - INVALID OPERATOR ID OR PASSWORD"))
    sid = secrets.token_hex(12)
    SESSIONS[sid] = {"tenant": slug, "opid": opid, "since": time.time()}
    resp = RedirectResponse(f"/t/{slug}/main", status_code=303)
    resp.set_cookie("LLSESSID", sid, httponly=True)
    return resp


@app.get("/t/{slug}/main", response_class=HTMLResponse)
def main_frameset(slug: str, request: Request):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return RedirectResponse(f"/t/{slug}/login", status_code=303)
    return f"""<html><head><title>{html.escape(t.brand)} - LedgerLine Core</title></head>
<frameset rows="58,*" border="0">
  <frame name="banner" src="/t/{slug}/banner" scrolling="no" noresize>
  <frameset cols="170,*">
    <frame name="nav" src="/t/{slug}/nav">
    <frame name="main" src="/t/{slug}/home">
  </frameset>
</frameset></html>"""


@app.get("/t/{slug}/banner", response_class=HTMLResponse)
def banner(slug: str, request: Request):
    t = tenant_or_404(slug)
    s = session_of(request, t)
    op = s["opid"] if s else "-"
    return page("banner", f"""<table width="100%"><tr><td bgcolor="{t.color}">
<font color="white" size="4"><b>{html.escape(t.brand)}</b></font>
<font color="#dddddd"> &nbsp; LedgerLine Core 7.2 &nbsp; | &nbsp; Operator: {html.escape(op)}</font>
</td></tr></table>""", bg=t.color)


@app.get("/t/{slug}/nav", response_class=HTMLResponse)
def nav(slug: str):
    t = tenant_or_404(slug)
    return page("nav", f"""<b>Functions</b><br><br>
<a href="/t/{slug}/inquiry" target="main">Member Inquiry</a><br>
<a href="/t/{slug}/home" target="main">Home</a><br><br>
<a href="/t/{slug}/logout" target="_top">Sign Off</a>""", bg="#e4e8ef")


@app.get("/t/{slug}/home", response_class=HTMLResponse)
def home(slug: str, request: Request):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    return page("home", heading("Operator Home") + "Select a function from the menu on the left.")


@app.get("/t/{slug}/logout")
def logout(slug: str, request: Request):
    SESSIONS.pop(request.cookies.get("LLSESSID", ""), None)
    return RedirectResponse(f"/t/{slug}/login", status_code=303)


@app.get("/t/{slug}/inquiry", response_class=HTMLResponse)
def inquiry(slug: str, request: Request, message: str = ""):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    return page("inquiry", heading("Member Inquiry") + f"""
<form method="get" action="/t/{slug}/inquiry/results">
<table cellpadding="4"><tr>
<td>{html.escape(t.member_label)}</td><td><input type="text" name="mid" size="12"></td>
<td><input type="submit" value="{html.escape(t.search_button)}" class="btn"></td>
</tr></table></form>""")


def guard_member_page(t: TenantConfig, request: Request, mid: str) -> Optional[str]:
    """Injects the chaos conditions for member pages. Returns HTML if it should cut the request short."""
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    if chaos_take("slow"):
        time.sleep(3.5)
    if chaos_take("session_expired_once"):
        SESSIONS.pop(request.cookies.get("LLSESSID", ""), None)
        return login_page(t, "Your session has expired. Please sign on again.")
    if chaos_take("notice_once"):
        return page("notice", heading("System Notice") + f"""
<table border="1" cellpadding="8" bgcolor="#fff8dc"><tr><td>
Scheduled maintenance tonight 11:00 PM - 1:00 AM PT. Share Draft posting will be delayed.<br><br>
<form method="get" action="/t/{t.slug}/member"><input type="hidden" name="mid" value="{html.escape(mid)}">
<input type="submit" value="Acknowledge"></form>
</td></tr></table>""")
    if chaos_take("unknown_dialog_once"):
        return page("warning", heading("Concurrent Session Warning") + f"""
This member record is currently open at another workstation (WS-0417).<br><br>
<form method="get" action="/t/{t.slug}/member" style="display:inline">
<input type="hidden" name="mid" value="{html.escape(mid)}"><input type="submit" value="Continue Anyway"></form>
<form method="get" action="/t/{t.slug}/inquiry" style="display:inline">
<input type="submit" value="Return"></form>""")
    return None


@app.get("/t/{slug}/inquiry/results", response_class=HTMLResponse)
def results(slug: str, request: Request, mid: str = ""):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    mid = mid.strip()
    if not mid.isdigit() or not (4 <= len(mid) <= 10):
        body = heading("Member Inquiry") + f'<span class="err">ERROR 1107: INVALID MEMBER NUMBER FORMAT</span>'
        return page("results", body + f'<br><br><a href="/t/{slug}/inquiry">New Search</a>')
    m = MEMBERS.get(mid)
    if m is None:
        body = heading("Search Results") + f'<span class="err">NO RECORDS FOUND FOR MEMBER {html.escape(mid)}</span>'
        return page("results", body + f'<br><br><a href="/t/{slug}/inquiry">New Search</a>')
    name = "*** RESTRICTED ***" if m.get("restricted") else m["name"]
    branch = "--" if m.get("restricted") else m["branch"]
    return page("results", heading("Search Results") + f"""
<table border="1" cellpadding="4" cellspacing="0">
<tr class="hdr"><td>Member No.</td><td>Name</td><td>Branch</td><td>&nbsp;</td></tr>
<tr><td>{mid}</td><td>{html.escape(name)}</td><td>{branch}</td>
<td><a href="/t/{slug}/member?mid={mid}">{html.escape(t.select_link)}</a></td></tr>
</table>""")


@app.get("/t/{slug}/member", response_class=HTMLResponse)
def member(slug: str, request: Request, mid: str = ""):
    t = tenant_or_404(slug)
    short = guard_member_page(t, request, mid)
    if short:
        return short
    if CHAOS["mode"] == "app_error":
        return HTMLResponse(page("error", heading("Application Error") +
            '<span class="err">ORA-01034: ORACLE not available</span><br>Reference: 7F3A-22C1. '
            "Contact your system administrator."), status_code=500)
    m = MEMBERS.get(mid)
    if m is None:
        return page("member", heading(t.summary_heading) +
                    f'<span class="err">NO RECORDS FOUND FOR MEMBER {html.escape(mid)}</span>')
    if m.get("restricted"):
        return page("member", heading(t.summary_heading) +
                    '<span class="err">ACCESS DENIED - INSUFFICIENT PRIVILEGES FOR RESTRICTED ACCOUNT</span>')
    rows = "".join(
        f"<tr><td>{s}</td><td>{d}</td><td>{st}</td><td align=right>${cb}</td><td align=right>${ab}</td></tr>"
        for s, d, st, cb, ab in m["accounts"]
    )
    return page("member", heading(t.summary_heading) + f"""
<table cellpadding="3" cellspacing="0" border="0">
<tr><td><b>Member No.</b></td><td>{mid}</td></tr>
<tr><td><b>Member Name</b></td><td>{html.escape(m['name'])}</td></tr>
<tr><td><b>SSN</b></td><td>{m['ssn']}</td></tr>
<tr><td><b>Date of Birth</b></td><td>{m['dob']}</td></tr>
<tr><td><b>Address</b></td><td>{html.escape(m['address'])}</td></tr>
<tr><td><b>Phone</b></td><td>{m['phone']}</td></tr>
</table><br>
<table border="1" cellpadding="4" cellspacing="0">
<tr class="hdr"><td>Suffix</td><td>Description</td><td>Status</td><td>Current Balance</td><td>{html.escape(t.avail_header)}</td></tr>
{rows}
</table><br>
<a href="/t/{slug}/subaccount?mid={mid}">Open Sub-Account</a> &nbsp;
<a href="/t/{slug}/inquiry">New Search</a>""")


@app.get("/t/{slug}/subaccount", response_class=HTMLResponse)
def subaccount_form(slug: str, request: Request, mid: str = "", message: str = ""):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    err = f'<span class="err">{html.escape(message)}</span><br><br>' if message else ""
    return page("subaccount", heading("Open Sub-Account") + err + f"""
<form method="get" action="/t/{slug}/subaccount/review">
<input type="hidden" name="mid" value="{html.escape(mid)}">
<table cellpadding="4">
<tr><td>Member No.:</td><td>{html.escape(mid)}</td></tr>
<tr><td>Account Type:</td><td><select name="atype">
<option value="">-- select --</option><option>Money Market</option>
<option>Holiday Club</option><option>Share Certificate</option></select></td></tr>
<tr><td>Nickname:</td><td><input type="text" name="nick" size="20"></td></tr>
<tr><td>Initial Deposit:</td><td><input type="text" name="dep" size="10"></td></tr>
<tr><td></td><td><input type="submit" value="Continue"></td></tr>
</table></form>""")


@app.get("/t/{slug}/subaccount/review", response_class=HTMLResponse)
def subaccount_review(slug: str, request: Request, mid: str = "", atype: str = "", nick: str = "", dep: str = ""):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    problem = None
    if not atype:
        problem = "ERROR 2201: ACCOUNT TYPE IS REQUIRED"
    else:
        try:
            if float(dep.replace(",", "")) < 0:
                raise ValueError
        except ValueError:
            problem = "ERROR 2204: INITIAL DEPOSIT MUST BE A NON-NEGATIVE AMOUNT"
    if problem:
        return subaccount_form(slug, request, mid=mid, message=problem)
    e = html.escape
    return page("review", heading("Review Sub-Account Request") + f"""
<table cellpadding="4" border="1" cellspacing="0">
<tr><td>Member No.</td><td>{e(mid)}</td></tr><tr><td>Account Type</td><td>{e(atype)}</td></tr>
<tr><td>Nickname</td><td>{e(nick)}</td></tr><tr><td>Initial Deposit</td><td>${e(dep)}</td></tr>
</table><br>
<form method="post" action="/t/{slug}/subaccount/submit" style="display:inline">
<input type="hidden" name="mid" value="{e(mid)}"><input type="hidden" name="atype" value="{e(atype)}">
<input type="hidden" name="nick" value="{e(nick)}"><input type="hidden" name="dep" value="{e(dep)}">
<input type="submit" value="Submit Request"></form>
<form method="get" action="/t/{slug}/member" style="display:inline">
<input type="hidden" name="mid" value="{e(mid)}"><input type="submit" value="Cancel"></form>""")


@app.post("/t/{slug}/subaccount/submit", response_class=HTMLResponse)
def subaccount_submit(slug: str, request: Request, mid: str = Form(""), atype: str = Form(""),
                      nick: str = Form(""), dep: str = Form("")):
    t = tenant_or_404(slug)
    if not session_of(request, t):
        return login_page(t, "Your session has expired. Please sign on again.")
    conf = f"CNF-{len(SUBACCOUNTS) + 40117}"
    SUBACCOUNTS.append({"mid": mid, "atype": atype, "nick": nick, "dep": dep, "conf": conf})
    return page("confirmation", heading("Sub-Account Opened") + f"""
Request accepted.<br><br><table cellpadding="3">
<tr><td><b>Confirmation No.</b></td><td>{conf}</td></tr>
<tr><td><b>Account Type</b></td><td>{html.escape(atype)}</td></tr></table><br>
<a href="/t/{slug}/member?mid={html.escape(mid)}">Return to Member</a>""")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
