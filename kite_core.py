from __future__ import annotations

import json
import math
import inspect
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from kiteconnect import KiteConnect


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
RUNS_DIR = DATA_DIR / "runs"
ORDER_REQUEST_DELAY_SECONDS = 0.4


@dataclass
class Account:
    label: str
    api_key: str
    api_secret: str
    access_token: str = ""
    user_id: str = ""


@dataclass
class OrderPlan:
    tradingsymbol: str
    exchange: str
    action: str
    quantity: int
    price: float
    value: float
    current_quantity: int
    sellable_quantity: int
    target_quantity: int
    reason: str
    status: str = "READY"
    order_id: str = ""
    message: str = ""


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    RUNS_DIR.mkdir(exist_ok=True)


def load_accounts() -> list[Account]:
    ensure_data_dirs()
    if not ACCOUNTS_FILE.exists():
        return []
    rows = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    return [Account(**row) for row in rows]


def save_accounts(accounts: list[Account]) -> None:
    ensure_data_dirs()
    ACCOUNTS_FILE.write_text(
        json.dumps([asdict(account) for account in accounts], indent=2),
        encoding="utf-8",
    )


def upsert_account(account: Account) -> None:
    accounts = [item for item in load_accounts() if item.label != account.label]
    accounts.append(account)
    save_accounts(accounts)


def kite_for(account: Account) -> KiteConnect:
    kite = KiteConnect(api_key=account.api_key)
    if account.access_token:
        kite.set_access_token(account.access_token)
    return kite


def finish_login(account: Account, request_token: str) -> Account:
    kite = KiteConnect(api_key=account.api_key)
    session = kite.generate_session(request_token, api_secret=account.api_secret)
    account.access_token = session["access_token"]
    account.user_id = session.get("user_id", account.user_id)
    upsert_account(account)
    return account


def normalize_target_portfolio(uploaded_file: Any) -> pd.DataFrame:
    raw = pd.read_csv(uploaded_file)
    if raw.empty:
        raise ValueError("The uploaded portfolio is empty.")

    normalized = raw.copy()
    normalized.columns = [
        str(col).strip().lower().replace(" ", "_").replace("-", "_")
        for col in normalized.columns
    ]
    import_kind = "target_portfolio"
    if {"date", "old_weight", "new_weight"}.issubset(normalized.columns):
        import_kind = "rebalance_history"
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        latest_date = normalized["date"].max()
        if pd.isna(latest_date):
            raise ValueError("Rebalance history has no valid date values.")
        normalized = normalized[normalized["date"] == latest_date].copy()

    symbol_col = first_existing(
        normalized,
        ["tradingsymbol", "trading_symbol", "symbol", "ticker", "stock", "name"],
    )
    exchange_col = first_existing(normalized, ["exchange", "segment"], required=False)
    qty_col = first_existing(
        normalized,
        ["target_quantity", "quantity", "qty", "shares", "target_qty"],
        required=False,
    )
    weight_col = first_existing(
        normalized,
        [
            "target_weight",
            "new_weight",
            "weight",
            "allocation",
            "allocation_%",
            "target_%",
        ],
        required=False,
    )
    value_col = first_existing(
        normalized,
        ["target_value", "value", "amount", "investment_amount", "target_amount"],
        required=False,
    )

    target = pd.DataFrame()
    target["tradingsymbol"] = normalized[symbol_col].astype(str).str.strip().str.upper()
    target["exchange"] = (
        normalized[exchange_col].astype(str).str.strip().str.upper()
        if exchange_col
        else "NSE"
    )
    target["target_quantity"] = numeric_column(normalized, qty_col)
    target["target_weight"] = numeric_column(normalized, weight_col)
    target["target_value"] = numeric_column(normalized, value_col)
    if "company" in normalized.columns:
        target["company"] = normalized["company"].astype(str).str.strip()
    if "action" in normalized.columns:
        target["action"] = normalized["action"].astype(str).str.strip()
    if "old_weight" in normalized.columns:
        target["old_weight"] = numeric_column(normalized, "old_weight")
    if "date" in normalized.columns:
        target["date"] = normalized["date"].dt.strftime("%Y-%m-%d")
    target = target[target["tradingsymbol"] != ""]

    if target["tradingsymbol"].duplicated().any():
        aggregations: dict[str, tuple[str, str]] = {
            "target_quantity": ("target_quantity", "sum"),
            "target_weight": ("target_weight", "sum"),
            "target_value": ("target_value", "sum"),
        }
        for column in ["company", "action", "old_weight", "date"]:
            if column in target.columns:
                aggregations[column] = (column, "first")
        target = target.groupby(["exchange", "tradingsymbol"], as_index=False).agg(
            **aggregations
        )

    has_qty = target["target_quantity"].fillna(0).gt(0).any()
    has_weight = target["target_weight"].fillna(0).gt(0).any()
    has_value = target["target_value"].fillna(0).gt(0).any()
    if not (has_qty or has_weight or has_value):
        raise ValueError(
            "Portfolio needs target quantity, target weight, or target value columns."
        )

    target.attrs["import_kind"] = import_kind
    target.attrs["liquidate_absent"] = import_kind == "target_portfolio"
    return target


def first_existing(
    frame: pd.DataFrame, candidates: list[str], required: bool = True
) -> str | None:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    if required:
        raise ValueError(f"Missing required column. Tried: {', '.join(candidates)}")
    return None


def numeric_column(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if not column:
        return pd.Series([0.0] * len(frame))
    cleaned = (
        frame[column]
        .astype(str)
        .str.replace("%", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.replace(r"[^0-9.\-]", "", regex=True)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)


def fetch_holdings(kite: KiteConnect) -> pd.DataFrame:
    holdings = pd.DataFrame(kite.holdings())
    if holdings.empty:
        return pd.DataFrame(
            columns=[
                "tradingsymbol",
                "exchange",
                "quantity",
                "t1_quantity",
                "sellable_quantity",
                "last_price",
                "value",
            ]
        )

    holdings["tradingsymbol"] = holdings["tradingsymbol"].astype(str).str.upper()
    holdings["exchange"] = holdings.get("exchange", "NSE")
    holdings["quantity"] = pd.to_numeric(holdings["quantity"], errors="coerce").fillna(0)
    holdings["t1_quantity"] = pd.to_numeric(
        holdings.get("t1_quantity", 0), errors="coerce"
    ).fillna(0)
    holdings["sellable_quantity"] = holdings["quantity"]
    holdings["quantity"] = holdings["quantity"] + holdings["t1_quantity"]
    holdings["last_price"] = pd.to_numeric(
        holdings.get("last_price", 0), errors="coerce"
    ).fillna(0)
    holdings["value"] = holdings["quantity"] * holdings["last_price"]
    return holdings[
        [
            "tradingsymbol",
            "exchange",
            "quantity",
            "t1_quantity",
            "sellable_quantity",
            "last_price",
            "value",
        ]
    ]


def fetch_cash(kite: KiteConnect) -> float:
    margins = kite.margins(segment="equity")
    return float(margins.get("available", {}).get("cash", 0) or 0)


def fetch_prices(kite: KiteConnect, target: pd.DataFrame, holdings: pd.DataFrame) -> dict[str, float]:
    instruments = sorted(
        {
            f"{row.exchange}:{row.tradingsymbol}"
            for row in pd.concat(
                [
                    target[["exchange", "tradingsymbol"]],
                    holdings[["exchange", "tradingsymbol"]],
                ],
                ignore_index=True,
            ).itertuples()
        }
    )
    prices: dict[str, float] = {}
    if not instruments:
        return prices
    for start in range(0, len(instruments), 200):
        quotes = kite.ltp(instruments[start : start + 200])
        for key, quote in quotes.items():
            prices[key] = float(quote.get("last_price", 0) or 0)
    return prices


def build_plan(
    target: pd.DataFrame,
    holdings: pd.DataFrame,
    prices: dict[str, float],
    cash: float,
    min_order_value: float,
    max_order_value: float,
    liquidate_absent: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    current = holdings.groupby(["exchange", "tradingsymbol"], as_index=False).agg(
        current_quantity=("quantity", "sum"),
        sellable_quantity=("sellable_quantity", "sum"),
        current_value=("value", "sum"),
    )
    join_type = "outer" if liquidate_absent else "left"
    merged = target.merge(current, how=join_type, on=["exchange", "tradingsymbol"])
    merged[["target_quantity", "target_weight", "target_value"]] = merged[
        ["target_quantity", "target_weight", "target_value"]
    ].fillna(0)
    merged[["current_quantity", "sellable_quantity", "current_value"]] = merged[
        ["current_quantity", "sellable_quantity", "current_value"]
    ].fillna(0)

    total_current_value = float(holdings["value"].sum()) if not holdings.empty else 0.0
    deployable_value = total_current_value + cash
    planned: list[OrderPlan] = []

    for row in merged.itertuples(index=False):
        key = f"{row.exchange}:{row.tradingsymbol}"
        last_price = prices.get(key, 0.0)
        if last_price <= 0:
            warnings.append(f"No valid price for {key}; skipped.")
            continue

        target_quantity = int(row.target_quantity or 0)
        if target_quantity <= 0 and row.target_value > 0:
            target_quantity = int(math.floor(row.target_value / last_price))
        if target_quantity <= 0 and row.target_weight > 0:
            weight = row.target_weight / 100 if row.target_weight > 1 else row.target_weight
            target_quantity = int(math.floor((deployable_value * weight) / last_price))

        current_quantity = int(row.current_quantity or 0)
        sellable_quantity = int(row.sellable_quantity or 0)
        delta = target_quantity - current_quantity
        if delta == 0:
            continue

        action = "BUY" if delta > 0 else "SELL"
        quantity = abs(delta)
        estimated_price = round(last_price, 2)
        value = quantity * estimated_price

        if value < min_order_value:
            warnings.append(f"{key} order value below minimum; skipped.")
            continue
        if max_order_value > 0 and value > max_order_value:
            warnings.append(f"{key} order value exceeds max order value; skipped.")
            continue
        if action == "SELL" and quantity > sellable_quantity:
            warnings.append(f"{key} sell quantity exceeds sellable holdings; skipped.")
            continue

        planned.append(
            OrderPlan(
                tradingsymbol=row.tradingsymbol,
                exchange=row.exchange,
                action=action,
                quantity=quantity,
                price=estimated_price,
                value=value,
                current_quantity=current_quantity,
                sellable_quantity=sellable_quantity,
                target_quantity=target_quantity,
                reason=f"{current_quantity} -> {target_quantity}",
            )
        )

    plan = pd.DataFrame([asdict(order) for order in planned])
    buy_value = float(plan.loc[plan["action"] == "BUY", "value"].sum()) if not plan.empty else 0
    sell_value = float(plan.loc[plan["action"] == "SELL", "value"].sum()) if not plan.empty else 0
    if buy_value > cash + sell_value:
        warnings.append(
            f"Estimated buy value {buy_value:,.2f} exceeds cash plus sells {(cash + sell_value):,.2f}."
        )
    return plan, warnings


def execute_plan(
    kite: KiteConnect, plan: pd.DataFrame, tag: str, market_protection: float
) -> pd.DataFrame:
    sell_first = plan.assign(
        execution_rank=plan["action"].map({"SELL": 0, "BUY": 1}).fillna(2)
    ).sort_values(["execution_rank", "tradingsymbol"])
    results = sell_first.drop(columns=["execution_rank"]).copy()
    results["order_type"] = "MARKET"
    results["market_protection"] = market_protection
    for idx, row in results.iterrows():
        try:
            order = {
                "variety": kite.VARIETY_REGULAR,
                "exchange": row["exchange"],
                "tradingsymbol": row["tradingsymbol"],
                "transaction_type": row["action"],
                "quantity": int(row["quantity"]),
                "product": kite.PRODUCT_CNC,
                "order_type": kite.ORDER_TYPE_MARKET,
                "validity": kite.VALIDITY_DAY,
                "tag": tag[:20],
            }
            order_id = place_order(kite, order, market_protection)
            results.loc[idx, "status"] = "PLACED"
            results.loc[idx, "order_id"] = order_id
            results.loc[idx, "message"] = ""
        except Exception as exc:
            results.loc[idx, "status"] = "FAILED"
            results.loc[idx, "message"] = str(exc)
        time.sleep(ORDER_REQUEST_DELAY_SECONDS)
    return refresh_order_status(kite, results)


def place_order(kite: KiteConnect, order: dict, market_protection: float) -> str:
    order_with_protection = {**order, "market_protection": market_protection}
    if "market_protection" in inspect.signature(kite.place_order).parameters:
        return kite.place_order(**order_with_protection)

    try:
        return kite.place_order(**order)
    except Exception as exc:
        if "market protection" not in str(exc).lower():
            raise

    variety = order["variety"]
    params = {
        key: value for key, value in order_with_protection.items() if key != "variety"
    }
    return kite._post(
        "order.place",
        url_args={"variety": variety},
        params=params,
    )["order_id"]


def refresh_order_status(kite: KiteConnect, results: pd.DataFrame) -> pd.DataFrame:
    if results.empty or "order_id" not in results.columns:
        return results
    try:
        orders = pd.DataFrame(kite.orders())
    except Exception as exc:
        results["message"] = results["message"].where(
            results["message"].astype(str).str.len() > 0,
            f"Status refresh failed: {exc}",
        )
        return results
    if orders.empty or "order_id" not in orders.columns:
        return results
    status_by_id = orders.set_index("order_id")
    for idx, row in results.iterrows():
        order_id = row.get("order_id", "")
        if not order_id or order_id not in status_by_id.index:
            continue
        order = status_by_id.loc[order_id]
        results.loc[idx, "status"] = order.get("status", row.get("status", "PLACED"))
        results.loc[idx, "message"] = order.get("status_message") or row.get("message", "")
    return results


def save_run(
    account: Account,
    target: pd.DataFrame,
    holdings: pd.DataFrame,
    plan: pd.DataFrame,
    warnings: list[str],
    executed: pd.DataFrame | None = None,
) -> Path:
    ensure_data_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(ch for ch in account.label if ch.isalnum() or ch in "-_").strip()
    run_dir = RUNS_DIR / f"{stamp}_{safe_label or 'account'}"
    run_dir.mkdir(parents=True, exist_ok=True)
    target.to_csv(run_dir / "target_portfolio.csv", index=False)
    holdings.to_csv(run_dir / "current_holdings.csv", index=False)
    plan.to_csv(run_dir / "execution_plan.csv", index=False)
    if executed is not None:
        executed.to_csv(run_dir / "execution_result.csv", index=False)
    (run_dir / "run_log.txt").write_text(
        "\n".join(
            [
                f"account={account.label}",
                f"user_id={account.user_id}",
                f"created_at={datetime.now().isoformat(timespec='seconds')}",
                "",
                "warnings:",
                *(warnings or ["none"]),
            ]
        ),
        encoding="utf-8",
    )
    return run_dir


def list_runs() -> pd.DataFrame:
    ensure_data_dirs()
    rows = []
    for path in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        plan_path = path / "execution_plan.csv"
        result_path = path / "execution_result.csv"
        plan = pd.read_csv(plan_path) if plan_path.exists() else pd.DataFrame()
        result = pd.read_csv(result_path) if result_path.exists() else pd.DataFrame()
        statuses = result["status"].astype(str) if "status" in result.columns else pd.Series(dtype=str)
        rows.append(
            {
                "run": path.name,
                "orders": len(plan),
                "buy_value": plan.loc[plan.get("action") == "BUY", "value"].sum()
                if not plan.empty
                else 0,
                "sell_value": plan.loc[plan.get("action") == "SELL", "value"].sum()
                if not plan.empty
                else 0,
                "placed": int(statuses.isin(["PLACED", "COMPLETE"]).sum())
                if not result.empty
                else 0,
                "failed": int((statuses == "FAILED").sum())
                if not result.empty
                else 0,
            }
        )
    return pd.DataFrame(rows)
