import { Panel, Metric, StatusPill } from "../../components/UI";
import { instruments, positions, riskMetrics, services, vaults } from "../../data/mockData";
import { money, pct } from "../../utils/format";
export function DeskDashboard(){
 const pnl=positions.reduce((a,p)=>a+p.unrealizedPnl+p.realizedPnl,0);
 const gross=positions.reduce((a,p)=>a+p.notional,0);
 return <div className="page">
  <div className="hero-row"><div><h1>Digital Assets Desk</h1><p>Trading, execution, risk, custody, settlement and operations</p></div><StatusPill status="SIMULATED · NOT PRODUCTION"/></div>
  <div className="metric-grid six"><Metric label="Desk P&L" value={money(pnl)} tone="positive"/><Metric label="Gross Notional" value={money(gross,0)}/><Metric label="Open Positions" value={`${positions.length}`}/><Metric label="Working Orders" value="3"/><Metric label="Vault Assets" value={money(vaults.reduce((a,v)=>a+v.value,0),0)}/><Metric label="Risk State" value="NORMAL" tone="positive"/></div>
  <div className="grid two">
   <Panel title="Markets"><table><thead><tr><th>Instrument</th><th>Last</th><th>24H</th><th>Venue</th></tr></thead><tbody>{instruments.map(x=><tr key={x.symbol}><td className="strong">{x.symbol}</td><td>{x.price.toLocaleString()}</td><td className={x.change24h>=0?"positive":"negative"}>{pct(x.change24h)}</td><td>{x.venue}</td></tr>)}</tbody></table></Panel>
   <Panel title="Risk Utilization">{riskMetrics.map(r=><div className="limit-row" key={r.label}><div><span>{r.label}</span><b>{r.value}</b></div><div className="bar"><i style={{width:`${r.utilization}%`}}/></div><small>{r.limit ? `Limit ${r.limit}`:""}</small></div>)}</Panel>
  </div>
  <Panel title="Platform Health"><div className="service-grid">{services.map(s=><div className="service" key={s.name}><StatusPill status={s.status}/><b>{s.name}</b><span>{s.latencyMs} ms</span><small>{s.throughput}</small></div>)}</div></Panel>
 </div>
}
