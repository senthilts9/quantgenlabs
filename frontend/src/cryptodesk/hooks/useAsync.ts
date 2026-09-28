import { useEffect, useRef, useState } from "react";

export interface AsyncState<T> { data: T | null; loading: boolean; error: string | null; }

/**
 * Run an async provider call and expose loading/error/data as one value.
 *
 * Phase 1 read its data from module constants, so no screen had a loading or
 * an error state. Real providers are asynchronous and fallible, and a screen
 * that renders a blank table while a fetch is in flight is indistinguishable
 * from one reporting a genuinely empty book. This makes that difference
 * explicit at every call site.
 *
 * `pollMs` re-runs on an interval. The desk is poll-based for now: the backend
 * exposes REST only, so the alternative to polling is stale prices, not push.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs = 0): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ data: null, loading: true, error: null });
  // A slow request that resolves after the symbol changed would otherwise
  // overwrite the new symbol's data with the old one's.
  const generation = useRef(0);

  useEffect(() => {
    const mine = ++generation.current;
    let timer: number | undefined;
    let cancelled = false;

    const run = async () => {
      try {
        const data = await fn();
        if (!cancelled && mine === generation.current) setState({ data, loading: false, error: null });
      } catch (e) {
        if (!cancelled && mine === generation.current) {
          setState(prev => ({ data: prev.data, loading: false, error: (e as Error).message || String(e) }));
        }
      }
      if (!cancelled && pollMs > 0) timer = window.setTimeout(run, pollMs);
    };

    setState(prev => ({ ...prev, loading: true }));
    run();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
