"""Rates & Fixed Income API — real curve, real bond analytics, factor model."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core import rates
from ..core.marketdata import treasury_curve, treasury_history
from ..core.rates import pca as pca_mod

router = APIRouter(prefix="/api/rates", tags=["rates"])


class BondReq(BaseModel):
    coupon_pct: float = Field(4.0, ge=0, le=25)
    maturity: float = Field(10.0, gt=0, le=30)
    face: float = Field(100.0, gt=0)
    confidence: float = Field(0.99, gt=0.5, lt=1)
    horizon_days: int = Field(1, ge=1, le=250)


@router.post("/analyze")
def analyze(req: BondReq) -> dict:
    """Full desk view of one bond off the live Treasury curve."""
    try:
        c = treasury_curve()
        pts = c.get("points") or []
        if len(pts) < 4:
            raise HTTPException(502, "treasury curve unavailable")
        years = [p["years"] for p in pts]
        par = [p["yield_pct"] for p in pts]

        zc = rates.bootstrap(years, par)
        bond = rates.Bond(face=req.face, coupon_pct=req.coupon_pct,
                          maturity=req.maturity)
        an = rates.bond_analytics(bond, zc)
        krd = rates.key_rate_durations(bond, years, par)
        scen = rates.scenario_pnl(bond, years, par)
        ns = rates.fit_nelson_siegel(years, par)

        hist = treasury_history(250)
        model = pca_mod.fit(hist["matrix"], hist["years"], 3)
        krd_map = {r["tenor_years"]: r["krd"] for r in krd["key_rate_durations"]}
        var = pca_mod.factor_var(model, krd_map, an["price"],
                                 req.confidence, req.horizon_days)

        return {
            "as_of": c.get("as_of"),
            "curve": {"tenors": [p["tenor"] for p in pts], "years": years,
                      "par_pct": par,
                      "zero_pct": [round(float(z) * 100, 4)
                                   for z in zc.zero_rate(years)]},
            "bond": {"coupon_pct": req.coupon_pct, "maturity": req.maturity,
                     "face": req.face},
            "analytics": an,
            "key_rate": krd,
            "scenarios": scen,
            "nelson_siegel": ns,
            "factor_model": pca_mod.public(model),
            "factor_shocks": pca_mod.factor_shocks(model, 1.0),
            "factor_var": var,
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"rates analysis failed: {type(e).__name__}: {e}")


@router.get("/factors")
def factors(days: int = 250, n: int = 3) -> dict:
    try:
        hist = treasury_history(days)
        model = pca_mod.fit(hist["matrix"], hist["years"], n)
        return {"dates": hist["dates"], "tenors": hist["tenors"],
                **pca_mod.public(model),
                "shocks": pca_mod.factor_shocks(model, 1.0)}
    except Exception as e:
        raise HTTPException(502, f"factor model failed: {e}")
