import type { ChainTransaction, Execution, Instrument, Order, Position, ReconBreak, RiskMetric, ServiceHealth, Transfer, Vault, Wallet } from "../types/domain";
import { chainTransactions, executions, initialOrders, instruments, positions, reconBreaks, riskMetrics, services, transfers, vaults, wallets } from "../data/mockData";

export interface MarketDataProvider { getInstruments(): Promise<Instrument[]>; }
export interface ExecutionProvider { getOrders(): Promise<Order[]>; getExecutions(): Promise<Execution[]>; }
export interface PositionProvider { getPositions(): Promise<Position[]>; }
export interface RiskProvider { getRiskMetrics(): Promise<RiskMetric[]>; }
export interface CustodyProvider { getVaults(): Promise<Vault[]>; getWallets(): Promise<Wallet[]>; getTransfers(): Promise<Transfer[]>; }
export interface BlockchainProvider { getTransactions(): Promise<ChainTransaction[]>; }
export interface OperationsProvider { getBreaks(): Promise<ReconBreak[]>; }
export interface HealthProvider { getServices(): Promise<ServiceHealth[]>; }

// Phase 1 provider. Phase 2: replace each method with REST/WebSocket clients.
// Python: orchestration, OMS, analytics, data services.
// C++: latency-sensitive pricing/risk/market-data calculations.
// Public/test-network adapters: market data and test-ledger observations only.
export const mockProviders = {
  market: { async getInstruments(){ return instruments; } } satisfies MarketDataProvider,
  execution: { async getOrders(){ return initialOrders; }, async getExecutions(){ return executions; } } satisfies ExecutionProvider,
  positions: { async getPositions(){ return positions; } } satisfies PositionProvider,
  risk: { async getRiskMetrics(){ return riskMetrics; } } satisfies RiskProvider,
  custody: { async getVaults(){ return vaults; }, async getWallets(){ return wallets; }, async getTransfers(){ return transfers; } } satisfies CustodyProvider,
  blockchain: { async getTransactions(){ return chainTransactions; } } satisfies BlockchainProvider,
  operations: { async getBreaks(){ return reconBreaks; } } satisfies OperationsProvider,
  health: { async getServices(){ return services; } } satisfies HealthProvider
};
