"""
SignalLead PL — FastAPI Web Application & REST API.

Endpoints:
  GET  /                  — SignalLead PL Dark Dashboard (dashboard.html)
  GET  /health            — Service healthcheck
  GET  /api/meta          — Supported Polish cities, industries & buyer personas
  GET  /api/leads         — Filtered leads + KPI summary (?min_score=0&teaser=false)
  POST /api/audit         — Live technical/SEO/Ads audit of a single Polish B2B domain
  POST /api/harvest       — Batch harvest & audit by city and industry
  GET  /api/export/csv    — Download leads pack as Excel-ready CSV (?teaser=true|false)
  GET  /api/export/json   — Download leads pack as JSON (?teaser=true|false)
  DELETE /api/leads/{dom} — Remove a single lead by domain
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from engine import (
    CITIES,
    INDUSTRIES,
    LeadDatabase,
    SignalAuditorAgent,
    mask_lead_for_teaser,
    run_harvest_and_audit,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SIGNALLEAD_DB_PATH", str(BASE_DIR / "signallead.db")))
DASHBOARD_FILE = BASE_DIR / "dashboard.html"

BUYER_PERSONAS = [
    "Software House / Agencja Web Dev",
    "Agencja Performance & SEO 360",
    "Agencja SEO / SEM",
    "Agencja Digital Marketing",
]

app = FastAPI(
    title="SignalLead PL",
    description="Autonomiczny silnik leadów sygnałowych B2B w Polsce (Teaser-First)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = LeadDatabase(DB_PATH)
auditor = SignalAuditorAgent()


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class AuditRequest(BaseModel):
    url: str = Field(..., min_length=3, examples=["https://www.dzp.pl"])
    city: str = Field(default="Warszawa")
    industry: str = Field(default="Kancelarie Prawne/Podatkowe")
    company_name: Optional[str] = Field(default=None)
    teaser: bool = Field(default=False)


class HarvestRequest(BaseModel):
    city: str = Field(default="Warszawa")
    industry: str = Field(default="Kancelarie Prawne/Podatkowe")
    limit: int = Field(default=5, ge=1, le=20)
    teaser: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    if not DASHBOARD_FILE.exists():
        raise HTTPException(status_code=500, detail="Brak pliku dashboard.html.")
    return HTMLResponse(content=DASHBOARD_FILE.read_text(encoding="utf-8"))


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "SignalLead PL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/meta")
async def get_meta() -> dict[str, list[str]]:
    return {
        "cities": list(CITIES.keys()),
        "industries": INDUSTRIES,
        "buyer_personas": BUYER_PERSONAS,
    }


@app.get("/api/leads")
async def get_leads(
    min_score: int = Query(default=0, ge=0, le=99),
    teaser: bool = Query(default=False),
    city: Optional[str] = Query(default=None),
    industry: Optional[str] = Query(default=None),
    buyer_persona: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
) -> dict:
    leads = db.list_leads(
        min_score=min_score,
        city=city,
        industry=industry,
        buyer_persona=buyer_persona,
        search=search,
        teaser=teaser,
    )
    kpis = db.compute_kpis(leads)
    return {
        "mode": "teaser" if teaser else "full",
        "kpis": kpis,
        "leads": leads,
    }


@app.post("/api/audit")
async def audit_single_domain(payload: AuditRequest) -> dict:
    try:
        record = await auditor.audit(
            raw_url=payload.url,
            city=payload.city,
            industry=payload.industry,
            hint_company_name=payload.company_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Błąd podczas audytu domeny: {exc}"
        ) from exc

    saved = db.upsert_lead(record)
    result = mask_lead_for_teaser(saved) if payload.teaser else saved
    return {"status": "ok", "lead": result}


@app.post("/api/harvest")
async def harvest_city_industry(payload: HarvestRequest) -> dict:
    if payload.city not in CITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Nieobsługiwane miasto. Dostępne: {', '.join(CITIES.keys())}",
        )
    if payload.industry not in INDUSTRIES:
        raise HTTPException(
            status_code=400,
            detail=f"Nieobsługiwana branża. Dostępne: {', '.join(INDUSTRIES)}",
        )

    audited = await run_harvest_and_audit(
        city=payload.city,
        industry=payload.industry,
        limit=payload.limit,
        db=db,
    )
    output_leads = (
        [mask_lead_for_teaser(lead) for lead in audited] if payload.teaser else audited
    )
    return {
        "status": "ok",
        "city": payload.city,
        "industry": payload.industry,
        "audited_count": len(output_leads),
        "leads": output_leads,
    }


@app.get("/api/export/csv")
async def export_leads_csv(
    teaser: bool = Query(default=False),
    min_score: int = Query(default=0, ge=0, le=99),
    city: Optional[str] = Query(default=None),
    industry: Optional[str] = Query(default=None),
) -> Response:
    leads = db.list_leads(
        min_score=min_score,
        city=city,
        industry=industry,
        teaser=teaser,
    )
    csv_body = db.export_csv(leads, teaser=teaser)
    mode_tag = "DEMO_TEASER" if teaser else "PELNA_PACZKA"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"SignalLead_PL_{mode_tag}_{stamp}.csv"
    return Response(
        content=csv_body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/json")
async def export_leads_json(
    teaser: bool = Query(default=False),
    min_score: int = Query(default=0, ge=0, le=99),
    city: Optional[str] = Query(default=None),
    industry: Optional[str] = Query(default=None),
) -> Response:
    leads = db.list_leads(
        min_score=min_score,
        city=city,
        industry=industry,
        teaser=teaser,
    )
    kpis = db.compute_kpis(leads)
    payload = {
        "product": "SignalLead PL",
        "mode": "DEMO / TEASER" if teaser else "PEŁNE DANE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": kpis,
        "leads": leads,
    }
    mode_tag = "DEMO_TEASER" if teaser else "PELNA_PACZKA"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"SignalLead_PL_{mode_tag}_{stamp}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/leads/{domain:path}")
async def delete_lead(domain: str) -> dict:
    deleted = db.delete_lead(domain)
    if not deleted:
        raise HTTPException(status_code=404, detail="Nie znaleziono domeny w bazie.")
    return {"status": "deleted", "domain": domain}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
