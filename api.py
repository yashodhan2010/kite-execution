from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from kite_core import (
    Account,
    build_plan,
    execute_plan,
    fetch_cash,
    fetch_holdings,
    fetch_prices,
    finish_login,
    kite_for,
    list_runs,
    load_accounts,
    normalize_target_portfolio,
    save_run,
    upsert_account,
)


load_dotenv()

PENDING_PLANS: dict[str, dict] = {}

app = FastAPI(title="Vriksha Execution")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AccountInput(BaseModel):
    label: str
    api_key: str
    api_secret: str


class LoginInput(BaseModel):
    label: str
    request_token: str


class ExecuteInput(BaseModel):
    plan_id: str
    market_protection: float = -1


@app.get("/api/accounts")
def accounts() -> list[dict]:
    return [
        {
            "label": account.label,
            "user_id": account.user_id,
            "connected": bool(account.access_token),
        }
        for account in load_accounts()
    ]


@app.post("/api/accounts")
def save_account(payload: AccountInput) -> dict:
    if not payload.label.strip() or not payload.api_key.strip() or not payload.api_secret:
        raise HTTPException(400, "Account label, API key, and API secret are required.")
    existing = get_account(payload.label, required=False)
    upsert_account(
        Account(
            label=payload.label.strip(),
            api_key=payload.api_key.strip(),
            api_secret=payload.api_secret,
            access_token=existing.access_token if existing else "",
            user_id=existing.user_id if existing else "",
        )
    )
    return {"ok": True}


@app.get("/api/login-url/{label}")
def login_url(label: str) -> dict:
    account = get_account(label)
    return {"login_url": kite_for(account).login_url()}


@app.get("/api/diagnostics/{label}")
def diagnostics(label: str) -> dict:
    account = get_account(label)
    kite = kite_for(account)
    checks = [
        ("profile", lambda: kite.profile()),
        ("holdings", lambda: kite.holdings()),
        ("margins_equity", lambda: kite.margins(segment="equity")),
        ("ltp", lambda: kite.ltp(["NSE:INFY"])),
    ]
    results = []
    for name, check in checks:
        try:
            check()
            results.append({"check": name, "status": "OK", "message": ""})
        except Exception as exc:
            results.append(
                {
                    "check": name,
                    "status": "FAIL",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"label": account.label, "results": results}


@app.post("/api/complete-login")
def complete_login(payload: LoginInput) -> dict:
    account = get_account(payload.label)
    try:
        account = finish_login(account, payload.request_token.strip())
    except Exception as exc:
        raise HTTPException(400, f"Login failed: {exc}") from exc
    return {"label": account.label, "user_id": account.user_id, "connected": True}


@app.post("/api/plan")
async def plan(
    label: str = Form(...),
    min_order_value: float = Form(500),
    max_order_value: float = Form(0),
    file: UploadFile = File(...),
) -> dict:
    account = get_account(label)
    if not account.access_token:
        raise HTTPException(400, "Account is not connected to Kite.")

    content = await file.read()
    try:
        target = normalize_target_portfolio(BytesIO(content))
        kite = kite_for(account)
        holdings = fetch_holdings(kite)
        cash = fetch_cash(kite)
        prices = fetch_prices(kite, target, holdings)
        order_plan, warnings = build_plan(
            target,
            holdings,
            prices,
            cash,
            min_order_value,
            max_order_value,
            liquidate_absent=target.attrs.get("liquidate_absent", True),
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    plan_id = uuid4().hex
    PENDING_PLANS[plan_id] = {
        "account": account,
        "target": target,
        "holdings": holdings,
        "plan": order_plan,
        "warnings": warnings,
        "cash": cash,
        "import_kind": target.attrs.get("import_kind", "target_portfolio"),
        "source_filename": file.filename,
    }
    return {
        "plan_id": plan_id,
        "cash": cash,
        "import_kind": target.attrs.get("import_kind", "target_portfolio"),
        "liquidate_absent": target.attrs.get("liquidate_absent", True),
        "target": frame_to_records(target),
        "holdings": frame_to_records(holdings),
        "plan": frame_to_records(order_plan),
        "warnings": warnings,
        "summary": summarize_plan(order_plan),
    }


@app.post("/api/execute")
def execute(payload: ExecuteInput) -> dict:
    pending = PENDING_PLANS.get(payload.plan_id)
    if not pending:
        raise HTTPException(404, "Plan not found. Generate a fresh plan before execution.")
    if payload.market_protection != -1 and not (0 < payload.market_protection <= 100):
        raise HTTPException(400, "Market protection must be -1 or above 0 up to 100.")

    account: Account = pending["account"]
    try:
        result = execute_plan(
            kite_for(account),
            pending["plan"],
            tag="vriksha_rebalance",
            market_protection=payload.market_protection,
        )
        run_dir = save_run(
            account,
            pending["target"],
            pending["holdings"],
            pending["plan"],
            pending["warnings"],
            result,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "result": frame_to_records(result),
        "summary": summarize_result(result),
        "run_dir": str(run_dir),
    }


@app.get("/api/runs")
def runs() -> dict:
    return {"runs": frame_to_records(list_runs())}


def get_account(label: str, required: bool = True) -> Account | None:
    for account in load_accounts():
        if account.label == label:
            return account
    if required:
        raise HTTPException(404, f"Account not found: {label}")
    return None


def frame_to_records(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    cleaned = frame.where(pd.notna(frame), None)
    return cleaned.to_dict(orient="records")


def summarize_plan(plan_frame: pd.DataFrame) -> dict:
    if plan_frame.empty:
        return {"orders": 0, "buy_value": 0, "sell_value": 0, "buys": 0, "sells": 0}
    buys = plan_frame["action"] == "BUY"
    sells = plan_frame["action"] == "SELL"
    return {
        "orders": int(len(plan_frame)),
        "buy_value": float(plan_frame.loc[buys, "value"].sum()),
        "sell_value": float(plan_frame.loc[sells, "value"].sum()),
        "buys": int(buys.sum()),
        "sells": int(sells.sum()),
    }


def summarize_result(result: pd.DataFrame) -> dict:
    if result.empty:
        return {"orders": 0, "placed": 0, "failed": 0}
    return {
        "orders": int(len(result)),
        "placed": int(result["status"].astype(str).isin(["PLACED", "COMPLETE"]).sum()),
        "failed": int((result["status"].astype(str) == "FAILED").sum()),
    }


@app.get("/api/run-file/{run_name}/{file_name}")
def run_file(run_name: str, file_name: str) -> dict:
    path = Path("data") / "runs" / run_name / file_name
    if not path.exists() or path.suffix.lower() != ".csv":
        raise HTTPException(404, "Run file not found.")
    return {"rows": frame_to_records(pd.read_csv(path))}
