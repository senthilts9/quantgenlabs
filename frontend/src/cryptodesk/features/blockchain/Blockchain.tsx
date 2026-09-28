import { Panel, Metric, StatusPill } from "../../components/UI";
import { chainTransactions } from "../../data/mockData";
export function Blockchain({settlement=false}:{settlement?:boolean}){
 return <div className="page"><div className="hero-row"><div><h1>{settlement?"Settlement":"On-chain Activity"}</h1><p>{settlement?"Trade-to-settlement traceability across internal records and test-ledger observations":"Test-network ledger observations and transaction status"}</p></div><StatusPill status="TEST NETWORKS ONLY"/></div>
 {!settlement?<><div className="metric-grid four"><Metric label="XRPL Testnet" value="VALIDATED" tone="positive"/><Metric label="EVM Testnet" value="HEALTHY" tone="positive"/><Metric label="Pending Transactions" value="0"/><Metric label="Ledger Lag" value="1.8 sec"/></div>
 <Panel title="Ledger Transactions"><table><thead><tr><th>TIME</th><th>HASH</th><th>NETWORK</th><th>TYPE</th><th>ASSET</th><th>AMOUNT</th><th>CONFIRMATIONS</th><th>STATUS</th></tr></thead><tbody>{chainTransactions.map(t=><tr key={t.hash}><td>{t.time}</td><td className="link mono">{t.hash}</td><td>{t.network}</td><td>{t.type}</td><td className="strong">{t.asset}</td><td>{t.amount.toLocaleString()}</td><td>{t.confirmations}</td><td><StatusPill status={t.status}/></td></tr>)}</tbody></table></Panel></>:
 <Panel title="Trade → Settlement Trace"><div className="settlement-flow">{["Trade QG-10421","Allocation AL-7781","Instruction SI-44021","Vault VLT-SET","Wallet W-XRP-01","Testnet TX A91F...120D","Ledger VALIDATED","Reconciled"].map((x,i)=><div key={x}><span>{String(i+1).padStart(2,"0")}</span><b>{x}</b><StatusPill status={i===7?"HEALTHY":"COMPLETE"}/></div>)}</div></Panel>}
 </div>
}
