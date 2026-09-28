import { Panel, Metric, StatusPill } from "../../components/UI";
import { auditEvents, reconBreaks } from "../../data/mockData";
export function Operations({audit=false}:{audit?:boolean}){
 return <div className="page"><div className="hero-row"><div><h1>{audit?"Audit Trail":"Reconciliation"}</h1><p>{audit?"Immutable-style operational event history for the demo":"OMS, position, custody and internal-ledger break management"}</p></div><StatusPill status="OPERATIONS"/></div>
 {!audit?<><div className="metric-grid four"><Metric label="Open Breaks" value="2"/><Metric label="High Severity" value="1" tone="warning"/><Metric label="Resolved Today" value="17"/><Metric label="STP Rate" value="99.82%"/></div><Panel title="Break Queue"><table><thead><tr><th>BREAK</th><th>SOURCE</th><th>TARGET</th><th>FIELD</th><th>EXPECTED</th><th>ACTUAL</th><th>SEVERITY</th><th>STATUS</th></tr></thead><tbody>{reconBreaks.map(b=><tr key={b.id}><td className="link">{b.id}</td><td>{b.source}</td><td>{b.target}</td><td>{b.field}</td><td>{b.expected}</td><td>{b.actual}</td><td><StatusPill status={b.severity}/></td><td><StatusPill status={b.status}/></td></tr>)}</tbody></table></Panel></>:
 <Panel title="Event History"><table><thead><tr><th>TIME</th><th>ACTOR</th><th>ACTION</th><th>ENTITY</th><th>RESULT</th></tr></thead><tbody>{auditEvents.map((a,i)=><tr key={i}><td>{a.time}</td><td className="mono">{a.actor}</td><td className="strong">{a.action}</td><td className="link">{a.entity}</td><td><StatusPill status={a.result}/></td></tr>)}</tbody></table></Panel>}
 </div>
}
