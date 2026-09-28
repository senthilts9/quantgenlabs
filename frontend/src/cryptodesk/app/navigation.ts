export const globalNav = [
  {section:"", items:["Overview"]},
  {section:"RISK", items:["Market Risk","Factor & Sensitivity","Stress & Scenarios","Limits & Alerts"]},
  {section:"PRICING", items:["Equity Derivatives","Rates & Fixed Income"]},
  {section:"PORTFOLIO", items:["Optimization"]},
  {section:"DIGITAL ASSETS", items:["Crypto Desk"]},
  {section:"ANALYTICS", items:["Risk & Quant Hub","Backtesting","Market Data","Observability","Model Library","Risk Copilot"]}
];
/**
 * Screens backed by real data, as opposed to the Phase 1 fixtures.
 *
 * This distinction is shown in the nav deliberately. A reader who clicks from
 * a genuinely live perp board into a fabricated positions table, with nothing
 * marking the difference, learns to discount *both* — the mock screens end up
 * costing the real ones their credibility. Labelling is cheaper than removing
 * the fixtures and more honest than leaving them unmarked.
 */
export const LIVE_SCREENS = new Set([
  "Trading Workstation",   // venue book + quotes, venue instrument specs
  "Orders & Executions",   // parents/slices/fills read back from the run store
  "Perpetuals",            // mark/index/basis/funding/OI
  "Exposure & P&L",        // VaR/ES engines
  "Limits & Alerts",       // same engine, alert framing
  "On-chain Activity",     // Chainlink cross-check
  "Settlement",            // chain observations
  "Vaults & Wallets",      // on-chain balances
  "Markets",               // DEX listed universe, marked live
  "Options Board",         // DEX option chain + venue greeks
  "Strategy Vaults",       // on-chain vault TVL/ROI/drawdown
  "Funding & Tape",        // DEX funding history + public prints
]);

export const deskNav = [
  {group:"OVERVIEW", items:["Desk Dashboard"]},
  {group:"MARKETS", items:["Markets","Funding & Tape"]},
  {group:"TRADING", items:["Trading Workstation","Orders & Executions","Positions"]},
  {group:"EXECUTION", items:["Algo Execution","Execution & TCA"]},
  {group:"DERIVATIVES", items:["Perpetuals","Options Board","Strategy Vaults"]},
  {group:"RISK", items:["Exposure & P&L","Limits & Alerts"]},
  {group:"CUSTODY", items:["Vaults & Wallets","Transfers & Approvals"]},
  {group:"BLOCKCHAIN", items:["On-chain Activity","Settlement"]},
  {group:"OPERATIONS", items:["Reconciliation","Audit Trail"]},
  {group:"SYSTEM", items:["Connectivity & Health"]}
];
