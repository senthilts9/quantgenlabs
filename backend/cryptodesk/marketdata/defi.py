"""DeFi market data: aggregator prices, on-chain oracles, and AMM pool state.

Three genuinely different kinds of price, and conflating them is a real source
of bugs:

* **Aggregator** (DefiLlama) - a convenience price assembled off-chain. Free,
  keyless, broad coverage, and carries a `confidence` score you should read.
  Fine for marking a portfolio, wrong for settling anything.
* **Oracle** (Chainlink) - what on-chain contracts actually believe. Updates
  on a deviation threshold or a heartbeat, so it is *intentionally* stale
  between updates. If you are modelling a liquidation, this is the price that
  triggers it, not the spot price on any exchange.
* **AMM pool** (Uniswap v3 `slot0`) - the instantaneous marginal price in one
  pool. It is the price you would *pay* only for an infinitesimal trade; any
  real size moves along the curve. Manipulable within a block, which is why
  lending protocols use TWAPs or oracles instead.

All on-chain reads go through `eth_call` against a public RPC. Read-only,
keyless, and pointed at Sepolia by default, consistent with the rest of the
package refusing to touch mainnet without an explicit opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from ..core.types import Quote, dec, now_ms
from .http import DEFAULT_CLIENT, HttpClient, TransportError

# ---------------------------------------------------------------------------
# DefiLlama - free, no key, no account
# ---------------------------------------------------------------------------

class DefiLlamaFeed:
    """Prices and yields from DefiLlama's open API."""

    name = "defillama"
    coins_url = "https://coins.llama.fi"
    api_url = "https://api.llama.fi"
    yields_url = "https://yields.llama.fi"

    def __init__(self, client: Optional[HttpClient] = None):
        self.client = client or DEFAULT_CLIENT
        # Llama is generous but not unlimited; keep well under the published cap.
        self.client.set_rate(self.coins_url, rate=4.0)

    def prices(self, coin_ids: List[str]) -> Dict[str, Quote]:
        """Current prices for DefiLlama coin identifiers.

        Identifiers are `chain:address` (`ethereum:0xA0b8...`) or
        `coingecko:slug`. Returns only the coins that resolved - a caller
        asking for five tokens and getting three back needs to see which three,
        not a silently zero-filled dict.
        """
        if not coin_ids:
            return {}
        raw = self.client.get("%s/prices/current/%s" % (self.coins_url, ",".join(coin_ids)))
        out: Dict[str, Quote] = {}
        for key, entry in (raw.get("coins") or {}).items():
            price = entry.get("price")
            if price is None:
                continue
            ts = entry.get("timestamp")
            out[key] = Quote(
                market=entry.get("symbol") or key,
                price=dec(price),
                timestamp_ms=int(ts) * 1000 if ts else now_ms(),
                source="%s(conf=%s)" % (self.name, entry.get("confidence", "?")),
            )
        return out

    def price(self, coin_id: str) -> Optional[Quote]:
        return self.prices([coin_id]).get(coin_id)

    def yield_pools(self, min_tvl_usd: float = 1_000_000,
                    limit: int = 50) -> List[Dict]:
        """Top yield pools by TVL.

        Filtered by TVL because the head of an unfiltered APY sort is always
        tiny pools with meaningless percentages - a pool with $400 of TVL
        printing 9000% APY is noise, not an opportunity.
        """
        raw = self.client.get("%s/pools" % self.yields_url)
        pools = raw.get("data", []) if isinstance(raw, dict) else []
        filtered = [p for p in pools if (p.get("tvlUsd") or 0) >= min_tvl_usd]
        filtered.sort(key=lambda p: p.get("apy") or 0, reverse=True)
        return filtered[:limit]

    def protocol_tvl(self, slug: str) -> Optional[Decimal]:
        try:
            raw = self.client.get("%s/tvl/%s" % (self.api_url, slug))
        except Exception:
            return None
        try:
            return dec(raw)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# JSON-RPC / on-chain reads
# ---------------------------------------------------------------------------

#: Keyless public RPCs. Sepolia first - this package is testnet-by-default.
PUBLIC_RPCS = {
    "sepolia": [
        "https://ethereum-sepolia-rpc.publicnode.com",
        "https://rpc.sepolia.org",
    ],
    "ethereum": [
        "https://ethereum-rpc.publicnode.com",
        "https://eth.llamarpc.com",
    ],
    "base-sepolia": [
        "https://base-sepolia-rpc.publicnode.com",
    ],
}


class JsonRpc:
    """Minimal Ethereum JSON-RPC client with endpoint failover.

    Public RPCs are best-effort infrastructure: they rate limit, they go down,
    and they occasionally return stale state. Rotating through a list on
    failure is the difference between a flaky read and a hard outage.
    """

    def __init__(self, chain: str = "sepolia",
                 urls: Optional[List[str]] = None,
                 client: Optional[HttpClient] = None):
        self.chain = chain
        self.urls = urls or PUBLIC_RPCS.get(chain, PUBLIC_RPCS["sepolia"])
        self.client = client or DEFAULT_CLIENT
        self._id = 0

    def call(self, method: str, params: List) -> object:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        last: Optional[Exception] = None
        for url in self.urls:
            try:
                raw = self.client.post(url, body=payload)
                if isinstance(raw, dict) and "error" in raw:
                    # A JSON-RPC error is the node rejecting the call - it will
                    # fail the same way on every endpoint, so stop rotating.
                    raise TransportError("rpc error: %s" % raw["error"])
                return raw.get("result") if isinstance(raw, dict) else None
            except TransportError:
                raise
            except Exception as exc:
                last = exc
                continue
        raise TransportError("all RPC endpoints failed for %s: %s" % (self.chain, last))

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        result = self.call("eth_call", [{"to": to, "data": data}, block])
        return result if isinstance(result, str) else "0x"

    def block_number(self) -> int:
        return int(str(self.call("eth_blockNumber", [])), 16)


def _word(hexstr: str, index: int) -> int:
    """Read the nth 32-byte word of ABI-encoded return data as an unsigned int."""
    body = hexstr[2:] if hexstr.startswith("0x") else hexstr
    start = index * 64
    chunk = body[start:start + 64]
    if len(chunk) < 64:
        raise ValueError("return data too short: wanted word %d of %r" % (index, hexstr))
    return int(chunk, 16)


def _signed(value: int, bits: int = 256) -> int:
    """Reinterpret a word as two's-complement signed.

    Chainlink's `answer` is int256. A negative answer is rare but real - some
    feeds quote spreads or rates that can go below zero - and reading it as
    unsigned turns a small negative number into ~1.16e77.
    """
    limit = 1 << (bits - 1)
    return value - (1 << bits) if value >= limit else value


@dataclass(frozen=True)
class ChainlinkFeed:
    name: str
    address: str
    chain: str = "sepolia"


#: Chainlink aggregators on Sepolia. Addresses are stable but Chainlink does
#: retire feeds; `ChainlinkOracle.read` surfaces an empty return rather than
#: fabricating a price if one goes away.
SEPOLIA_FEEDS = {
    "BTC-USD": ChainlinkFeed("BTC/USD", "0x1b44F3514812d835EB1BDB0acB33d3fA3351Ee43"),
    "ETH-USD": ChainlinkFeed("ETH/USD", "0x694AA1769357215DE4FAC081bf1f309aDC325306"),
    "LINK-USD": ChainlinkFeed("LINK/USD", "0xc59E3633BAAC79493d908e63626716e204A45EdF"),
}


class ChainlinkOracle:
    """Reads `latestRoundData()` from a Chainlink aggregator."""

    # Function selectors: keccak256("latestRoundData()")[:4] and decimals().
    SEL_LATEST_ROUND = "0xfeaf968c"
    SEL_DECIMALS = "0x313ce567"

    def __init__(self, rpc: Optional[JsonRpc] = None, chain: str = "sepolia"):
        self.rpc = rpc or JsonRpc(chain)
        self._decimals_cache: Dict[str, int] = {}

    def decimals(self, address: str) -> int:
        """Feed decimals, cached - it never changes for a deployed aggregator."""
        if address not in self._decimals_cache:
            raw = self.rpc.eth_call(address, self.SEL_DECIMALS)
            self._decimals_cache[address] = _word(raw, 0) if raw and raw != "0x" else 8
        return self._decimals_cache[address]

    def read(self, market: str) -> Optional[Quote]:
        """Latest oracle price for a canonical market symbol.

        `updatedAt` becomes the quote timestamp, so the staleness rule in the
        validator measures the age of the *oracle round* rather than the age of
        our HTTP request. Those differ by design: a feed with a one-hour
        heartbeat is legitimately 59 minutes stale and still authoritative.
        """
        feed = SEPOLIA_FEEDS.get(market.upper())
        if feed is None:
            return None
        raw = self.rpc.eth_call(feed.address, self.SEL_LATEST_ROUND)
        if not raw or raw == "0x":
            return None
        try:
            answer = _signed(_word(raw, 1))
            updated_at = _word(raw, 3)
        except ValueError:
            return None
        if answer <= 0:
            return None
        price = Decimal(answer) / (Decimal(10) ** self.decimals(feed.address))
        return Quote(
            market=market.upper(),
            price=price,
            timestamp_ms=updated_at * 1000 if updated_at else now_ms(),
            source="chainlink:%s@%s" % (feed.name, self.rpc.chain),
        )


class UniswapV3Pool:
    """Reads the marginal price out of a Uniswap v3 pool's `slot0`.

    `sqrtPriceX96` is the square root of the token1/token0 price in Q64.96
    fixed point. The conversion squares it and divides by 2^192, then corrects
    for the two tokens' differing decimals.

    This is a spot read of a single pool and is manipulable within one block -
    it is here to show what an AMM price *is*, not as something to mark a book
    against. For anything that matters, use the pool's TWAP oracle or a
    Chainlink feed.
    """

    SEL_SLOT0 = "0x3850c7bd"

    def __init__(self, address: str, token0_decimals: int = 18,
                 token1_decimals: int = 6, rpc: Optional[JsonRpc] = None,
                 chain: str = "ethereum"):
        self.address = address
        self.token0_decimals = token0_decimals
        self.token1_decimals = token1_decimals
        self.rpc = rpc or JsonRpc(chain)

    def price(self) -> Optional[Decimal]:
        raw = self.rpc.eth_call(self.address, self.SEL_SLOT0)
        if not raw or raw == "0x":
            return None
        try:
            sqrt_price_x96 = _word(raw, 0)
        except ValueError:
            return None
        if sqrt_price_x96 == 0:
            return None
        # Stay in integers until the final division: squaring a 160-bit value
        # as a float loses the low bits entirely.
        numerator = Decimal(sqrt_price_x96) ** 2
        price = numerator / (Decimal(2) ** 192)
        return price * (Decimal(10) ** (self.token0_decimals - self.token1_decimals))


def defi_snapshot(markets: Optional[List[str]] = None,
                  chain: str = "sepolia") -> Dict[str, Dict[str, object]]:
    """Side-by-side aggregator and oracle prices, with the basis between them.

    The gap is the interesting number. A wide aggregator-vs-oracle basis means
    the oracle has not updated through a move, which is exactly the condition
    under which on-chain liquidations lag the market.
    """
    markets = markets or ["BTC-USD", "ETH-USD"]
    llama = DefiLlamaFeed()
    oracle = ChainlinkOracle(chain=chain)
    slugs = {"BTC-USD": "coingecko:bitcoin", "ETH-USD": "coingecko:ethereum",
             "LINK-USD": "coingecko:chainlink"}

    agg: Dict[str, Quote] = {}
    try:
        wanted = [slugs[m] for m in markets if m in slugs]
        by_slug = llama.prices(wanted)
        for m in markets:
            slug = slugs.get(m)
            if slug and slug in by_slug:
                agg[m] = by_slug[slug]
    except Exception:
        pass

    out: Dict[str, Dict[str, object]] = {}
    for m in markets:
        row: Dict[str, object] = {"market": m}
        a = agg.get(m)
        if a is not None:
            row["aggregator"] = str(a.price)
            row["aggregator_source"] = a.source
        try:
            o = oracle.read(m)
        except Exception:
            o = None
        if o is not None:
            row["oracle"] = str(o.price)
            row["oracle_age_s"] = o.age_ms() // 1000
            row["oracle_source"] = o.source
        if a is not None and o is not None and o.price > 0:
            basis = (a.price - o.price) / o.price * Decimal(10000)
            row["basis_bps"] = str(basis.quantize(Decimal("0.1")))
        out[m] = row
    return out
