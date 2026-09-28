"""Command-line interface.

Every command is read-only or paper by default. The two that could touch real
value - `keys set` and anything routed to a live venue - print the active
network guard first, so the state of the guard is never something you have to
remember.

Run `python3 -m cryptodesk <command>`.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from typing import List, Optional

from .config import Settings, describe_guard


def _print(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    """Reachability and capability report for everything configured."""
    from .config import ENDPOINTS
    from .marketdata.http import probe
    from .vault import AVAILABLE, EncryptedFileVault, MacKeychainVault

    settings = Settings.from_env()
    print(describe_guard(settings))
    print()

    print("optional libraries")
    print("  eth-account   %s  (EIP-712 signing: Hyperliquid, EVM onboarding)"
          % ("yes" if AVAILABLE["eth_account"] else "NO"))
    print("  starknet-py   %s  (Paradex order signing)"
          % ("yes" if AVAILABLE["starknet_py"] else "NO"))
    print("  cryptography  %s  (encrypted file vault)"
          % ("yes" if EncryptedFileVault.available() else "NO"))
    print("  macOS keychain %s (recommended vault backend here)"
          % ("yes" if MacKeychainVault.available() else "NO"))
    print()

    print("endpoints")
    for name in sorted(ENDPOINTS):
        try:
            url = settings.resolve(name)
        except Exception as exc:
            # Show only the first line - the full guard message is a paragraph.
            print("  %-18s BLOCKED  %s" % (name, str(exc).splitlines()[0]))
            continue
        ok, detail = probe(url + ENDPOINTS[name].health_path)
        print("  %-18s %-8s %s" % (name, "up" if ok else "DOWN", detail))
    return 0


def cmd_venues(args) -> int:
    """Capability matrix - which venues can actually match an order."""
    from .venues import VENUES

    rows = []
    for name, cls in sorted(VENUES.items()):
        try:
            rows.append(cls().capabilities())
        except Exception as exc:
            rows.append({"venue": name, "error": str(exc)})

    print("%-18s %-8s %-9s %-7s %s" % ("VENUE", "TRADE", "MATCHING", "SIGNED", "NOTE"))
    for r in rows:
        if "error" in r:
            print("%-18s %s" % (r["venue"], r["error"]))
            continue
        note = str(r.get("notes", ""))
        print("%-18s %-8s %-9s %-7s %s" % (
            r["venue"],
            "yes" if r["supports_trading"] else "-",
            "MOCK" if r["is_mock"] else ("yes" if r["has_matching_engine"] else "-"),
            "yes" if r["requires_signature"] else "-",
            note[:70] + ("..." if len(note) > 70 else ""),
        ))
    print("\nMOCK = responses are canned; fills are not real executions.")
    return 0


def cmd_quote(args) -> int:
    """Consolidated mid across public venues."""
    from .marketdata.feeds import PUBLIC_FEEDS, consolidated_mid

    for market in args.markets:
        print("== %s ==" % market)
        for name, cls in PUBLIC_FEEDS.items():
            try:
                q = cls().fetch_quote(market)
                print("  %-18s %s" % (name, q.price))
            except Exception as exc:
                print("  %-18s unavailable (%s)" % (name, type(exc).__name__))
        c = consolidated_mid(market)
        print("  %-18s %s" % ("CONSOLIDATED", c.price if c else "n/a"))
    return 0


def cmd_book(args) -> int:
    """Order book with spread and sweep cost."""
    from .core.types import Side
    from .marketdata.feeds import best_effort_book

    book = best_effort_book(args.market, depth=args.depth)
    print("%s  source=%s" % (args.market, book.source))
    if book.source == "synthetic":
        print("WARNING: no live feed reachable - this book is a fixture.")
    print("  bid %s   ask %s   mid %s   spread %s bps"
          % (book.best_bid, book.best_ask, book.mid,
             (book.spread_bps or Decimal(0)).quantize(Decimal("0.01"))))
    print("\n  %-14s %-14s | %-14s %s" % ("BID SIZE", "BID", "ASK", "ASK SIZE"))
    for i in range(min(args.depth, max(len(book.bids), len(book.asks)))):
        b = book.bids[i] if i < len(book.bids) else None
        a = book.asks[i] if i < len(book.asks) else None
        print("  %-14s %-14s | %-14s %s" % (
            b.size if b else "", b.price if b else "",
            a.price if a else "", a.size if a else ""))
    if args.size:
        size = Decimal(args.size)
        for side in (Side.BUY, Side.SELL):
            filled, avg, complete = book.sweep(side, size)
            mid = book.mid or Decimal(0)
            slip = ((avg - mid) / mid * 10000 * side.sign) if mid and avg else Decimal(0)
            print("\n  sweep %s %s -> filled %s @ %s (%s bps%s)"
                  % (side.value, size, filled, avg,
                     slip.quantize(Decimal("0.1")),
                     "" if complete else ", INCOMPLETE"))
    return 0


def cmd_defi(args) -> int:
    """Aggregator vs on-chain oracle, and the basis between them."""
    from .marketdata.defi import defi_snapshot
    _print(defi_snapshot(args.markets, chain=args.chain))
    print("\nbasis_bps = aggregator vs oracle. A wide gap means the oracle has "
          "not\nupdated through a move - the condition under which on-chain "
          "liquidations lag.")
    return 0


def cmd_oracle(args) -> int:
    """Cross-check a venue mark against an independent on-chain oracle.

    Exit status is the point of this one: 0 when the mark stands up, 1 when it
    does not, so it can gate something rather than only inform a human.
    """
    from .marketdata.feeds import best_effort_book
    from .risk.oracle import cross_check

    print(describe_guard())

    cex_price = None
    book = best_effort_book(args.market.replace("-PERP", ""), depth=5)
    if book.source == "synthetic":
        # Comparing the chain to a fabricated mid would manufacture a
        # divergence, or worse, manufacture agreement. Report the mark as
        # unverified instead and let the oracle side speak for itself.
        print("\nno public venue answered - reporting the oracle alone. The "
              "synthetic\nbook is not a second opinion about price.")
    else:
        cex_price = book.mid

    check = cross_check(args.market, cex_price, chain=args.chain,
                        tolerance_bps=Decimal(args.tolerance_bps),
                        max_age_s=args.max_age_s)
    if check is None:
        print("\nno aggregator configured for %s on %s - the mark is "
              "unverified,\nwhich is not the same as verified-and-fine."
              % (args.market, args.chain))
        return 0

    _print(check.to_dict())
    if cex_price is not None:
        print("\nvenue %s (%s) vs chain %s at block %d"
              % (book.mid, book.source, check.oracle_price, check.block))
    reason = check.reason()
    print("\n%s" % (reason if reason else
                    "mark agrees with the chain, and the round is fresh."))

    if args.store:
        from .store import Store
        store = Store(args.store)
        try:
            rid = store.record_chain_observation({
                "observed_ms": check.observed_ms, "chain": check.chain,
                "block": check.block, "market": check.market,
                "source": check.oracle_source, "price": check.oracle_price,
                "cex_price": check.cex_price,
                # Quantized to match what `to_dict` reported. Prices are stored
                # exactly because they are money; a divergence is a derived
                # ratio, and keeping 27 digits of it would mean the stored
                # value and the printed one disagree on sight.
                "divergence_bps": (None if check.divergence_bps is None else
                                   check.divergence_bps.quantize(Decimal("0.01"))),
            })
            store.prune_chain_observations(keep=args.keep)
            print("recorded observation %d in %s" % (rid, store.path))
        finally:
            store.close()

    return 0 if check.ok else 1


def cmd_wallet(args) -> int:
    """Native and ERC-20 balances for an address."""
    from .chain import Wallet
    from .marketdata.defi import DefiLlamaFeed, JsonRpc

    rpc = JsonRpc(args.chain)
    w = Wallet(args.address, rpc, chain=args.chain)

    prices = {}
    if args.price:
        try:
            llama = DefiLlamaFeed()
            got = llama.prices(["coingecko:ethereum", "coingecko:usd-coin",
                                "coingecko:chainlink", "coingecko:uniswap"])
            slug_to_sym = {"coingecko:ethereum": "WETH", "coingecko:usd-coin": "USDC",
                           "coingecko:chainlink": "LINK", "coingecko:uniswap": "UNI"}
            prices = {slug_to_sym[k]: q.price for k, q in got.items() if k in slug_to_sym}
        except Exception:
            pass

    snap = w.snapshot(price_lookup=lambda s: prices.get(s))
    print("wallet  %s  chain=%s  block=%s" % (snap.address, snap.chain, snap.block))
    print("  native  %s ETH" % snap.native)
    for t in snap.tokens:
        print("  token   %s" % t)
    total = snap.total_usd(prices.get("WETH"))
    if total is not None:
        print("  ~total  $%s" % total.quantize(Decimal("0.01")))
    else:
        print("  ~total  unpriced (pass --price, or no price source reachable)")
    return 0


def cmd_vault(args) -> int:
    """ERC-4626 tokenized vault report."""
    from .chain import ERC4626Vault
    from .marketdata.defi import JsonRpc

    rpc = JsonRpc(args.chain)
    for address in args.addresses:
        v = ERC4626Vault(address, rpc, chain=args.chain)
        if not v.is_erc4626():
            print("%s  not an ERC-4626 vault" % address)
            continue
        _print(v.report(holder=args.holder))
    print("\nround_trip_cost_bps = deposit then immediately redeem. This is the "
          "cost\na yield has to earn back before the position is profitable.")
    return 0


def cmd_validate(args) -> int:
    """Run an order through the full validation pipeline."""
    from .core import (
        AccountState, InstrumentRegistry, OrderRequest, OrderType, Quote, Side,
        now_ms,
    )
    from .marketdata.feeds import best_effort_book
    from .validation import for_venue

    reg = InstrumentRegistry()
    validator = for_venue(args.venue, reg)

    book = None
    mark = None
    if not args.offline:
        book = best_effort_book(args.market.replace("-PERP", ""), depth=10)
        from .core.types import OrderBook
        book = OrderBook(args.market, book.bids, book.asks, book.timestamp_ms,
                         book.source)
        if book.mid:
            mark = Quote(args.market, book.mid, now_ms(), book.source)

    request = OrderRequest(
        market=args.market,
        side=Side(args.side.upper()),
        order_type=OrderType(args.type.upper()),
        size=Decimal(args.size),
        price=Decimal(args.price) if args.price else None,
        post_only=args.post_only,
        reduce_only=args.reduce_only,
        nonce=1, signature="0xdemo", signature_timestamp_ms=now_ms(),
    )
    account = AccountState(free_collateral=Decimal(args.collateral),
                           total_collateral=Decimal(args.collateral))

    print(describe_guard())
    if book is not None:
        print("book source=%s mid=%s" % (book.source, book.mid))
    print()

    if args.repair:
        fixed, report, changes = validator.validate_and_repair(
            request, book=book, mark=mark, account=account)
        for c in changes:
            print("  repaired: %s" % c)
        request, = (fixed,)
    else:
        report = validator.check(request, book=book, mark=mark, account=account)

    print(report)
    if not report.ok:
        _print(report.to_dict())
        return 1
    for w in report.warnings:
        print("  warning: %s" % w)
    return 0


def cmd_paper(args) -> int:
    """Run a paper trading session against a live-seeded book."""
    from .core import InstrumentRegistry, OrderRequest, OrderType, Side
    from .execution.oms import OrderManager
    from .risk.limits import RiskEngine, RiskLimits
    from .venues import PaperVenue

    reg = InstrumentRegistry()
    venue = PaperVenue(reg, starting_collateral=Decimal(args.collateral))
    book = venue.seed_from_feed(args.market.replace("-PERP", ""), depth=25)
    from .core.types import OrderBook
    venue.seed(OrderBook(args.market, book.bids, book.asks, book.timestamp_ms,
                         book.source))

    print(describe_guard())
    print("seeded %s from %s: bid=%s ask=%s"
          % (args.market, book.source, book.best_bid, book.best_ask))

    limits = RiskLimits(require_real_market_data=(book.source != "synthetic"))
    oms = OrderManager(venue, RiskEngine(limits))

    mid = book.mid or Decimal("60000")
    inst = reg.require(args.market)
    demo = [
        ("market buy", OrderRequest(args.market, Side.BUY, OrderType.MARKET,
                                    Decimal("0.01"))),
        ("resting bid", OrderRequest(args.market, Side.BUY, OrderType.LIMIT,
                                     Decimal("0.01"),
                                     inst.round_price(mid * Decimal("0.99"), Side.BUY),
                                     post_only=True)),
        ("reduce-only close", OrderRequest(args.market, Side.SELL, OrderType.MARKET,
                                           Decimal("0.01"), reduce_only=True)),
    ]
    for label, req in demo:
        t = oms.submit(req)
        print("\n%-18s -> %s" % (label, t.status.value))
        if t.rejections:
            for r in t.rejections:
                print("    %s" % r)
        elif t.filled_size:
            print("    filled %s @ %s" % (t.filled_size, t.avg_price))

    print("\n-- blotter --")
    _print(oms.blotter())
    print("\n-- stats --")
    _print(oms.stats())
    print("\n-- pnl --")
    _print(venue.pnl_summary())
    return 0


def cmd_fund(args) -> int:
    """Tokenized vault: deposits, a traded position, and PnL as share price."""
    from .chain import StrategyVault
    from .core import InstrumentRegistry, OrderRequest, OrderType, Side
    from .core.types import OrderBook
    from .execution import VaultStrategyRunner
    from .risk.limits import RiskEngine, RiskLimits
    from .venues import PaperVenue

    market = args.market
    reg = InstrumentRegistry()
    venue = PaperVenue(reg, starting_collateral=Decimal("0"))
    seed = venue.seed_from_feed(market.replace("-PERP", ""), depth=25)
    venue.seed(OrderBook(market, seed.bids, seed.asks, seed.timestamp_ms, seed.source))
    mid = seed.mid or Decimal("60000")

    vault = StrategyVault(performance_fee_bps=Decimal(args.performance_fee),
                          management_fee_bps=Decimal("0"))
    run = VaultStrategyRunner(vault, venue, RiskEngine(RiskLimits.permissive()))

    print(describe_guard())
    print("seeded %s from %s at %s\n" % (market, seed.source, mid))

    def row(tag):
        r = vault.report()
        print("  %-24s price=%-22s NAV=%-12s supply=%s"
              % (tag, r["share_price"], r["total_assets"], r["total_supply"]))

    print("investors deposit")
    run.deposit(Decimal("100000"), "alice"); row("alice 100,000")
    run.deposit(Decimal("50000"), "bob");    row("bob 50,000")

    print("\ndeploy and trade")
    allocated = run.allocate_to_venue(); row("allocated %s" % allocated)
    inst = reg.require(market)
    size = inst.round_size(Decimal("100000") / mid)
    t = run.trade(OrderRequest(market, Side.BUY, OrderType.MARKET, size))
    row("bought %s" % t.filled_size)

    move = Decimal(args.move_pct) / Decimal(100)
    print("\nmark %s%% price move" % args.move_pct)
    moved = OrderBook.from_levels(
        market,
        [(lv.price * (1 + move), lv.size) for lv in seed.bids],
        [(lv.price * (1 + move), lv.size) for lv in seed.asks],
        source="scenario")
    venue.seed(moved)
    run.mark(); row("marked")

    print("\ninvestors")
    for r in run.investor_table():
        print("  %-8s shares=%-14s value=%-14s pnl=%-10s roi=%s%%"
              % (r["address"], r["shares"], r["value"], r["pnl"], r["roi_pct"]))
    print("\nperformance fee accrued: %s (high-water mark %s)"
          % (vault.fees_accrued.quantize(Decimal("0.01")), vault.high_water_mark))
    print("\nNote: both investors earn the same percentage regardless of size -")
    print("that is the invariant share-based accounting exists to guarantee.")
    return 0


def cmd_trade(args) -> int:
    """Run the full institutional order path against a live testnet venue.

    Dry-run by default. This is the command that shows what "institutions
    trade by code" actually means: an order is not a button press, it is a
    pipeline - venue metadata, live book, 45 validation rules, operator risk
    limits, idempotency, signed transport, acknowledgement, reconciliation.
    Every stage is printed so the refusals are as visible as the fills.
    """
    from .core import InstrumentRegistry, OrderRequest, OrderType, Side
    from .execution import OrderManager
    from .risk.limits import RiskEngine, RiskLimits
    from .vault import default_vault
    from .venues import VENUES
    from .venues.base import NotAuthenticated, VenueError

    print(describe_guard())
    print()

    # -- 1. venue -----------------------------------------------------------
    from .store import Store
    store = Store(args.store) if args.store else None
    if store is not None:
        print("state      %s (%d order(s) already recorded)"
              % (store.path, store.order_count()))

    reg = InstrumentRegistry()
    vault = default_vault()
    name = args.venue.lower()
    if name not in VENUES:
        print("unknown venue %r; known: %s" % (name, ", ".join(sorted(VENUES))),
              file=sys.stderr)
        return 2

    if name == "binance":
        venue = VENUES[name](registry=reg, vault=vault, store=store)
    elif name == "paper":
        venue = VENUES[name](registry=reg, starting_collateral=Decimal("100000"),
                             store=store)
        # The local engine has no book of its own - seed it from live public
        # depth so a dry run faces realistic slippage rather than a fixture.
        from .core.types import OrderBook
        seeded = venue.seed_from_feed(args.market.replace("-PERP", ""), depth=25)
        venue.seed(OrderBook(args.market, seeded.bids, seeded.asks,
                             seeded.timestamp_ms, seeded.source))
    else:
        venue = VENUES[name](registry=reg, store=store)

    caps = venue.capabilities()
    print("venue      %s" % name)
    print("  trading=%s  matching=%s  signed=%s  mock=%s" % (
        caps["supports_trading"],
        "MOCK" if caps["is_mock"] else caps["has_matching_engine"],
        caps["requires_signature"], caps["is_mock"]))

    if caps["is_mock"]:
        print("\n  refusing: this venue has no matching engine. A fill here is a")
        print("  fixture, not an execution. Use --venue binance or --venue paper.")
        return 1

    # -- 2. instrument specs, from the venue itself -------------------------
    n = reg.refresh(venue)
    print("  instruments refreshed from venue: %d%s" % (
        n, "" if n else "  (using local defaults)"))
    inst = reg.get(args.market)
    if inst is None:
        near = [s for s in reg.symbols() if args.market.split("-")[0] in s][:6]
        print("\nunknown market %r. try: %s" % (args.market, ", ".join(near) or "?"),
              file=sys.stderr)
        return 2
    print("  %s tick=%s step=%s minNotional=%s band=%sbps" % (
        inst.symbol, inst.tick_size, inst.step_size, inst.min_notional,
        inst.price_band_bps))

    # -- 3. live market data (public, no credentials) -----------------------
    book = mark = None
    try:
        book = venue.fetch_book(args.market, depth=10)
        mark = venue.fetch_quote(args.market)
        print("\nmarket     bid=%s ask=%s mark=%s" % (
            book.best_bid, book.best_ask, mark.price))
    except (VenueError, Exception) as exc:
        print("\nmarket     unavailable (%s)" % type(exc).__name__)

    # -- 4. build the order -------------------------------------------------
    side = Side(args.side.upper())
    otype = OrderType(args.type.upper())
    price = Decimal(args.price) if args.price else None
    if otype is OrderType.LIMIT and price is None and mark is not None:
        # Default to a passive price a whole band inside the spread, so a
        # dry run demonstrates a resting order rather than a taker.
        offset = Decimal(1) - (inst.price_band_bps / Decimal(40000)) * side.sign
        price = inst.round_price(mark.price * offset, side)
        print("           (no --price given; using passive %s)" % price)

    req = OrderRequest(
        market=args.market, side=side, order_type=otype,
        size=Decimal(args.size), price=price,
        post_only=args.post_only, reduce_only=args.reduce_only,
    )

    # -- 5. validation ------------------------------------------------------
    from .validation import for_venue as validator_for
    validator = validator_for(name, reg)
    fixed, report, changes = validator.validate_and_repair(
        req, book=book, mark=mark, account=None)
    for c in changes:
        print("\nrepaired   %s" % c)
    req = fixed
    print("\nvalidation %s" % ("ACCEPTED" if report.ok else "REJECTED"))
    if not report.ok:
        for r in report.rejections:
            print("           %s" % r)
        return 1
    for w in report.warnings:
        print("           warning: %s" % w)

    # -- 6. operator risk ---------------------------------------------------
    risk = RiskEngine(RiskLimits(
        max_order_notional=Decimal(args.max_notional),
        require_real_market_data=(book.source != "synthetic" if book else False),
    ), store=store)
    rejections = risk.check(req, mark=mark, venue=name,
                            book_source=book.source if book else None)
    print("risk       %s" % ("cleared" if not rejections else "REFUSED"))
    for r in rejections:
        print("           %s" % r)
    if rejections:
        return 1

    # -- 7. credentials preflight ------------------------------------------
    authenticated = getattr(venue, "authenticated", True)
    print("auth       %s" % ("ready" if authenticated else "NO CREDENTIALS"))

    if not args.execute:
        print("\n--- DRY RUN. Nothing was sent. ---")
        print("order that would be submitted:")
        _print(req.to_dict())
        if not authenticated:
            print("\nto execute for real you need testnet credentials:")
            print("  1. get free keys (no deposit, no mainnet account) from the")
            print("     venue's testnet portal")
            print("  2. python3 -m cryptodesk keys set binance-testnet-key")
            print("  3. python3 -m cryptodesk keys set binance-testnet-secret")
            print("  4. re-run this command with --execute")
        else:
            print("\nre-run with --execute to send it.")
        return 0

    if not authenticated:
        print("\ncannot execute: no credentials. See the dry-run output above.",
              file=sys.stderr)
        return 1

    # -- 8. send, through the OMS ------------------------------------------
    oms = OrderManager(venue, risk, store=store)
    try:
        tracked = oms.submit(req, mark=mark, book=book)
    except NotAuthenticated as exc:
        print("\nauth failed: %s" % exc, file=sys.stderr)
        return 1

    print("\nsubmitted  %s" % tracked.status.value)
    print("  client_order_id %s" % tracked.client_order_id)
    print("  venue order_id  %s" % tracked.order_id)
    if tracked.filled_size:
        print("  filled %s @ %s" % (tracked.filled_size, tracked.avg_price))
    for r in tracked.rejections:
        print("  %s" % r)

    # -- 9. reconcile -------------------------------------------------------
    print("\nreconcile")
    for key, findings in oms.reconcile().items():
        for f in findings:
            print("  %-20s %s" % (key, f))
    print("\nblotter")
    _print(oms.blotter())
    return 0


def cmd_algo(args) -> int:
    """Work a parent order as children, then measure what it cost."""
    from .core import InstrumentRegistry, OrderRequest, OrderType, Side
    from .core.types import OrderBook, now_ms
    from .execution import TWAP, VWAP, AlgoRunner, MarketTrade, analyse, save_run
    from .execution.oms import OrderManager
    from .execution.persistence import format_stats
    from .marketdata.feeds import best_effort_book
    from .marketdata.http import HttpClient
    from .risk.limits import RiskEngine, RiskLimits
    from .venues import PaperVenue

    if args.history:
        return _algo_history(args)

    market = args.market
    size = Decimal(args.size)
    seed = best_effort_book(market.replace("-PERP", ""), depth=25)
    arrival = seed.mid
    t0 = now_ms()

    print(describe_guard())
    print("\n%s   arrival mid %s   parent %s" % (market, arrival, size))

    trades = []
    try:
        raw = HttpClient().get(
            "https://api.exchange.coinbase.com/products/%s/trades"
            % market.replace("-PERP", ""))
        trades = [MarketTrade(Decimal(t["price"]), Decimal(t["size"]),
                              t0 + i * 10) for i, t in enumerate(raw[:300])]
        vol = sum((t.size for t in trades), Decimal(0))
        print("interval benchmark: %d real prints, %s of volume" % (len(trades), vol))
    except Exception:
        print("interval benchmark unavailable (no tape)")

    def fresh():
        reg = InstrumentRegistry()
        v = PaperVenue(reg, starting_collateral=Decimal("50000000"))
        v.seed(OrderBook(market, list(seed.bids), list(seed.asks),
                         seed.timestamp_ms, seed.source))
        return reg, v, OrderManager(v, RiskEngine(RiskLimits.permissive()))

    def show(label, fills, extra=""):
        r = analyse(market, Side.BUY, fills, arrival_price=arrival,
                    trades=trades or None, start_ms=t0, end_ms=t0 + 300000)
        print("\n%s" % label)
        print("   avg %s   shortfall %s   vs VWAP %s   fees %s   all-in %s"
              % (r.avg_price.quantize(Decimal("0.01")),
                 _bps(r.implementation_shortfall_bps),
                 _bps(r.vwap_slippage_bps), _bps(r.fee_bps),
                 _bps(r.total_cost_bps)))
        if r.participation_rate is not None:
            pr = r.participation_rate * 100
            warn = "  <- benchmark is mostly our own prints" if pr > 15 else ""
            print("   participation %.1f%%%s" % (pr, warn))
        if extra:
            print("   %s" % extra)
        return r

    store = None
    if args.store:
        from .store import Store
        store = Store(args.store)
        print("persisting runs to %s" % store.path)

    reg, v, oms = fresh()
    one = oms.submit(OrderRequest(market, Side.BUY, OrderType.MARKET, size))
    base = show("single MARKET sweep", one.fills)

    for algo in (TWAP(), VWAP()):
        reg, v, oms = fresh()
        runner = AlgoRunner(oms, algo)
        parent = runner.create(
            OrderRequest(market, Side.BUY, OrderType.MARKET, size),
            reg.require(market), arrival, slices=args.slices,
            duration_ms=300000, arrival_price=arrival, start_ms=t0)
        runner.run(parent)
        fills = [f for c in parent.children for f in c.fills]
        sizes = ", ".join(str(sl.size) for sl in parent.slices)
        r = show("%s across %d children" % (algo.name, parent.sent_slices), fills,
                 "slices: %s" % sizes)
        if (base.implementation_shortfall_bps is not None
                and r.implementation_shortfall_bps is not None):
            print("   saved %.2f bps vs the sweep"
                  % (base.implementation_shortfall_bps
                     - r.implementation_shortfall_bps))
        if store is not None:
            pid = save_run(store, parent, r, venue="paper",
                           benchmark_prints=len(trades), keep=args.keep)
            print("   stored as %s" % pid)

    if store is not None:
        print()
        for line in format_stats(store.algo_stats()):
            print(line)
        print("\nOne run is an anecdote. The line above is the reason these are")
        print("kept: a median over many runs is a claim that survives scrutiny,")
        print("and `--history` reads it back without re-running anything.")
        store.close()

    print("\nShortfall is measured against arrival - the price when the decision")
    print("was taken. VWAP-relative is reported beside it because the two can")
    print("disagree, and a VWAP-only report flatters a strategy that just waited.")
    return 0


def _algo_history(args) -> int:
    """Read stored runs back. No venue, no network, no new orders."""
    from .execution.persistence import format_parent_row, format_stats
    from .store import Store

    if not args.store:
        print("--history reads stored runs, so it needs --store PATH")
        return 2

    store = Store(args.store, durable=False)
    try:
        rows = store.list_algo_parents(limit=args.limit, market=args.market
                                       if args.market_filter else None)
        if not rows:
            print("no stored runs in %s - run `algo --store %s` first"
                  % (store.path, args.store))
            return 0
        print("%-28s %-6s %-14s %-4s %12s %10s %8s %8s"
              % ("PARENT", "ALGO", "MARKET", "SIDE", "FILLED", "STATUS",
                 "SHORTFL", "ALL-IN"))
        for row in rows:
            print(format_parent_row(row))
        print()
        for line in format_stats(store.algo_stats()):
            print(line)
        print("\nShortfall and all-in are in bps, signed so positive is a cost.")
        print("A run with n/a had no arrival benchmark or no fills to measure.")
        return 0
    finally:
        store.close()


def _bps(v) -> str:
    return "%+.2f bps" % v if v is not None else "n/a"


def cmd_funding(args) -> int:
    """Live funding rates, annualised, and what carrying costs."""
    from .core import InstrumentRegistry
    from .venues import VENUES

    print(describe_guard())
    print("\nfunding is the perp-only cost of carry. A positive rate means the")
    print("perp trades above index, so LONGS PAY SHORTS.\n")
    print("%-14s %-22s %-8s %-12s %s"
          % ("VENUE", "RATE / PERIOD", "PERIOD", "APR", "COST PER $10k / DAY"))

    for name in ("paradex", "hyperliquid"):
        try:
            reg = InstrumentRegistry()
            venue = VENUES[name](registry=reg)
            reg.refresh(venue)
            fr = venue.fetch_funding_rate(args.market)
        except Exception as exc:
            print("%-14s unavailable (%s)" % (name, type(exc).__name__))
            continue
        if fr is None:
            print("%-14s no rate published for %s" % (name, args.market))
            continue
        per_day = fr.rate * (Decimal(24) / Decimal(fr.period_hours)) * Decimal(10000)
        print("%-14s %-22s %-8s %-12s %s"
              % (name, fr.rate, "%dh" % fr.period_hours,
                 "%.2f%%" % (fr.apr * 100),
                 "$%s" % (-per_day).quantize(Decimal("0.01"))))

    print("\nCost is for a LONG; a short earns the same amount. The period is the")
    print("part that bites - the same numeric rate is 8x the annual cost when it")
    print("settles hourly rather than every eight hours.")
    return 0


def cmd_desk(args) -> int:
    """Serve the local order-ticket UI.

    The same OrderManager, rules and risk limits as `trade` - rendered twice,
    once as a retail app would show it and once as a desk would.
    """
    from .store import Store
    from .web import serve

    store = Store(args.store) if args.store else None
    serve(host=args.host, port=args.port, store=store)
    return 0


def cmd_state(args) -> int:
    """Inspect a durable state file: blotter, replay, kill switch, vault."""
    from .store import Store

    store = Store(args.path, durable=False)
    print(describe_guard())
    print()
    _print(store.stats())

    orders = store.load_orders()
    if orders:
        print("\nblotter")
        for coid, rec in orders.items():
            print("  %-34s %-16s %-12s %s" % (
                coid, rec.get("status"), rec.get("venue"),
                rec.get("order_id") or "(no venue id)"))
        pending = [c for c, r in orders.items()
                   if r.get("status") == "PENDING_NEW" and not r.get("order_id")]
        if pending:
            print("\n  %d order(s) written ahead of a send that never "
                  "acknowledged." % len(pending))
            print("  Query the venue by client_order_id before resending.")

    runs = store.list_algo_parents(limit=10)
    if runs:
        from .execution.persistence import format_parent_row, format_stats
        print("\nalgo runs (most recent 10)")
        for row in runs:
            print(format_parent_row(row))
        for line in format_stats(store.algo_stats()):
            print("  " + line)

    observations = store.list_chain_observations(limit=5)
    if observations:
        print("\non-chain observations (most recent 5)")
        for obs in observations:
            print("  %-10s block %-10s %-10s %-14s venue %-12s div %s"
                  % (obs["chain"], obs["block"], obs["market"], obs["price"],
                     obs["cex_price"] or "unverified",
                     obs["divergence_bps"] or "n/a"))

    ks = store.get("kill_switch") or {}
    if ks.get("tripped"):
        print("\nKILL SWITCH ACTIVE [%s]: %s"
              % (ks.get("code"), ks.get("reason")))
    pnl = store.get("risk_pnl")
    if pnl:
        print("\nrealised pnl %s on %s" % (pnl.get("realised"), pnl.get("day")))
    return 0


def cmd_keys(args) -> int:
    """Inspect and manage vault-held keys."""
    from .vault import MacKeychainVault, default_vault

    if args.action == "list":
        vault = default_vault()
        keys = vault.list_keys()
        if not keys:
            print("no keys stored. add one with:\n"
                  "  python3 -m cryptodesk keys set <name>")
            return 0
        for name in keys:
            try:
                print("  %s" % vault.ref(name))
            except Exception as exc:
                print("  %-24s <unreadable: %s>" % (name, exc))
        return 0

    if args.action == "set":
        if not args.name:
            print("usage: keys set <name>", file=sys.stderr)
            return 2
        if not MacKeychainVault.available():
            print("macOS keychain unavailable; set the env var instead:\n"
                  "  export CRYPTODESK_%s=<value>"
                  % args.name.upper().replace("-", "_"), file=sys.stderr)
            return 1
        import getpass
        secret = getpass.getpass("value for %r (not echoed): " % args.name)
        if not secret:
            print("empty value, aborted", file=sys.stderr)
            return 1
        MacKeychainVault().put(args.name, secret)
        print("stored %r in the macOS keychain." % args.name)
        print("note: `security add-generic-password` passes the value as an "
              "argv element,\nso it is briefly visible to same-user processes. "
              "For keys that hold real\nvalue, add them via Keychain Access.app "
              "instead and this will read them.")
        return 0

    if args.action == "check":
        vault = default_vault()
        for name in (args.name and [args.name]) or vault.list_keys():
            found = vault.which(name) if hasattr(vault, "which") else None
            print("  %-24s %s" % (name, found or "NOT FOUND"))
        return 0

    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cryptodesk",
        description="Testnet-only crypto trading, DeFi and vault toolkit.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="reachability and capability report").set_defaults(
        func=cmd_doctor)
    sub.add_parser("venues", help="venue capability matrix").set_defaults(
        func=cmd_venues)

    q = sub.add_parser("quote", help="consolidated mid across public venues")
    q.add_argument("markets", nargs="*", default=["BTC-USD", "ETH-USD"])
    q.set_defaults(func=cmd_quote)

    b = sub.add_parser("book", help="order book and sweep cost")
    b.add_argument("market", nargs="?", default="BTC-USD")
    b.add_argument("--depth", type=int, default=10)
    b.add_argument("--size", help="estimate sweep cost for this size")
    b.set_defaults(func=cmd_book)

    d = sub.add_parser("defi", help="aggregator vs on-chain oracle prices")
    d.add_argument("markets", nargs="*", default=["BTC-USD", "ETH-USD"])
    d.add_argument("--chain", default="sepolia")
    d.set_defaults(func=cmd_defi)

    o = sub.add_parser("oracle", help="cross-check a venue mark against "
                                      "Chainlink; exit 1 if it fails")
    o.add_argument("--market", default="BTC-USD")
    o.add_argument("--chain", default="sepolia")
    o.add_argument("--tolerance-bps", default="100",
                   help="divergence that counts as a breach (default 100 = 1%%)")
    o.add_argument("--max-age-s", type=int, default=3600,
                   help="oldest acceptable oracle round (default 3600)")
    o.add_argument("--store", help="append the observation to a state file")
    o.add_argument("--keep", type=int, default=2000,
                   help="observations to retain when storing (default 2000)")
    o.set_defaults(func=cmd_oracle)

    w = sub.add_parser("wallet", help="native and ERC-20 balances")
    w.add_argument("address")
    w.add_argument("--chain", default="sepolia")
    w.add_argument("--price", action="store_true", help="value holdings in USD")
    w.set_defaults(func=cmd_wallet)

    v = sub.add_parser("vault", help="ERC-4626 tokenized vault report")
    v.add_argument("addresses", nargs="+")
    v.add_argument("--chain", default="ethereum")
    v.add_argument("--holder", help="also report this address's position")
    v.set_defaults(func=cmd_vault)

    val = sub.add_parser("validate", help="run an order through validation")
    val.add_argument("--venue", default="paradex")
    val.add_argument("--market", default="BTC-USD-PERP")
    val.add_argument("--side", default="BUY")
    val.add_argument("--type", default="LIMIT")
    val.add_argument("--size", default="0.01")
    val.add_argument("--price")
    val.add_argument("--collateral", default="100000")
    val.add_argument("--post-only", action="store_true")
    val.add_argument("--reduce-only", action="store_true")
    val.add_argument("--repair", action="store_true", help="apply mechanical fixes")
    val.add_argument("--offline", action="store_true", help="structural checks only")
    val.set_defaults(func=cmd_validate)

    pa = sub.add_parser("paper", help="paper trading session on a live-seeded book")
    pa.add_argument("--market", default="BTC-USD-PERP")
    pa.add_argument("--collateral", default="100000")
    pa.set_defaults(func=cmd_paper)

    f = sub.add_parser("fund", help="tokenized vault: deposits, trading, share price")
    f.add_argument("--market", default="BTC-USD-PERP")
    f.add_argument("--move-pct", default="5", help="price move to mark against")
    f.add_argument("--performance-fee", default="2000", help="bps of new profit")
    f.set_defaults(func=cmd_fund)

    t = sub.add_parser("trade", help="full order path against a testnet venue")
    t.add_argument("--venue", default="paper",
                   help="paper | binance | paradex | hyperliquid")
    t.add_argument("--market", default="BTC-USD-PERP")
    t.add_argument("--side", default="BUY")
    t.add_argument("--type", default="LIMIT")
    t.add_argument("--size", default="0.001")
    t.add_argument("--price", help="omit for a passive price inside the band")
    t.add_argument("--post-only", action="store_true")
    t.add_argument("--reduce-only", action="store_true")
    t.add_argument("--max-notional", default="10000",
                   help="operator risk cap for this order")
    t.add_argument("--execute", action="store_true",
                   help="actually send it (default is a dry run)")
    t.add_argument("--store", help="path to a durable state file (SQLite)")
    t.set_defaults(func=cmd_trade)

    al = sub.add_parser("algo", help="TWAP/VWAP parent-child execution with TCA")
    al.add_argument("--market", default="BTC-USD-PERP")
    al.add_argument("--size", default="3.0")
    al.add_argument("--slices", type=int, default=6)
    al.add_argument("--store", help="persist each run (parent, children, fills, "
                                    "TCA) to a SQLite state file")
    al.add_argument("--keep", type=int, default=500,
                    help="runs to retain when storing (default 500)")
    al.add_argument("--history", action="store_true",
                    help="print stored runs and their aggregates, then exit")
    al.add_argument("--market-filter", action="store_true",
                    help="with --history, restrict to --market")
    al.add_argument("--limit", type=int, default=20,
                    help="with --history, rows to show (default 20)")
    al.set_defaults(func=cmd_algo)

    fr = sub.add_parser("funding", help="live perp funding rates, annualised")
    fr.add_argument("--market", default="BTC-USD-PERP")
    fr.set_defaults(func=cmd_funding)

    dk = sub.add_parser("desk", help="local order-ticket UI (loopback only)")
    dk.add_argument("--host", default="127.0.0.1")
    dk.add_argument("--port", type=int, default=8787)
    dk.add_argument("--store", help="path to a durable state file (SQLite)")
    dk.set_defaults(func=cmd_desk)

    st = sub.add_parser("state", help="inspect a durable state file")
    st.add_argument("path")
    st.set_defaults(func=cmd_state)

    k = sub.add_parser("keys", help="manage vault-held keys")
    k.add_argument("action", choices=["list", "set", "check"])
    k.add_argument("name", nargs="?")
    k.set_defaults(func=cmd_keys)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from .vault.base import redact
        print("error: %s" % redact(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
