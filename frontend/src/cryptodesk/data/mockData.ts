import type { AuditEvent, BookLevel, ChainTransaction, Execution, Instrument, Order, Position, ReconBreak, RiskMetric, ServiceHealth, Transfer, Vault, Wallet } from "../types/domain";

// PHASE 1 MOCK DATA ONLY.
// Replace this module in Phase 2 with provider implementations backed by permitted public/test-network APIs,
// Python services and in-house C++ pricing/risk engines. UI components must not import external APIs directly.

export const instruments: Instrument[] = [
  { symbol:"BTC-USD-PERP", name:"Bitcoin Perpetual", assetClass:"PERPETUAL", price:77714.45, change24h:1.28, bid:77713.8, ask:77715.1, volume24h:1840000000, venue:"SIM-X1" },
  { symbol:"ETH-USD-PERP", name:"Ether Perpetual", assetClass:"PERPETUAL", price:2469.60, change24h:-0.42, bid:2469.43, ask:2469.78, volume24h:890000000, venue:"SIM-X1" },
  { symbol:"SOL-USD-PERP", name:"Solana Perpetual", assetClass:"PERPETUAL", price:100.54, change24h:2.61, bid:100.52, ask:100.56, volume24h:420000000, venue:"SIM-X2" },
  { symbol:"XRP-USD", name:"XRP Spot", assetClass:"SPOT", price:2.3118, change24h:0.86, bid:2.3114, ask:2.3122, volume24h:310000000, venue:"SIM-X2" }
];

export const orderBook: BookLevel[] = [
  {price:77721.2,size:0.82,total:8.91,side:"ASK"},{price:77719.8,size:1.44,total:8.09,side:"ASK"},
  {price:77718.4,size:2.08,total:6.65,side:"ASK"},{price:77716.9,size:1.72,total:4.57,side:"ASK"},
  {price:77715.1,size:2.85,total:2.85,side:"ASK"},{price:77713.8,size:3.08,total:3.08,side:"BID"},
  {price:77712.4,size:1.84,total:4.92,side:"BID"},{price:77710.7,size:2.41,total:7.33,side:"BID"},
  {price:77709.1,size:3.12,total:10.45,side:"BID"},{price:77707.6,size:1.95,total:12.40,side:"BID"}
];

export const initialOrders: Order[] = [
  {id:"QG-10425",parentId:"ALG-9012",time:"11:07:42.114",symbol:"BTC-USD-PERP",side:"BUY",type:"LIMIT",algo:"VWAP",qty:3,filled:1.5,limitPrice:77712,avgPrice:77711.62,status:"PARTIAL",venue:"SIM-X1"},
  {id:"QG-10424",time:"11:06:58.327",symbol:"ETH-USD-PERP",side:"SELL",type:"MARKET",algo:"DIRECT",qty:20,filled:20,avgPrice:2469.89,status:"FILLED",venue:"SIM-X2"},
  {id:"QG-10423",parentId:"ALG-9011",time:"11:04:11.502",symbol:"SOL-USD-PERP",side:"BUY",type:"LIMIT",algo:"TWAP",qty:500,filled:320,limitPrice:100.48,avgPrice:100.46,status:"WORKING",venue:"SIM-X2"},
  {id:"QG-10422",time:"11:02:09.843",symbol:"BTC-USD-PERP",side:"SELL",type:"LIMIT",algo:"DIRECT",qty:1.2,filled:0,limitPrice:78000,status:"WORKING",venue:"SIM-X1"},
  {id:"QG-10421",time:"10:59:47.201",symbol:"XRP-USD",side:"BUY",type:"LIMIT",algo:"SOR",qty:25000,filled:25000,limitPrice:2.31,avgPrice:2.3098,status:"FILLED",venue:"SIM-X2"}
];

export const executions: Execution[] = [
  {id:"EX-88731",orderId:"QG-10425",time:"11:07:43.018",symbol:"BTC-USD-PERP",side:"BUY",qty:0.75,price:77711.2,fee:5.83,venue:"SIM-X1"},
  {id:"EX-88730",orderId:"QG-10425",time:"11:07:42.711",symbol:"BTC-USD-PERP",side:"BUY",qty:0.75,price:77712.04,fee:5.83,venue:"SIM-X1"},
  {id:"EX-88729",orderId:"QG-10424",time:"11:06:58.419",symbol:"ETH-USD-PERP",side:"SELL",qty:20,price:2469.89,fee:13.72,venue:"SIM-X2"},
  {id:"EX-88728",orderId:"QG-10423",time:"11:05:51.122",symbol:"SOL-USD-PERP",side:"BUY",qty:80,price:100.45,fee:3.01,venue:"SIM-X2"}
];

export const positions: Position[] = [
  {symbol:"BTC-USD-PERP",side:"LONG",qty:12.4,avgEntry:76420.22,mark:77714.45,unrealizedPnl:16048.45,realizedPnl:2840.20,notional:963659.18,marginUsed:96365.92},
  {symbol:"ETH-USD-PERP",side:"SHORT",qty:84,avgEntry:2494.15,mark:2469.60,unrealizedPnl:2062.20,realizedPnl:798.10,notional:207446.40,marginUsed:20744.64},
  {symbol:"SOL-USD-PERP",side:"LONG",qty:1250,avgEntry:96.88,mark:100.54,unrealizedPnl:4575.00,realizedPnl:-224.25,notional:125675.00,marginUsed:12567.50},
  {symbol:"XRP-USD",side:"LONG",qty:82500,avgEntry:2.21,mark:2.3118,unrealizedPnl:8398.50,realizedPnl:910.00,notional:190723.50,marginUsed:0}
];

/**
 * Risk tiles, **derived from `positions` rather than typed in.**
 *
 * They used to be hardcoded, and they had drifted into contradicting the table
 * directly beneath them: the tile read a $4.82M gross exposure against
 * positions summing to $1.68M, and a 38.6% concentration against a book that
 * was 57% BTC. Both are pure arithmetic over the position list, so there was
 * never a second source for them to come from — only a second *copy*, which is
 * the version that goes stale.
 *
 * This matters more on a fixture screen than on a live one. A reader who
 * catches a header disagreeing with the rows under it stops trusting the
 * screens that are real, which is the expensive half of the mistake.
 */
const _gross = positions.reduce((a, p) => a + p.notional, 0);
const _net = positions.reduce(
  (a, p) => a + (p.side === "LONG" ? p.notional : -p.notional), 0);
const _margin = positions.reduce((a, p) => a + p.marginUsed, 0);
const _largest = positions.reduce((a, p) => Math.max(a, p.notional), 0);

/** Utilisation drives the colour, so a limit that is nearly met has to say so. */
const _zone = (pct: number): RiskMetric["status"] =>
  pct >= 90 ? "CRITICAL" : pct >= 75 ? "WARNING" : "NORMAL";

const _m = (v: number) => `$${(v / 1e6).toFixed(2)}M`;
const _k = (v: number) => `$${(v / 1e3).toFixed(1)}K`;

// VaR and ES cannot be derived from a position list — they need a model and a
// return series, which is what the LIVE Exposure & P&L screen runs. Here they
// stay fixtures, but scaled to this book's gross so the ratio between them and
// the exposure tiles stays believable.
const _var99 = _gross * 0.0381;
const _es = _var99 * 1.34;

export const riskMetrics: RiskMetric[] = [
  {label:"Gross Exposure",value:_m(_gross),limit:"$10.00M",
   utilization:+(100*_gross/10e6).toFixed(1),status:_zone(100*_gross/10e6)},
  {label:"Net Exposure",value:_m(_net),limit:"$5.00M",
   utilization:+(100*_net/5e6).toFixed(1),status:_zone(100*_net/5e6)},
  {label:"1D VaR 99%",value:_k(_var99),limit:"$500K",
   utilization:+(100*_var99/5e5).toFixed(1),status:_zone(100*_var99/5e5)},
  {label:"Expected Shortfall",value:_k(_es),limit:"$700K",
   utilization:+(100*_es/7e5).toFixed(1),status:_zone(100*_es/7e5)},
  {label:"Margin Utilization",value:`${(100*_margin/_gross).toFixed(1)}%`,limit:"65%",
   utilization:+(100*(_margin/_gross)/0.65).toFixed(1),status:_zone(100*(_margin/_gross)/0.65)},
  // 75%, not 50%. A four-instrument book is concentrated by construction — its
  // largest line cannot be under half without the other three being padding —
  // so a 50% limit produced a permanent 130% breach that read as a broken tile
  // rather than a risk signal. At 75% the same book sits in warning, which is
  // what a single-name concentration of 65% should actually look like.
  {label:"Concentration",value:`${(100*_largest/_gross).toFixed(1)}%`,limit:"75%",
   utilization:+(100*(_largest/_gross)/0.75).toFixed(1),status:_zone(100*(_largest/_gross)/0.75)},
];

export const vaults: Vault[] = [
  {id:"VLT-TRD",name:"Trading Vault",policy:"2-of-3 approval",value:1965000,available:1740000,reserved:225000,status:"HEALTHY",wallets:4},
  {id:"VLT-TRY",name:"Treasury Vault",policy:"3-of-5 approval",value:1480000,available:1420000,reserved:60000,status:"HEALTHY",wallets:3},
  {id:"VLT-SET",name:"Settlement Vault",policy:"2-of-3 approval",value:910000,available:780000,reserved:130000,status:"HEALTHY",wallets:5},
  {id:"VLT-COLD",name:"Cold Vault",policy:"Offline / demo",value:465000,available:465000,reserved:0,status:"NORMAL",wallets:2}
];

export const wallets: Wallet[] = [
  {id:"W-XRP-01",label:"XRPL Settlement",network:"XRPL TESTNET",asset:"XRP",address:"rDemo...8Qm1",balance:82450,available:80000,reserved:2450,status:"HEALTHY"},
  {id:"W-ETH-01",label:"EVM Settlement",network:"EVM TESTNET",asset:"ETH",address:"0x7aD...31F2",balance:184.2,available:170.2,reserved:14,status:"HEALTHY"},
  {id:"W-BTC-01",label:"BTC Simulation",network:"SIMULATED",asset:"BTC",address:"sim-btc...2041",balance:12.4,available:10.9,reserved:1.5,status:"NORMAL"},
  {id:"W-USDC-01",label:"Stablecoin Treasury",network:"EVM TESTNET",asset:"USDC",address:"0x91C...AA02",balance:680000,available:620000,reserved:60000,status:"HEALTHY"}
];

export const transfers: Transfer[] = [
  {id:"TR-55091",time:"10:54:22",type:"INTERNAL",asset:"XRP",amount:1250,from:"Trading Vault",to:"Settlement Vault",network:"XRPL TESTNET",status:"CONFIRMED",txHash:"A91F...120D"},
  {id:"TR-55090",time:"10:42:08",type:"WITHDRAWAL",asset:"USDC",amount:25000,from:"Treasury Vault",to:"Counterparty-03",network:"EVM TESTNET",status:"APPROVAL"},
  {id:"TR-55089",time:"10:18:31",type:"DEPOSIT",asset:"ETH",amount:12.5,from:"External Test Wallet",to:"Settlement Vault",network:"EVM TESTNET",status:"CONFIRMED",txHash:"0x7b...ae19"},
  {id:"TR-55088",time:"09:55:04",type:"INTERNAL",asset:"BTC",amount:1.5,from:"Treasury Vault",to:"Trading Vault",network:"SIMULATED",status:"PENDING"}
];

export const chainTransactions: ChainTransaction[] = [
  {hash:"A91F7D82...120D",time:"10:54:25",network:"XRPL TESTNET",type:"Payment",asset:"XRP",amount:1250,confirmations:1,status:"VALIDATED"},
  {hash:"0x7b21...ae19",time:"10:18:44",network:"EVM TESTNET",type:"Transfer",asset:"ETH",amount:12.5,confirmations:18,status:"VALIDATED"},
  {hash:"0x88c1...91aa",time:"09:48:19",network:"EVM TESTNET",type:"Token Transfer",asset:"USDC",amount:10000,confirmations:24,status:"VALIDATED"},
  {hash:"C20391AF...AA18",time:"09:31:11",network:"XRPL TESTNET",type:"Payment",asset:"XRP",amount:5000,confirmations:1,status:"VALIDATED"}
];

export const reconBreaks: ReconBreak[] = [
  {id:"BRK-2204",source:"OMS",target:"Position Service",field:"BTC Qty",expected:"12.4000",actual:"12.3998",severity:"MEDIUM",status:"INVESTIGATING"},
  {id:"BRK-2203",source:"Wallet",target:"Internal Ledger",field:"USDC Reserved",expected:"60,000",actual:"59,750",severity:"HIGH",status:"OPEN"},
  {id:"BRK-2202",source:"Execution",target:"TCA",field:"Venue Fee",expected:"13.72",actual:"13.70",severity:"LOW",status:"RESOLVED"}
];

export const services: ServiceHealth[] = [
  {name:"Market Data Gateway",status:"HEALTHY",latencyMs:3.8,throughput:"128K msg/s",lastHeartbeat:"11:08:02.114"},
  {name:"OMS / EMS",status:"HEALTHY",latencyMs:7.2,throughput:"1.4K ord/s",lastHeartbeat:"11:08:02.098"},
  {name:"Risk Service",status:"HEALTHY",latencyMs:1.4,throughput:"82K calc/s",lastHeartbeat:"11:08:02.107"},
  {name:"Position Service",status:"HEALTHY",latencyMs:4.1,throughput:"32K evt/s",lastHeartbeat:"11:08:02.089"},
  {name:"Custody Adapter",status:"NORMAL",latencyMs:18.7,throughput:"420 tx/s",lastHeartbeat:"11:08:01.992"},
  {name:"Testnet Gateway",status:"HEALTHY",latencyMs:42.3,throughput:"96 req/s",lastHeartbeat:"11:08:01.941"}
];

export const auditEvents: AuditEvent[] = [
  {time:"11:07:43.018",actor:"oms-service",action:"EXECUTION_RECORDED",entity:"EX-88731",result:"SUCCESS"},
  {time:"11:07:42.114",actor:"demo.trader",action:"ORDER_SUBMITTED",entity:"QG-10425",result:"ACCEPTED"},
  {time:"10:54:25.044",actor:"settlement-service",action:"TX_VALIDATED",entity:"TR-55091",result:"SUCCESS"},
  {time:"10:42:08.441",actor:"demo.ops",action:"TRANSFER_CREATED",entity:"TR-55090",result:"AWAITING_APPROVAL"},
  {time:"10:31:11.009",actor:"risk-service",action:"LIMIT_WARNING",entity:"CONCENTRATION",result:"WARNING"}
];
