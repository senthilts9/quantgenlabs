"""GenAI execution layer — explain and configure routing in natural language.

Two uses a trading desk actually wants:
  * explain(): turn a RoutingResult into a concise, auditable best-execution
    rationale a trader or compliance reviewer can read — grounded strictly in
    the numbers the router produced (venues, bps, fill probabilities), never
    invented.
  * policy_from_text(): parse a natural-language execution instruction
    ("work it passively, cap DEX exposure, done in 5 minutes") into structured
    routing parameters.

Model is env-configurable; without an API key both degrade to a deterministic
template so the console/demo still works offline.
"""
from __future__ import annotations

import json
import os

from .router import RoutingResult

SYSTEM = (
    "You are the execution-analytics copilot for a cross-asset Smart Order "
    "Router. Explain routing decisions concisely and quantitatively, grounded "
    "ONLY in the ROUTING JSON provided. Use best-execution language "
    "(implementation shortfall, venue toxicity, fill probability, price "
    "impact). Never invent venues or numbers.\n\nROUTING:\n{ctx}\n"
)


def _client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=key)
    except Exception:
        return None


def _template(res: RoutingResult, fill_prob: dict | None) -> str:
    s = res.summary()
    top = max(s["allocation"], key=s["allocation"].get) if s["allocation"] else "—"
    lines = [
        f"Routed {s['filled_qty']:.0f}/{s['target_qty']:.0f} {s['side'].upper()} "
        f"at VWAP {s['vwap']:.4f} — {s['slippage_bps']:.2f} bps implementation "
        f"shortfall vs arrival mid ({s['fees_bps']:.2f} bps of that is fees).",
        f"Marginal-cost routing improved execution by "
        f"{s['improvement_vs_best_single_bps']:.2f} bps versus sweeping the single "
        f"best venue, and far outperformed naive pro-rata "
        f"({s['benchmarks_bps']['pro_rata']:.1f} bps).",
        f"Largest allocation went to {top}. Splits: "
        + ", ".join(f"{k} {v:.0f}" for k, v in s["allocation"].items()) + ".",
    ]
    if fill_prob:
        lines.append("Size was discounted by ML fill probability: "
                     + ", ".join(f"{k} {p*100:.0f}%" for k, p in fill_prob.items())
                     + " — liquidity that is cheap but unlikely to fill was down-weighted.")
    return " ".join(lines)


def explain(res: RoutingResult, fill_prob: dict | None = None) -> dict:
    ctx = {"summary": res.summary(), "fill_prob": fill_prob or {}}
    client = _client()
    if client is None:
        return {"explanation": _template(res, fill_prob), "model": "offline"}
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    msg = client.messages.create(
        model=model, max_tokens=400,
        system=SYSTEM.format(ctx=json.dumps(ctx, indent=2)),
        messages=[{"role": "user", "content": "Explain this route for the desk."}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return {"explanation": text, "model": model}


def policy_from_text(instruction: str) -> dict:
    """Best-effort NL → routing params. Offline: keyword heuristic; with a key,
    Claude returns structured JSON. Kept deterministic-safe either way."""
    client = _client()
    if client is None:
        t = instruction.lower()
        return {
            "urgency": "high" if any(w in t for w in ("urgent", "now", "fast")) else "low",
            "cap_amm": "avoid" in t or "cap dex" in t or "no dex" in t,
            "style": "passive" if "passiv" in t else "aggressive" if "aggress" in t else "balanced",
            "source": "heuristic",
        }
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    msg = client.messages.create(
        model=model, max_tokens=300,
        system="Convert the execution instruction into JSON with keys "
               "urgency(low|med|high), cap_amm(bool), style(passive|balanced|aggressive), "
               "max_venues(int). Reply with JSON only.",
        messages=[{"role": "user", "content": instruction}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        return {**json.loads(text), "source": model}
    except Exception:
        return {"style": "balanced", "source": model, "raw": text}
