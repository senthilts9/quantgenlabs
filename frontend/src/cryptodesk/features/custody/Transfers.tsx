import { useState } from "react";
import { Panel, StatusPill } from "../../components/UI";
import { transfers } from "../../data/mockData";
export function Transfers(){
 const [show,setShow]=useState(false);
 return <div className="page"><div className="hero-row"><div><h1>Transfers & Approvals</h1><p>Test/simulated asset movement workflow with approval states</p></div><button className="primary" onClick={()=>setShow(!show)}>New Demo Transfer</button></div>
 {show&&<Panel title="Create Demo Transfer"><div className="form-row"><label>From<select><option>Trading Vault</option><option>Treasury Vault</option></select></label><label>To<select><option>Settlement Vault</option><option>Demo Counterparty</option></select></label><label>Asset<select><option>XRP</option><option>ETH</option><option>USDC</option></select></label><label>Amount<input defaultValue="1000"/></label><button className="primary" onClick={()=>setShow(false)}>Create Approval Request</button></div><small className="disclaimer">UI simulation only; this does not transmit assets or sign blockchain transactions.</small></Panel>}
 <Panel title="Transfer Queue"><table><thead><tr><th>TIME</th><th>ID</th><th>TYPE</th><th>ASSET</th><th>AMOUNT</th><th>FROM</th><th>TO</th><th>NETWORK</th><th>TX</th><th>STATUS</th></tr></thead><tbody>{transfers.map(t=><tr key={t.id}><td>{t.time}</td><td className="link">{t.id}</td><td>{t.type}</td><td className="strong">{t.asset}</td><td>{t.amount.toLocaleString()}</td><td>{t.from}</td><td>{t.to}</td><td>{t.network}</td><td className="mono">{t.txHash||"—"}</td><td><StatusPill status={t.status}/></td></tr>)}</tbody></table></Panel>
 </div>
}
