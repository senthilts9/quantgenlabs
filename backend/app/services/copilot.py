"""Claude copilot + chat — grounded in live desk state.

Two endpoints:
  POST /api/copilot          one-shot answer (JSON)
  POST /api/copilot/stream   token stream (SSE) for the chat UI

The desk snapshot from desk_context is injected into the system prompt, so
answers reason about the actual book. Model is env-configurable
(ANTHROPIC_MODEL) and defaults to a fast, cheap model for interactive chat.
If no API key is present the endpoint degrades gracefully to a deterministic
local summary of the risk numbers rather than erroring — the console still
works in a demo/offline setting.
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .desk_context import DeskContext, Position

router = APIRouter(prefix="/api/copilot", tags=["copilot"])

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

# USD per million tokens, (input, output). Verified 2026-09-11.
#
# Sonnet 5 was (2.00, 10.00) here — that was the introductory rate, which ran
# out on 2026-08-31. Standard pricing is 3/15, so every Sonnet call logged
# after that date was booked at two-thirds of what it actually cost. The
# GenAI Monitor's cost VaR is only as honest as this table, so it carries a
# verification date: a stale price is a silent, systematic under-report.
PRICING_PER_MTOK = {
    "claude-opus-5":    (5.00, 25.00),
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Unknown models fall back to Haiku's rate, which UNDER-states cost for
# anything bigger. Flagged on the row so a mispriced call is visible as a
# mispriced call rather than quietly averaged into the totals.
_FALLBACK_PER_MTOK = (1.00, 5.00)


def _cost(model: str, tin: int, tout: int) -> float:
    pin, pout = PRICING_PER_MTOK.get(model, _FALLBACK_PER_MTOK)
    return tin / 1e6 * pin + tout / 1e6 * pout


def priced(model: str) -> bool:
    """Whether this model's cost figures come from a known rate."""
    return model in PRICING_PER_MTOK


def _record(endpoint: str, model: str, t0: float, resp=None,
            error: str | None = None, prompt_chars: int = 0) -> None:
    """Telemetry for the GenAI Monitor screen. Never raises: monitoring must
    not be able to break the thing it is monitoring."""
    import time as _t
    try:
        from app.api.oms import oms
        tin = tout = 0
        reply = 0
        if resp is not None and getattr(resp, "usage", None):
            tin = int(resp.usage.input_tokens or 0)
            tout = int(resp.usage.output_tokens or 0)
        if resp is not None:
            try:
                reply = sum(len(b.text) for b in resp.content if b.type == "text")
            except Exception:
                reply = 0
        oms().store.record_llm(
            model=model, endpoint=endpoint,
            latency_ms=(_t.perf_counter() - t0) * 1000.0,
            input_tokens=tin, output_tokens=tout,
            cost_usd=_cost(model, tin, tout),
            ok=error is None, error=error,
            prompt_chars=prompt_chars, reply_chars=reply)
    except Exception:
        pass
MAX_TOKENS = int(os.getenv("COPILOT_MAX_TOKENS", "1024"))

SYSTEM = (
    "You are the risk copilot for a cross-asset trading desk: equities and "
    "index products, rates/fixed income, FX, and a digital-assets book on "
    "XRPL and perpetual-futures venues. Answer concisely and quantitatively, "
    "grounded in the DESK STATE JSON below. Prefer the desk's own numbers "
    "over textbook generalities. Flag limit utilisation, execution cost and "
    "liquidation risk proactively. If asked something the data cannot "
    "support, say so plainly rather than estimating. Never invent positions "
    "or fills -- if the blotter is empty, say it is empty.\n\n"
    "DESK STATE:\n{desk}\n"
)


class ChatMessage(BaseModel):
    role: str          # "user" | "assistant"
    content: str


class CopilotRequest(BaseModel):
    messages: list[ChatMessage]
    positions: list[dict] = []
    funding_rates: dict[str, float] = {}
    returns_window: dict[str, list[float]] = {}
    confidence: float = 0.99
    # Pull the live OMS book, risk-limit utilisation and TCA into context.
    # Without this the copilot only ever sees what the CLIENT chose to send,
    # which makes "grounded in live desk state" a claim the code doesn't keep.
    include_desk_state: bool = True


def _live_desk_state() -> dict:
    """Real platform state: positions, limits, execution quality.

    Imported lazily inside the function -- app.api.oms imports services, so a
    module-level import here would be circular. Each section is independently
    guarded so a single failing subsystem degrades that block rather than
    blanking the copilot's whole context.
    """
    state: dict = {}
    try:
        from app.api.oms import _marks, current_limits, oms
        from app.core.oms import utilisation

        engine = oms()
        marks = _marks()
        state["blotter"] = engine.blotter(marks)
        limits = current_limits(engine)
        state["risk_limits"] = limits.to_dict()
        state["limit_utilisation"] = utilisation(
            limits, engine.store.list_positions(), marks,
            engine.store.realized_pnl_today())
        state["recent_breaches"] = engine.store.list_breaches(5)
    except Exception as e:
        state["blotter_error"] = f"{type(e).__name__}: {e}"[:120]

    try:
        from app.core import tca
        from app.api.oms import oms as _oms
        eng = _oms()
        orders = [{k: (v.value if hasattr(v, "value") else v)
                   for k, v in o.__dict__.items()} for o in eng.store.list_orders(200)]
        fills = [{k: (v.value if hasattr(v, "value") else v)
                  for k, v in f.__dict__.items()} for f in eng.store.list_fills(200)]
        t = tca.analyse(orders, fills)
        state["execution_quality"] = {
            "orders_analysed": t["orders_analysed"],
            "notional_weighted_cost_bps": t["notional_weighted_cost_bps"],
            "cost_bps": t["cost_bps"], "by_algo": t["by_algo"],
        }
    except Exception as e:
        state["tca_error"] = f"{type(e).__name__}: {e}"[:120]

    try:
        from app.core.marketdata import fred_latest, treasury_curve
        state["market"] = {
            "indices": fred_latest(["SP500", "VIXCLS", "T10Y2Y"]),
            "curve_as_of": treasury_curve().get("as_of"),
        }
    except Exception as e:
        state["market_error"] = f"{type(e).__name__}: {e}"[:120]
    return state


def _build_context(req: CopilotRequest) -> DeskContext:
    return DeskContext(
        positions=[Position(**p) for p in req.positions],
        funding_rates=req.funding_rates,
        returns_window=req.returns_window,
    )


def _prompt_block(req: CopilotRequest, ctx: DeskContext) -> str:
    """Crypto desk context plus live platform state, as one JSON block."""
    payload = {"crypto_desk": ctx.snapshot()}
    if req.include_desk_state:
        payload["platform"] = _live_desk_state()
    return json.dumps(payload, indent=2, default=str)


def _offline_answer(ctx: DeskContext, question: str) -> str:
    snap = ctx.snapshot()
    lines = [
        "⚠ Running without an Anthropic API key — deterministic local summary:",
        f"• {snap['position_count']} positions, gross notional "
        f"${snap['gross_notional']:,.0f}.",
        f"• Closest position to liquidation: "
        f"{snap['closest_liq_distance_pct']:.1f}% away.",
    ]
    for p in snap["positions"]:
        seg = (f"• {p['symbol']} {p['side']} — liq {p['liq_price']}, "
               f"{p['distance_to_liq_pct']:+.1f}% to liq, carry "
               f"{p['annualised_carry_pct']:+.1f}%/yr")
        if "var99_pct" in p:
            seg += f", VaR99 {p['var99_pct']}% / ES {p['es99_pct']}%"
        lines.append(seg)
    lines.append("Set ANTHROPIC_API_KEY to enable full natural-language reasoning.")
    return "\n".join(lines)


def _anthropic_client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=key)
    except Exception:
        return None


@router.post("")
def copilot(req: CopilotRequest) -> dict:
    ctx = _build_context(req)
    client = _anthropic_client()
    question = req.messages[-1].content if req.messages else ""
    if client is None:
        return {"role": "assistant", "content": _offline_answer(ctx, question),
                "grounded": True, "model": "offline"}
    import time as _time
    t0 = _time.perf_counter()
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=SYSTEM.format(desk=_prompt_block(req, ctx)),
            messages=[m.model_dump() for m in req.messages],
        )
    except Exception as e:
        # Record the failure before re-raising: an error rate that is only
        # visible in logs is not something a monitoring screen can act on.
        _record("/api/copilot", MODEL, t0, error=f"{type(e).__name__}: {e}"[:200],
                prompt_chars=len(question))
        raise
    _record("/api/copilot", MODEL, t0, resp=resp, prompt_chars=len(question))
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return {"role": "assistant", "content": text, "grounded": True, "model": MODEL}


@router.post("/stream")
def copilot_stream(req: CopilotRequest) -> StreamingResponse:
    ctx = _build_context(req)
    client = _anthropic_client()

    def gen():
        if client is None:
            yield f"data: {json.dumps({'text': _offline_answer(ctx, '')})}\n\n"
            yield "data: [DONE]\n\n"
            return
        with client.messages.stream(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=SYSTEM.format(desk=_prompt_block(req, ctx)),
            messages=[m.model_dump() for m in req.messages],
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
