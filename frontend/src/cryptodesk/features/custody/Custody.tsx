import { useState } from "react";
import { Panel, Metric, StatusPill, Tabs } from "../../components/UI";
import { vaults, wallets } from "../../data/mockData";
import { money, num } from "../../utils/format";
export function Custody(){
 const [tab,setTab]=useState("Vaults");
 return <div className="page"><div className="hero-row"><div><h1>Vaults & Wallets</h1><p>Institutional custody-style inventory, reservations and policy visibility</p></div><StatusPill status="TEST / SIMULATED ASSETS"/></div>
 <div className="metric-grid four"><Metric label="Total Assets" value={money(vaults.reduce((a,v)=>a+v.value,0),0)}/><Metric label="Available" value={money(vaults.reduce((a,v)=>a+v.available,0),0)}/><Metric label="Reserved" value={money(vaults.reduce((a,v)=>a+v.reserved,0),0)}/><Metric label="Wallets" value={`${wallets.length}`}/></div>
 <Tabs items={["Vaults","Wallets"]} active={tab} onChange={setTab}/>
 {tab==="Vaults"?<Panel><table><thead><tr><th>VAULT</th><th>POLICY</th><th>VALUE</th><th>AVAILABLE</th><th>RESERVED</th><th>WALLETS</th><th>STATUS</th></tr></thead><tbody>{vaults.map(v=><tr key={v.id}><td><b>{v.name}</b><small className="block">{v.id}</small></td><td>{v.policy}</td><td>{money(v.value)}</td><td>{money(v.available)}</td><td>{money(v.reserved)}</td><td>{v.wallets}</td><td><StatusPill status={v.status}/></td></tr>)}</tbody></table></Panel>:
 <Panel><table><thead><tr><th>WALLET</th><th>NETWORK</th><th>ASSET</th><th>ADDRESS</th><th>BALANCE</th><th>AVAILABLE</th><th>RESERVED</th><th>STATUS</th></tr></thead><tbody>{wallets.map(w=><tr key={w.id}><td><b>{w.label}</b><small className="block">{w.id}</small></td><td>{w.network}</td><td>{w.asset}</td><td className="mono">{w.address}</td><td>{num(w.balance)}</td><td>{num(w.available)}</td><td>{num(w.reserved)}</td><td><StatusPill status={w.status}/></td></tr>)}</tbody></table></Panel>}
 </div>
}
