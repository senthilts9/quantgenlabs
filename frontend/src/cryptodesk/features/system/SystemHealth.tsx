import { Panel, Metric, StatusPill } from "../../components/UI";
import { services } from "../../data/mockData";
export function SystemHealth(){
 return <div className="page"><div className="hero-row"><div><h1>Connectivity & Health</h1><p>Service heartbeat, latency, throughput and venue/feed connectivity</p></div><StatusPill status="5 HEALTHY · 1 NORMAL"/></div>
 <div className="metric-grid four"><Metric label="Market Data P50" value="3.8 ms"/><Metric label="OMS P99" value="18.4 ms"/><Metric label="Risk Compute" value="82K calc/s"/><Metric label="Events Today" value="18.7M"/></div>
 <Panel title="Service Health"><table><thead><tr><th>SERVICE</th><th>STATUS</th><th>LATENCY</th><th>THROUGHPUT</th><th>LAST HEARTBEAT</th></tr></thead><tbody>{services.map(s=><tr key={s.name}><td className="strong">{s.name}</td><td><StatusPill status={s.status}/></td><td>{s.latencyMs} ms</td><td>{s.throughput}</td><td className="mono">{s.lastHeartbeat}</td></tr>)}</tbody></table></Panel>
 <div className="grid two"><Panel title="Venue Connectivity"><div className="service-grid">{["SIM-X1 WebSocket","SIM-X2 WebSocket","XRPL Testnet","EVM Testnet"].map(x=><div className="service" key={x}><StatusPill status="HEALTHY"/><b>{x}</b><small>Heartbeat OK</small></div>)}</div></Panel><Panel title="Event Pipeline"><div className="pipeline">{["Market Data","Normalizer","Kafka / Redpanda","OMS / Risk","Position / TCA","UI"].map((x,i)=><span key={x}>{x}{i<5&&<b>→</b>}</span>)}</div></Panel></div>
 </div>
}
