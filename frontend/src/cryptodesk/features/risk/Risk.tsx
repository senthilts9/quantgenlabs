import { useState } from "react";
import { Panel, Metric, StatusPill } from "../../components/UI";
import { useAsync } from "../../hooks/useAsync";
import { liveProviders } from "../../services/liveProviders";
import { money } from "../../utils/format";

const MARKETS = ["BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP"];
const NOTIONAL = 1_000_000;

/**
 * Market risk, computed rather than asserted.
 *
 * Public candles feed the in-house VaR/ES engines — historical, parametric,
 * Cornish-Fisher and Monte Carlo alongside the crypto-native EVT
 * peaks-over-threshold and EWMA-scaled estimators. Showing them together is
 * the point: the gap between a normal assumption and a fitted tail is the
 * whole argument for running EVT on this asset class.
 */
export function Risk({ alerts = false }: { alerts?: boolean }) {
  const [market, setMarket] = useState(MARKETS[0]);
  const risk = useAsync(
    () => liveProviders.getCryptoRisk(market, 0.99, NOTIONAL), [market], 120000);
  const d = risk.data;

  const byMethod = (m: string) => d?.methods.find(x => x.method.startsWith(m));
  const evt = byMethod("evt");
  const para = byMethod("parametric");
  // The headline comparison: how badly a normal assumption understates the
  // tail it is supposed to be measuring.
  const esGapPct = evt && para && para.es_pct > 0
    ? ((evt.es_pct - para.es_pct) / para.es_pct) * 100 : null;

  return <div className="page">
    <div className="hero-row">
      <div><h1>{alerts ? "Limits & Alerts" : "Market Risk"}</h1>
        <p>VaR / Expected Shortfall across six estimators, with out-of-sample backtests</p></div>
      <StatusPill status={risk.error ? "ENGINE ERROR"
        : d ? `LIVE · ${d.cppAvailable ? "C++" : "NUMPY"}` : "COMPUTING"} />
    </div>

    <div className="tabs">
      {MARKETS.map(m => <button key={m} className={m === market ? "active" : ""}
        onClick={() => setMarket(m)}>{m.replace("-USD-PERP", "")}</button>)}
    </div>

    {risk.error && <Panel><div className="empty">Engine unavailable — {risk.error}</div></Panel>}
    {!d && risk.loading && <Panel><div className="empty">Computing VaR…</div></Panel>}

    {d && <>
      <div className="metric-grid six">
        <Metric label="1d VaR 99% (EVT)" value={evt ? `${evt.var_pct.toFixed(2)}%` : "—"}
          sub={evt ? money(evt.var_currency, 0) : undefined} tone="warning" />
        <Metric label="Expected Shortfall" value={evt ? `${evt.es_pct.toFixed(2)}%` : "—"}
          sub={evt ? money(evt.es_currency, 0) : undefined} tone="warning" />
        <Metric label="Annualised Vol" value={`${d.annualisedPct.toFixed(1)}%`} />
        <Metric label="EWMA Vol (current)" value={`${d.ewmaAnnualisedPct.toFixed(1)}%`}
          sub={`regime ${d.regime}`} />
        <Metric label="Excess Kurtosis" value={d.excessKurtosis.toFixed(2)}
          sub={d.fatTailed ? "fat-tailed" : "near-normal"}
          tone={d.fatTailed ? "warning" : ""} />
        <Metric label="Observations" value={`${d.nReturns}`}
          sub={d.clean ? "clean series" : "gaps present"} />
      </div>

      <Panel title={`VaR / ES — ${d.market} · ${d.horizon} · ${(d.confidence * 100).toFixed(0)}% · on ${money(d.notional, 0)}`}>
        <table>
          <thead><tr>
            <th>ESTIMATOR</th><th>FAMILY</th><th>VaR %</th><th>ES %</th>
            <th>VaR</th><th>ES</th><th>NOTE</th>
          </tr></thead>
          <tbody>{[...d.methods].sort((a, b) => a.var_pct - b.var_pct).map(m => <tr key={m.method}>
            <td className="strong">{m.method}</td>
            <td>{m.family}</td>
            <td className="mono">{m.var_pct.toFixed(4)}</td>
            <td className="mono">{m.es_measured === false ? "—" : m.es_pct.toFixed(4)}</td>
            <td>{money(m.var_currency, 0)}</td>
            <td>{m.es_measured === false ? "—" : money(m.es_currency, 0)}</td>
            <td className="muted">
              {m.method.startsWith("evt") && m.gpd_xi != null
                ? `GPD ξ=${m.gpd_xi.toFixed(3)} · ${m.n_exceedances} exceedances`
                : m.es_measured === false ? "quantile adjustment; no ES integrated" : ""}
            </td>
          </tr>)}</tbody>
        </table>
      </Panel>

      <div className="grid two">
        <Panel title="Model Backtest — out of sample">
          <table>
            <thead><tr><th>MODEL</th><th>OBS</th><th>EXC</th><th>EXPECTED</th>
              <th>KUPIEC p</th><th>BASEL</th><th>COVERAGE</th></tr></thead>
            <tbody>{d.backtests.map(b => <tr key={b.method}>
              <td className="strong">{b.method}</td>
              <td>{b.observations}</td><td>{b.exceptions}</td>
              <td>{b.expected.toFixed(2)}</td>
              <td className="mono">{b.kupiec_p.toFixed(3)}</td>
              <td><StatusPill status={b.basel_zone} /></td>
              <td className={b.coverage_verdict === "REJECTED" ? "negative" : "positive"}>
                {b.coverage_verdict}</td>
            </tr>)}</tbody>
          </table>
          <p className="disclaimer">
            Each forecast is fitted only to data strictly before the day it is
            tested against. An in-sample backtest is the commonest way to
            publish a VaR model that looks calibrated and is not.
          </p>
        </Panel>

        <Panel title="Why the tail estimator matters">
          <div className="cost-grid">
            <div><span>Normal (parametric) ES</span>
              <b>{para ? `${para.es_pct.toFixed(2)}%` : "—"}</b></div>
            <div><span>Fitted tail (EVT) ES</span>
              <b className="warning">{evt ? `${evt.es_pct.toFixed(2)}%` : "—"}</b></div>
            <div><span>Understatement</span>
              <b className="negative">{esGapPct === null ? "—" : `${esGapPct.toFixed(0)}%`}</b></div>
            <div><span>On {money(d.notional, 0)}</span>
              <b className="negative">
                {evt && para ? money(evt.es_currency - para.es_currency, 0) : "—"}</b></div>
          </div>
          <p className="disclaimer">
            Measured excess kurtosis {d.excessKurtosis.toFixed(2)} — a normal
            distribution has 0. Assuming normality on this series understates
            the expected loss beyond VaR by the margin above.
            {d.historyNote ? ` ${d.historyNote}.` : ""}
            {d.incompleteDropped > 0
              ? " The venue's in-progress bar is excluded: a part-period return biases the conditional volatility low."
              : ""}
          </p>
        </Panel>
      </div>
    </>}
  </div>;
}
