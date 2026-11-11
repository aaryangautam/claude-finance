"""Localhost app: Plaid Link (link / re-auth), account mapping, dashboard charts, unclear queue (read-only).

Access tokens are written to .env and never rendered. Nothing here can initiate a payment or trade.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import accounts as acct_mod
from .. import config as cfgmod
from ..db import open_db
from ..linking import register_item
from ..pipeline.categorize import unclear_queue
from ..planner import month as month_plan
from ..planner.common import PlannerError
from ..plaid_client import PlaidClient, PlaidError
from ..vault import SchemaError, load_budget_schema

app = FastAPI(title="Finance")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def cfg():
    return cfgmod.load()


def schema_or_none(c):
    try:
        return load_budget_schema(c.budget_schema_path)
    except SchemaError:
        return None


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/dashboard")


# --- Accounts / Link ---------------------------------------------------------
@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    c = cfg()
    conn = open_db(c.db_path)
    items = conn.execute("SELECT * FROM plaid_items ORDER BY institution").fetchall()
    accts = conn.execute("SELECT * FROM accounts ORDER BY institution, id").fetchall()
    s = schema_or_none(c)
    mapped_ids = {a["id"] for a in accts}
    free_schema_ids = [a.id for a in s.accounts if a.id not in mapped_ids] if s else []
    return templates.TemplateResponse(
        request,
        "accounts.html",
        dict(items=items, accounts=accts, free_schema_ids=free_schema_ids, env=c.secret("PLAID_ENV"), schema_ok=s is not None,
             institutions=c.link_institutions),
    )


@app.post("/link/token")
def link_token(item_id: str | None = Form(None)):
    c = cfg()
    client = PlaidClient(c)
    access_token = None
    if item_id:
        conn = open_db(c.db_path)
        row = conn.execute("SELECT token_env_key FROM plaid_items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown item")
        access_token = c.secret(row["token_env_key"])
    try:
        return {"link_token": client.create_link_token("finance-local", access_token=access_token)}
    except PlaidError as e:
        raise HTTPException(502, str(e))


@app.post("/link/exchange")
def link_exchange(public_token: str = Form(...), institution: str = Form(...)):
    c = cfg()
    try:
        return register_item(c, PlaidClient(c), public_token, institution)
    except PlaidError as e:
        raise HTTPException(502, str(e))


@app.post("/link/reauth-done")
def reauth_done(item_id: str = Form(...)):
    c = cfg()
    conn = open_db(c.db_path)
    conn.execute("UPDATE plaid_items SET status='ok', last_error=NULL WHERE item_id=?", (item_id,))
    conn.commit()
    return {"ok": True}


@app.post("/accounts/map")
def accounts_map(old_id: str = Form(...), new_id: str = Form(...)):
    c = cfg()
    conn = open_db(c.db_path)
    s = load_budget_schema(c.budget_schema_path)
    try:
        acct_mod.map_account(conn, s, old_id, new_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse("/accounts", status_code=303)


# --- Dashboard -----------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, month: str | None = None):
    c = cfg()
    conn = open_db(c.db_path, ro=True) if c.db_path.exists() else open_db(c.db_path)
    s = schema_or_none(c)
    month = month or date.today().strftime("%Y-%m")
    cats, uncategorized, plan_error, plan = [], 0.0, None, None
    if s:
        try:
            plan = month_plan.build(conn, s, month, income_category=c.income_category)
        except PlannerError as e:
            plan_error = str(e)
    if plan:
        cats = [dict(id=l.id, group=l.group, type=l.type, planned=l.planned_usd, actual=l.actual_usd,
                     pct=l.pct_used, projected=l.projected_usd, paid=l.paid, reimbursed=l.reimbursed_usd)
                for l in plan.lines]
        uncategorized = plan.uncategorized_usd
    nw = conn.execute("SELECT date, net_worth_usd, assets_usd, liabilities_usd FROM v_net_worth ORDER BY date").fetchall()
    income = month_plan.income_history(conn, s, income_category=c.income_category) if s else []
    months = [r[0] for r in conn.execute("SELECT DISTINCT substr(date,1,7) FROM transactions ORDER BY 1 DESC")]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        dict(
            month=month, months=months, cats=cats, uncategorized=uncategorized, net_worth=[dict(r) for r in nw],
            income=income, planned_net=s.planned_net if s else None, schema_ok=s is not None,
            plan_error=plan_error, totals=plan.totals if plan else None,
        ),
    )


@app.get("/unclear", response_class=HTMLResponse)
def unclear(request: Request, month: str | None = None):
    c = cfg()
    conn = open_db(c.db_path, ro=True)
    rows = unclear_queue(conn, c, month)
    return templates.TemplateResponse(request, "unclear.html", dict(rows=rows, month=month, threshold=c.unclear_threshold))


@app.get("/api/health")
def health():
    c = cfg()
    return JSONResponse({"db": c.db_path.exists(), "schema": schema_or_none(c) is not None})
