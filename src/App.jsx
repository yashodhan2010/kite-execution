import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  CheckCircle2,
  FileUp,
  KeyRound,
  Play,
  RefreshCw,
  ShieldCheck,
  Wallet,
} from 'lucide-react';
import './styles.css';

const API = 'http://127.0.0.1:8001';

function App() {
  const [accounts, setAccounts] = useState([]);
  const [active, setActive] = useState('');
  const [loginUrl, setLoginUrl] = useState('');
  const [requestToken, setRequestToken] = useState('');
  const [accountForm, setAccountForm] = useState({ label: '', api_key: '', api_secret: '' });
  const [file, setFile] = useState(null);
  const [settings, setSettings] = useState({ min_order_value: 500, max_order_value: 0, market_protection: -1 });
  const [plan, setPlan] = useState(null);
  const [result, setResult] = useState(null);
  const [diagnostics, setDiagnostics] = useState(null);
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    refresh();
    completeRedirectLogin();
  }, []);

  const activeAccount = useMemo(
    () => accounts.find((account) => account.label === active),
    [accounts, active],
  );

  async function refresh() {
    setError('');
    const [accountRes, runRes] = await Promise.all([
      fetch(`${API}/api/accounts`),
      fetch(`${API}/api/runs`),
    ]);
    const accountData = await accountRes.json();
    const runData = await runRes.json();
    setAccounts(accountData);
    setRuns(runData.runs || []);
    if (!active && accountData.length) setActive(accountData[0].label);
  }

  async function saveAccount(event) {
    event.preventDefault();
    setBusy('Saving account');
    await call('/api/accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(accountForm),
    });
    setAccountForm({ label: '', api_key: '', api_secret: '' });
    await refresh();
    setBusy('');
  }

  async function getLoginUrl() {
    if (!active) return;
    setBusy('Opening login');
    const data = await call(`/api/login-url/${encodeURIComponent(active)}`);
    setLoginUrl(data.login_url);
    localStorage.setItem('kite_login_account', active);
    window.open(data.login_url, '_blank', 'noopener,noreferrer');
    setBusy('');
  }

  async function completeRedirectLogin() {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('request_token');
    const status = params.get('status');
    const label = localStorage.getItem('kite_login_account');
    if (!token || status === 'error' || !label) return;

    try {
      setBusy(`Completing login for ${label}`);
      await call('/api/complete-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, request_token: token }),
      });
      localStorage.removeItem('kite_login_account');
      window.history.replaceState({}, document.title, window.location.pathname);
      setActive(label);
      await refresh();
    } catch {
      window.history.replaceState({}, document.title, window.location.pathname);
    } finally {
      setBusy('');
    }
  }

  async function completeLogin() {
    setBusy('Completing login');
    await call('/api/complete-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: active, request_token: requestToken }),
    });
    setRequestToken('');
    await refresh();
    setBusy('');
  }

  async function runDiagnostics() {
    if (!active) return;
    setBusy('Checking permissions');
    const data = await call(`/api/diagnostics/${encodeURIComponent(active)}`);
    setDiagnostics(data);
    setBusy('');
  }

  async function generatePlan() {
    if (!file || !active) return;
    setBusy('Generating plan');
    setResult(null);
    setConfirm(false);
    const form = new FormData();
    form.append('label', active);
    form.append('min_order_value', settings.min_order_value);
    form.append('max_order_value', settings.max_order_value);
    form.append('file', file);
    const data = await call('/api/plan', { method: 'POST', body: form });
    setPlan(data);
    setBusy('');
  }

  async function execute() {
    if (!plan || !confirm) return;
    setBusy('Executing orders');
    const data = await call('/api/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan_id: plan.plan_id,
        market_protection: Number(settings.market_protection),
      }),
    });
    setResult(data);
    await refresh();
    setBusy('');
  }

  async function call(path, options = {}) {
    setError('');
    const response = await fetch(`${API}${path}`, options);
    const data = await response.json();
    if (!response.ok) {
      setBusy('');
      setError(data.detail || 'Request failed');
      throw new Error(data.detail || 'Request failed');
    }
    return data;
  }

  const summary = plan?.summary || {};

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Activity size={22} /></div>
          <div>
            <h1>Vriksha Execution</h1>
            <p>Local rebalance console</p>
          </div>
        </div>

        <section className="panel">
          <div className="panel-title">Accounts</div>
          <div className="account-list">
            {accounts.map((account) => (
              <button
                key={account.label}
                className={account.label === active ? 'account active' : 'account'}
                onClick={() => setActive(account.label)}
              >
                <span>{account.label}</span>
                <small>{account.connected ? account.user_id || 'Connected' : 'Login needed'}</small>
              </button>
            ))}
          </div>
        </section>

        <form className="panel compact" onSubmit={saveAccount}>
          <div className="panel-title">Add Account</div>
          <input placeholder="Account label" value={accountForm.label} onChange={(e) => setAccountForm({ ...accountForm, label: e.target.value })} />
          <input placeholder="API key" value={accountForm.api_key} onChange={(e) => setAccountForm({ ...accountForm, api_key: e.target.value })} />
          <input placeholder="API secret" type="password" value={accountForm.api_secret} onChange={(e) => setAccountForm({ ...accountForm, api_secret: e.target.value })} />
          <button className="primary" type="submit">Save Account</button>
        </form>

        <section className="panel compact">
          <div className="panel-title">Execution Settings</div>
          <label>Market protection %</label>
          <input type="number" step="0.5" min="-1" max="100" value={settings.market_protection} onChange={(e) => setSettings({ ...settings, market_protection: e.target.value })} />
          <label>Minimum order value</label>
          <input type="number" value={settings.min_order_value} onChange={(e) => setSettings({ ...settings, min_order_value: e.target.value })} />
          <label>Max order value</label>
          <input type="number" value={settings.max_order_value} onChange={(e) => setSettings({ ...settings, max_order_value: e.target.value })} />
        </section>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <h2>Execution Desk</h2>
            <p>{activeAccount ? `${activeAccount.label} · ${activeAccount.connected ? 'Connected' : 'Not connected'}` : 'No account selected'}</p>
          </div>
          <button className="ghost" onClick={refresh}><RefreshCw size={17} /> Refresh</button>
        </header>

        {error && <div className="alert danger">{error}</div>}
        {busy && <div className="alert">{busy}</div>}

        <section className="grid two">
          <div className="panel action-panel">
            <div className="step-icon"><KeyRound size={20} /></div>
            <h3>Kite Login</h3>
            <p>{activeAccount?.connected ? 'Session token is available for this account.' : 'Open Kite, complete login, and the app will capture the token from the redirect.'}</p>
            <div className="button-row">
              <button className="secondary" disabled={!active} onClick={getLoginUrl}>Open Kite Login</button>
              <button className="secondary" disabled={!activeAccount?.connected} onClick={runDiagnostics}>Check Permissions</button>
              {loginUrl && <a className="link-button" href={loginUrl} target="_blank" rel="noreferrer">Login URL</a>}
            </div>
            <div className="token-row">
              <input placeholder="request_token fallback" value={requestToken} onChange={(e) => setRequestToken(e.target.value)} />
              <button className="primary" disabled={!requestToken} onClick={completeLogin}>Complete</button>
            </div>
            {diagnostics && (
              <div className="diagnostics">
                {diagnostics.results.map((item) => (
                  <div className={item.status === 'OK' ? 'diag ok' : 'diag fail'} key={item.check}>
                    <strong>{item.check}</strong>
                    <span>{item.status}</span>
                    {item.message && <small>{item.message}</small>}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="panel action-panel">
            <div className="step-icon"><FileUp size={20} /></div>
            <h3>Import Target</h3>
            <p>Use Vriksha latest portfolio or rebalance history CSV.</p>
            <input className="file" type="file" accept=".csv" onChange={(e) => setFile(e.target.files?.[0] || null)} />
            <button className="primary wide" disabled={!file || !activeAccount?.connected} onClick={generatePlan}>Generate Execution Plan</button>
          </div>
        </section>

        <section className="metrics">
          <Metric icon={<Wallet />} label="Cash" value={money(plan?.cash || 0)} />
          <Metric icon={<Play />} label="Orders" value={summary.orders || 0} />
          <Metric icon={<ShieldCheck />} label="Buy Value" value={money(summary.buy_value || 0)} />
          <Metric icon={<CheckCircle2 />} label="Sell Value" value={money(summary.sell_value || 0)} />
        </section>

        {plan && (
          <section className="panel">
            <div className="section-head">
              <div>
                <h3>Execution Plan</h3>
                <p>{plan.import_kind === 'rebalance_history' ? 'Latest rebalance changes only; unchanged holdings stay untouched.' : 'Full target portfolio; absent holdings are planned as exits.'}</p>
              </div>
              <div className="badge">{plan.import_kind.replace('_', ' ')}</div>
            </div>
            {plan.warnings?.map((warning) => <div className="alert warn" key={warning}>{warning}</div>)}
            <DataTable rows={plan.plan} />
            <div className="execute-bar">
              <label className="check">
                <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
                <span>I reviewed this plan and want to place market orders with market protection.</span>
              </label>
              <button className="danger-button" disabled={!confirm || !plan.plan.length} onClick={execute}>Execute Orders</button>
            </div>
          </section>
        )}

        {result && (
          <section className="panel">
            <div className="section-head">
              <div>
                <h3>Execution Result</h3>
                <p>Logged at {result.run_dir}</p>
              </div>
              <div className="badge">{result.summary.placed} placed · {result.summary.failed} failed</div>
            </div>
            <DataTable rows={result.result} />
          </section>
        )}

        <section className="panel">
          <div className="section-head">
            <div>
              <h3>Run Tracker</h3>
              <p>Saved dry runs and executed order records.</p>
            </div>
          </div>
          <DataTable rows={runs} empty="No runs yet." />
        </section>
      </section>
    </main>
  );
}

function Metric({ icon, label, value }) {
  return (
    <div className="metric">
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DataTable({ rows, empty = 'No rows.' }) {
  if (!rows?.length) return <div className="empty">{empty}</div>;
  const columns = Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column.replaceAll('_', ' ')}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (typeof value === 'number') return Number.isInteger(value) ? value : value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
  return String(value);
}

function money(value) {
  return Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

createRoot(document.getElementById('root')).render(<App />);
