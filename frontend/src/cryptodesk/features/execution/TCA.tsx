import { Panel, Metric, StatusPill } from "../../components/UI";
export function TCA(){
 const rows=[["QG-10425","BTC-USD-PERP","VWAP","77,714.45","77,711.62","77,712.08","+0.36","-0.21"],["QG-10424","ETH-USD-PERP","DIRECT","3,428.72","3,429.12","3,429.04","-1.17","+0.23"],["QG-10421","XRP-USD","SOR","2.3118","2.3098","2.3104","+8.65","+2.60"]];
 return <div className="page"><div className="hero-row"><div><h1>Execution & TCA</h1><p>Transaction cost analysis, benchmark comparison and venue quality</p></div><StatusPill status="ANALYTICS · MOCK"/></div>
 <div className="metric-grid four"><Metric label="Implementation Shortfall" value="-1.8 bps" tone="positive"/><Metric label="VWAP Performance" value="+0.7 bps" tone="positive"/><Metric label="Avg Slippage" value="1.4 bps"/><Metric label="Fill Quality" value="92.4%"/></div>
 <Panel title="Execution Quality"><table><thead><tr><th>ORDER</th><th>INSTRUMENT</th><th>ALGO</th><th>ARRIVAL</th><th>EXECUTED</th><th>VWAP</th><th>VS ARRIVAL (bps)</th><th>VS VWAP (bps)</th></tr></thead><tbody>{rows.map(r=><tr key={r[0]}>{r.map((x,i)=><td key={i} className={i===0?"link":i===1?"strong":""}>{x}</td>)}</tr>)}</tbody></table></Panel>
 <div className="grid two"><Panel title="Venue Fill Quality"><div className="bars">{[["SIM-X1",94],["SIM-X2",89],["SIM-AMM",76]].map(([n,v])=><div key={n}><span>{n}</span><div className="bar"><i style={{width:`${v}%`}}/></div><b>{v}%</b></div>)}</div></Panel><Panel title="Cost Decomposition"><div className="cost-grid"><div><span>Spread Cost</span><b>0.8 bps</b></div><div><span>Market Impact</span><b>1.2 bps</b></div><div><span>Fees</span><b>0.4 bps</b></div><div><span>Timing</span><b className="positive">-0.6 bps</b></div></div></Panel></div>
 </div>
}
