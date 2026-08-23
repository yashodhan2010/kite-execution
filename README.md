# Vriksha Execution

A local FastAPI + React console for Kite-based portfolio execution.

## What It Does

- Stores multiple Kite accounts locally.
- Logs in through Kite Connect.
- Imports a target portfolio CSV, including Vriksha-style exports.
- Detects Vriksha rebalance-history CSVs separately from full target portfolios.
- Fetches existing Kite holdings and cash.
- Builds a current-portfolio-aware execution plan.
- Places CNC market orders with Kite market protection only after explicit confirmation.
- Saves every dry run and execution under `data/runs/`.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
npm install
```

Run the backend:

```powershell
uvicorn api:app --host 127.0.0.1 --port 8001
```

Run the frontend in another terminal:

```powershell
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

Create a Kite Connect app with redirect URL:

```text
http://127.0.0.1:5173
```

## Login Flow

Click `Open Kite Login` from the selected account. Complete the Zerodha login in
Kite's own page. When Kite redirects back with `request_token`, the frontend
captures it and the backend generates the access token automatically.

The app does not store Kite passwords or TOTP seeds. The `request_token` input is
kept only as a fallback if browser redirect capture is unavailable.

## Vriksha Direct Import

The app can pull subscribed strategy data directly from Vriksha if the Vriksha
site exposes:

```text
GET /api/execution/subscriptions
GET /api/execution/strategies/{strategy_id}/latest-model-portfolio.csv
GET /api/execution/strategies/{strategy_id}/rebalance-history.csv
```

Use the `Vriksha` import tab and provide:

```text
Base URL
Logged-in Cookie header, or a bearer token if Vriksha supports token auth
```

The fetched CSV is normalized through the same execution planner as uploaded
files.

For the cleanest login experience, Vriksha should expose:

```text
GET /execution/connect?redirect_uri=http://127.0.0.1:5173
```

Behavior:

```text
If not logged in, complete Vriksha login first.
Generate a short-lived execution API token for the logged-in user.
Redirect to:
http://127.0.0.1:5173?vriksha_token={token}&vriksha_base_url=https://www.vriksha-capital.com
```

The local app captures `vriksha_token` and uses it as a bearer token for the
execution export endpoints.

## Accepted Portfolio Columns

The importer looks for a symbol column:

```text
tradingsymbol, trading_symbol, symbol, ticker, stock, name
```

Optional exchange column:

```text
exchange, segment
```

And at least one target column:

```text
target_quantity, quantity, qty, shares, target_qty
target_weight, weight, allocation, allocation_%, target_%
target_value, value, amount, investment_amount, target_amount
```

If exchange is missing, NSE is assumed.

## Vriksha Templates

Latest model portfolio exports are treated as complete targets:

```text
symbol, company, sector, marketcap, weight, note
```

Because this is a full model portfolio, existing holdings absent from this file are
planned as exits.

Rebalance history exports are treated as latest-date change logs:

```text
date, symbol, action, old_weight, new_weight, summary
```

Only rows from the latest rebalance date are planned. Symbols not present in that
latest rebalance event are left untouched.

## Execution

Orders are submitted as:

```text
product = CNC
order_type = MARKET
market_protection = -1 by default
```

`market_protection` can be changed in the sidebar. Use `-1` for Kite's automatic
market protection, or a custom percentage from above `0` to `100`.
