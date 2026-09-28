import { Panel, Metric, StatusPill } from "../../components/UI";
import { positions } from "../../data/mockData";
import { money } from "../../utils/format";
export function Positions(){
 const upnl=positions.reduce((a,p)=>a+p.unrealizedPnl,0), rpnl=positions.reduce((a,p)=>a+p.realizedPnl,0);
 return <div className="page"><div className="hero-row"><div><h1>Positions</h1><p>Real-time position, mark-to-market and margin view</p></div><StatusPill status="POSITION SERVICE · HEALTHY"/></div>
 <div className="metric-grid four"><Metric label="Unrealized P&L" value={money(upnl)} tone="positive"/><Metric label="Realized P&L" value={money(rpnl)} tone="positive"/><Metric label="Gross Notional" value={money(positions.reduce((a,p)=>a+p.notional,0),0)}/><Metric label="Margin Used" value={money(positions.reduce((a,p)=>a+p.marginUsed,0),0)}/></div>
 <Panel><table><thead><tr><th>INSTRUMENT</th><th>SIDE</th><th>QTY</th><th>AVG ENTRY</th><th>MARK</th><th>NOTIONAL</th><th>UNREALIZED</th><th>REALIZED</th><th>MARGIN</th></tr></thead><tbody>{positions.map(p=><tr key={p.symbol}><td className="strong">{p.symbol}</td><td>{p.side}</td><td>{p.qty.toLocaleString()}</td><td>{p.avgEntry.toLocaleString()}</td><td>{p.mark.toLocaleString()}</td><td>{money(p.notional)}</td><td className={p.unrealizedPnl>=0?"positive":"negative"}>{money(p.unrealizedPnl)}</td><td className={p.realizedPnl>=0?"positive":"negative"}>{money(p.realizedPnl)}</td><td>{money(p.marginUsed)}</td></tr>)}</tbody></table></Panel></div>
}
