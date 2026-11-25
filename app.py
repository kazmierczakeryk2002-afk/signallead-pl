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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from buyer_finder import (
    AGENCY_TYPES,
    OUTREACH_STATUSES,
    AgencyHarvesterAgent,
    BuyerDatabase,
    OutreachOfferGenerator,
    run_buyer_harvest,
)
from engine import (
    CITIES,
    INDUSTRIES,
    LeadDatabase,
    SignalAuditorAgent,
    mask_lead_for_teaser,
    run_harvest_and_audit,
)
from pack_store import (
    PACK_STATUS_LABELS,
    PackDatabase,
    PackNotFoundError,
    PackUnavailableError,
    StripeBillingService,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SIGNALLEAD_DB_PATH", str(BASE_DIR / "signallead.db")))
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
PACK_PAGE_FILE = BASE_DIR / "pack_page.html"

BUYER_PERSONAS = AGENCY_TYPES

app = FastAPI(
    title="SignalLead PL",
    description="Autonomiczny silnik leadów sygnałowych B2B w Polsce (Teaser-First + Buyer Finder + Stripe Pack Store)",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = LeadDatabase(DB_PATH)
buyer_db = BuyerDatabase(DB_PATH)
pack_db = PackDatabase(DB_PATH)
auditor = SignalAuditorAgent()
agency_harvester = AgencyHarvesterAgent()
offer_generator = OutreachOfferGenerator()
stripe_billing = StripeBillingService(pack_db=pack_db, buyer_db=buyer_db)


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


class BuyerHarvestRequest(BaseModel):
    city: str = Field(default="Warszawa")
    agency_type: Optional[str] = Field(default="Wszystkie")
    limit: int = Field(default=5, ge=1, le=15)


class BuyerAddRequest(BaseModel):
    url: str = Field(..., min_length=3, examples=["https://delante.pl"])
    city: str = Field(default="Kraków")
    agency_type: Optional[str] = Field(default=None)
    agency_name: Optional[str] = Field(default=None)


class BuyerOfferRequest(BaseModel):
    pack_city: Optional[str] = Field(default=None)
    min_score: int = Field(default=0, ge=0, le=99)
    match_persona_only: bool = Field(default=False)
    sender_name: str = Field(default="Zespół SignalLead PL")


class BuyerStatusRequest(BaseModel):
    status: str = Field(..., examples=["teaser_sent"])
    notes: Optional[str] = Field(default=None)


class PackCreateRequest(BaseModel):
    title: Optional[str] = Field(default=None)
    description: str = Field(default="")
    city: str = Field(default="Wszystkie")
    industry: str = Field(default="Wszystkie")
    min_score: int = Field(default=0, ge=0, le=99)
    custom_price_pln: Optional[int] = Field(default=None, ge=10)
    buyer_domain: Optional[str] = Field(default=None)


class PackPriceUpdateRequest(BaseModel):
    final_price_pln: int = Field(..., ge=10)


class SimulateCheckoutRequest(BaseModel):
    buyer_name: str = Field(..., min_length=2)
    buyer_email: str = Field(..., min_length=5)
    buyer_nip: Optional[str] = Field(default=None)
    buyer_address: str = Field(default="")
    payment_method: str = Field(default="blik")


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
async def get_meta() -> dict:
    return {
        "cities": list(CITIES.keys()),
        "industries": INDUSTRIES,
        "buyer_personas": BUYER_PERSONAS,
        "agency_types": AGENCY_TYPES,
        "outreach_statuses": OUTREACH_STATUSES,
        "pack_statuses": PACK_STATUS_LABELS,
        "stripe_live": stripe_billing.is_live_configured,
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


# ---------------------------------------------------------------------------
# Stage 2 / Module 1: Buyer Finder & Outreach Generator Routes
# ---------------------------------------------------------------------------


@app.get("/api/buyers")
async def get_buyers(
    city: Optional[str] = Query(default=None),
    agency_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
) -> dict:
    buyers = buyer_db.list_buyers(
        city=city,
        agency_type=agency_type,
        status=status,
        search=search,
    )
    kpis = buyer_db.compute_kpis(buyers)
    return {
        "kpis": kpis,
        "buyers": buyers,
    }


@app.post("/api/buyers/harvest")
async def harvest_agency_buyers(payload: BuyerHarvestRequest) -> dict:
    if payload.city not in CITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Nieobsługiwane miasto. Dostępne: {', '.join(CITIES.keys())}",
        )
    if payload.agency_type and payload.agency_type != "Wszystkie" and payload.agency_type not in AGENCY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Nieobsługiwany typ agencji. Dostępne: {', '.join(AGENCY_TYPES)}",
        )
    enriched = await run_buyer_harvest(
        city=payload.city,
        agency_type=payload.agency_type,
        limit=payload.limit,
        buyer_db=buyer_db,
    )
    return {
        "status": "ok",
        "city": payload.city,
        "agency_type": payload.agency_type or "Wszystkie",
        "discovered_count": len(enriched),
        "buyers": enriched,
    }


@app.post("/api/buyers/add")
async def add_single_buyer(payload: BuyerAddRequest) -> dict:
    try:
        record = await agency_harvester.enrich_agency(
            raw_url=payload.url,
            city=payload.city,
            hint_type=payload.agency_type,
            hint_name=payload.agency_name,
            source="manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Błąd podczas analizy strony agencji: {exc}"
        ) from exc

    saved = buyer_db.upsert_buyer(record)
    return {"status": "ok", "buyer": saved}


@app.get("/api/buyers/export/csv")
async def export_buyers_outreach_csv(
    city: Optional[str] = Query(default=None),
    agency_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=99),
) -> Response:
    buyers = buyer_db.list_buyers(
        city=city, agency_type=agency_type, status=status
    )
    csv_body = buyer_db.export_outreach_csv(buyers, lead_db=db, min_score=min_score)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"SignalLead_PL_Buyer_Outreach_{stamp}.csv"
    return Response(
        content=csv_body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/buyers/{domain}/offer")
async def generate_buyer_offer(
    domain: str, payload: BuyerOfferRequest, request: Request
) -> dict:
    buyer = buyer_db.get_buyer(domain)
    if not buyer:
        raise HTTPException(status_code=404, detail="Nie znaleziono agencji w bazie.")

    target_city = (
        payload.pack_city
        if (payload.pack_city and payload.pack_city != "Wszystkie")
        else buyer.get("city")
    )
    active_pack = pack_db.find_latest_available_pack_for_city(target_city)
    pack_url = None
    pack_price = None
    if active_pack:
        base_url = os.getenv(
            "PUBLIC_BASE_URL", str(request.base_url).rstrip("/")
        ).rstrip("/")
        pack_url = f"{base_url}/pack/{active_pack['pack_id']}"
        pack_price = active_pack["final_price_pln"]

    offer = offer_generator.generate(
        buyer=buyer,
        lead_db=db,
        pack_city=payload.pack_city,
        min_score=payload.min_score,
        match_persona_only=payload.match_persona_only,
        sender_name=payload.sender_name,
        pack_url=pack_url,
        pack_price_pln=pack_price,
    )
    return {"status": "ok", "offer": offer}


@app.patch("/api/buyers/{domain}/status")
async def update_buyer_status(domain: str, payload: BuyerStatusRequest) -> dict:
    try:
        updated = buyer_db.update_status(
            domain=domain, status=payload.status, notes=payload.notes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Nie znaleziono agencji w bazie.")
    return {"status": "ok", "buyer": updated}


@app.delete("/api/buyers/{domain}")
async def delete_buyer(domain: str) -> dict:
    deleted = buyer_db.delete_buyer(domain)
    if not deleted:
        raise HTTPException(status_code=404, detail="Nie znaleziono agencji w bazie.")
    return {"status": "deleted", "domain": domain}


# ---------------------------------------------------------------------------
# Stage 2 / Module 2: Exclusive Pack Store (/pack/{id}) & Stripe Billing
# ---------------------------------------------------------------------------


@app.get("/pack/{pack_id}", response_class=HTMLResponse)
@app.get("/pack/{pack_id}/success", response_class=HTMLResponse)
async def serve_pack_page(pack_id: str) -> HTMLResponse:
    if not PACK_PAGE_FILE.exists():
        raise HTTPException(status_code=500, detail="Brak pliku pack_page.html.")
    return HTMLResponse(content=PACK_PAGE_FILE.read_text(encoding="utf-8"))


@app.get("/api/packs")
async def list_packs(status: Optional[str] = Query(default=None)) -> dict:
    packs = pack_db.list_packs(status=status)
    kpis = pack_db.compute_store_kpis(packs)
    return {
        "stripe_live": stripe_billing.is_live_configured,
        "kpis": kpis,
        "packs": packs,
    }


@app.post("/api/packs")
async def create_pack(payload: PackCreateRequest) -> dict:
    leads = db.list_leads(
        min_score=payload.min_score,
        city=None if payload.city == "Wszystkie" else payload.city,
        industry=None if payload.industry == "Wszystkie" else payload.industry,
        teaser=False,
    )
    try:
        pack = pack_db.create_pack(
            leads=leads,
            title=payload.title,
            city=payload.city,
            industry=payload.industry,
            min_score=payload.min_score,
            description=payload.description,
            custom_price_pln=payload.custom_price_pln,
            exclusive=True,
            buyer_domain=payload.buyer_domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "pack": pack}


@app.patch("/api/packs/{pack_id}/price")
async def update_pack_price(pack_id: str, payload: PackPriceUpdateRequest) -> dict:
    try:
        pack = pack_db.update_price(pack_id, payload.final_price_pln)
    except PackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PackUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "pack": pack}


@app.delete("/api/packs/{pack_id}")
async def delete_pack(pack_id: str) -> dict:
    try:
        deleted = pack_db.delete_pack(pack_id)
    except PackUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Nie znaleziono paczki.")
    return {"status": "deleted", "pack_id": pack_id}


@app.get("/api/packs/{pack_id}/public")
async def get_pack_public(pack_id: str) -> dict:
    pack = pack_db.get_pack(pack_id, include_teaser_leads=True, include_admin_fields=False)
    if not pack:
        raise HTTPException(status_code=404, detail="Nie znaleziono paczki.")
    return {
        "stripe_live": stripe_billing.is_live_configured,
        "pack": pack,
    }


@app.get("/api/packs/{pack_id}/demo.csv")
async def download_pack_demo_csv(pack_id: str) -> Response:
    pack = pack_db.get_pack(pack_id, include_teaser_leads=True)
    if not pack:
        raise HTTPException(status_code=404, detail="Nie znaleziono paczki.")
    csv_body = db.export_csv(pack.get("teaser_leads", []), teaser=True)
    filename = f"SignalLead_PL_{pack_id}_DEMO.csv"
    return Response(
        content=csv_body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/packs/{pack_id}/checkout")
async def create_pack_checkout(pack_id: str, request: Request) -> dict:
    base_url = str(request.base_url).rstrip("/")
    try:
        result = stripe_billing.create_checkout_session(pack_id=pack_id, base_url=base_url)
    except PackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PackUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Błąd Stripe Checkout: {exc}") from exc
    return {"status": "ok", **result}


@app.post("/api/packs/{pack_id}/simulate-checkout")
async def simulate_pack_checkout(pack_id: str, payload: SimulateCheckoutRequest) -> dict:
    try:
        result = stripe_billing.simulate_test_checkout(
            pack_id=pack_id,
            buyer_email=payload.buyer_email,
            buyer_name=payload.buyer_name,
            buyer_nip=payload.buyer_nip,
            buyer_address=payload.buyer_address,
            payment_method=payload.payment_method,
        )
    except PackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PackUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **result}


@app.get("/api/packs/{pack_id}/verify")
async def verify_pack_purchase(
    pack_id: str, session_id: str = Query(..., min_length=4)
) -> dict:
    fulfilled = stripe_billing.verify_and_fulfill_session(
        session_id=session_id, expected_pack_id=pack_id
    )
    if not fulfilled:
        raise HTTPException(
            status_code=402,
            detail="Płatność nie została jeszcze potwierdzona dla tej sesji.",
        )
    full_pack = pack_db.get_pack(
        pack_id, include_full_leads=True, include_admin_fields=True
    )
    return {"status": "ok", "pack": full_pack}


@app.get("/api/packs/{pack_id}/download/csv")
async def download_unlocked_pack_csv(
    pack_id: str, token: str = Query(..., min_length=8)
) -> Response:
    pack = pack_db.validate_download_token(pack_id, token)
    if not pack:
        raise HTTPException(
            status_code=403,
            detail="Nieprawidłowy lub nieaktywny token pobierania pełnej paczki.",
        )
    csv_body = db.export_csv(pack.get("full_leads", []), teaser=False)
    filename = f"SignalLead_PL_{pack_id}_PELNA_PACZKA.csv"
    return Response(
        content=csv_body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/packs/{pack_id}/download/json")
async def download_unlocked_pack_json(
    pack_id: str, token: str = Query(..., min_length=8)
) -> Response:
    pack = pack_db.validate_download_token(pack_id, token)
    if not pack:
        raise HTTPException(
            status_code=403,
            detail="Nieprawidłowy lub nieaktywny token pobierania pełnej paczki.",
        )
    payload = {
        "product": "SignalLead PL — Exclusive Pack",
        "pack_id": pack["pack_id"],
        "title": pack["title"],
        "sold_to": pack.get("buyer_name"),
        "buyer_nip": pack.get("buyer_nip"),
        "price_pln_non_vat": pack["final_price_pln"],
        "sold_at": pack.get("sold_at"),
        "leads": pack.get("full_leads", []),
    }
    filename = f"SignalLead_PL_{pack_id}_PELNA_PACZKA.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict:
    payload_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        result = stripe_billing.handle_webhook(payload_bytes, sig_header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Błąd webhooka Stripe: {exc}") from exc
    return {"status": "ok", **result}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
