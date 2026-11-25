"""
SignalLead PL — Stage 2 / Module 2: Exclusive Pack Store & Stripe Checkout Billing.

Implements:
  - PackDatabase: SQLite persistence (`packs` table in signallead.db) for frozen
    exclusive lead packs with dynamic pricing (-25% bundle) + manual price override,
    atomic reservation locking (prevents double-selling), cryptographic download tokens,
    and Polish B2B invoice data (NIP, company name, address, Stripe Invoice PDF).
  - StripeBillingService: Stripe Checkout integration tailored for Poland
    (PLN without VAT, BLIK + Card + Przelewy24, tax_id_collection for NIP,
    invoice_creation, webhook verification) plus a safe local Sandbox simulation
    mode when STRIPE_SECRET_KEY is not configured.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import stripe

from buyer_finder import BuyerDatabase
from engine import LeadDatabase, mask_lead_for_teaser, validate_polish_nip

logger = logging.getLogger("signallead.pack_store")

PACK_STATUS_LABELS: dict[str, str] = {
    "available": "Dostępna (Wyłączność)",
    "reserved": "Zarezerwowana (Płatność w toku)",
    "sold": "Sprzedana na wyłączność",
    "archived": "Zarchiwizowana",
}

POLISH_TRANSLIT = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)


class PackError(Exception):
    """Base error for pack operations."""


class PackNotFoundError(PackError):
    """Raised when a pack_id does not exist."""


class PackUnavailableError(PackError):
    """Raised when attempting to buy/edit a pack that is already reserved or sold."""


def _slugify(text: str, max_len: int = 24) -> str:
    ascii_text = text.translate(POLISH_TRANSLIT).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:max_len].strip("-") or "paczka"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).isoformat()


def _format_stripe_address(addr: Any) -> str:
    if not addr:
        return ""
    if isinstance(addr, str):
        return addr.strip()
    parts = [
        addr.get("line1") if isinstance(addr, dict) else getattr(addr, "line1", None),
        addr.get("line2") if isinstance(addr, dict) else getattr(addr, "line2", None),
        " ".join(
            p
            for p in (
                addr.get("postal_code") if isinstance(addr, dict) else getattr(addr, "postal_code", None),
                addr.get("city") if isinstance(addr, dict) else getattr(addr, "city", None),
            )
            if p
        ),
        addr.get("country") if isinstance(addr, dict) else getattr(addr, "country", None),
    ]
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# PackDatabase (SQLite persistence)
# ---------------------------------------------------------------------------


class PackDatabase:
    """SQLite persistence layer for exclusive SignalLead PL sales packs."""

    def __init__(self, db_path: str | Path = "signallead.db") -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS packs (
                    pack_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT 'Wszystkie',
                    industry TEXT NOT NULL DEFAULT 'Wszystkie',
                    min_score INTEGER NOT NULL DEFAULT 0,
                    lead_domains_json TEXT NOT NULL DEFAULT '[]',
                    leads_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    lead_count INTEGER NOT NULL DEFAULT 0,
                    hot_count INTEGER NOT NULL DEFAULT 0,
                    with_nip_count INTEGER NOT NULL DEFAULT 0,
                    with_email_count INTEGER NOT NULL DEFAULT 0,
                    pipeline_pln INTEGER NOT NULL DEFAULT 0,
                    regular_value_pln INTEGER NOT NULL DEFAULT 0,
                    suggested_price_pln INTEGER NOT NULL DEFAULT 0,
                    final_price_pln INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'PLN',
                    vat_mode TEXT NOT NULL DEFAULT 'non_vat',
                    exclusive INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'available',
                    reserved_until TEXT,
                    stripe_session_id TEXT,
                    stripe_payment_intent TEXT,
                    stripe_invoice_url TEXT,
                    download_token TEXT,
                    buyer_domain TEXT,
                    buyer_email TEXT,
                    buyer_name TEXT,
                    buyer_nip TEXT,
                    buyer_address TEXT,
                    payment_method TEXT,
                    created_at TEXT NOT NULL,
                    sold_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packs_status ON packs (status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packs_city ON packs (city)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packs_session ON packs (stripe_session_id)"
            )
            conn.commit()

    def release_expired_reservations(self) -> int:
        """Reset expired checkout reservations back to 'available'."""
        now_iso = _utc_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE packs
                SET status = 'available', reserved_until = NULL, stripe_session_id = NULL
                WHERE status = 'reserved' AND reserved_until IS NOT NULL AND reserved_until < ?
                """,
                (now_iso,),
            )
            conn.commit()
            return cur.rowcount

    def create_pack(
        self,
        leads: list[dict[str, Any]],
        title: Optional[str] = None,
        city: str = "Wszystkie",
        industry: str = "Wszystkie",
        min_score: int = 0,
        description: str = "",
        custom_price_pln: Optional[int] = None,
        exclusive: bool = True,
        buyer_domain: Optional[str] = None,
    ) -> dict[str, Any]:
        if not leads:
            raise ValueError(
                "Brak leadów spełniających wybrane filtry — najpierw przeskanuj firmy w zakładce Leady Sygnałowe."
            )

        kpis = LeadDatabase.compute_kpis(leads)
        suggested_price = max(29, int(kpis["bundle_price_pln"]))
        final_price = (
            int(custom_price_pln)
            if custom_price_pln is not None
            else suggested_price
        )
        if final_price < 10:
            raise ValueError("Minimalna cena paczki to 10 PLN.")

        if not title or not title.strip():
            city_lbl = city if city != "Wszystkie" else "Polska"
            ind_lbl = industry if industry != "Wszystkie" else "B2B"
            title = f"{city_lbl} — {ind_lbl} ({kpis['total_leads']} firm)"

        slug_prefix = _slugify(f"{city if city != 'Wszystkie' else 'pl'}-{industry if industry != 'Wszystkie' else 'b2b'}")
        pack_id = f"{slug_prefix}-{secrets.token_hex(2)}"
        domains = [l["domain"] for l in leads]
        now_iso = _utc_iso()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO packs (
                    pack_id, title, description, city, industry, min_score,
                    lead_domains_json, leads_snapshot_json,
                    lead_count, hot_count, with_nip_count, with_email_count,
                    pipeline_pln, regular_value_pln, suggested_price_pln, final_price_pln,
                    currency, vat_mode, exclusive, status, buyer_domain, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    'PLN', 'non_vat', ?, 'available', ?, ?
                )
                """,
                (
                    pack_id,
                    title.strip(),
                    description.strip(),
                    city,
                    industry,
                    min_score,
                    json.dumps(domains, ensure_ascii=False),
                    json.dumps(leads, ensure_ascii=False),
                    kpis["total_leads"],
                    kpis["hot_leads_75_plus"],
                    kpis["with_nip_count"],
                    kpis["with_email_count"],
                    kpis["total_contract_pipeline_pln"],
                    kpis["pack_value_pln"],
                    suggested_price,
                    final_price,
                    1 if exclusive else 0,
                    buyer_domain.strip().lower() if buyer_domain else None,
                    now_iso,
                ),
            )
            conn.commit()

        return self.get_pack(pack_id, include_admin_fields=True)  # type: ignore[return-value]

    def _row_to_dict(
        self,
        row: sqlite3.Row,
        include_teaser_leads: bool = False,
        include_full_leads: bool = False,
        include_admin_fields: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pack_id": row["pack_id"],
            "title": row["title"],
            "description": row["description"],
            "city": row["city"],
            "industry": row["industry"],
            "min_score": row["min_score"],
            "lead_count": row["lead_count"],
            "hot_count": row["hot_count"],
            "with_nip_count": row["with_nip_count"],
            "with_email_count": row["with_email_count"],
            "pipeline_pln": row["pipeline_pln"],
            "regular_value_pln": row["regular_value_pln"],
            "suggested_price_pln": row["suggested_price_pln"],
            "final_price_pln": row["final_price_pln"],
            "currency": row["currency"],
            "vat_mode": row["vat_mode"],
            "exclusive": bool(row["exclusive"]),
            "status": row["status"],
            "status_label": PACK_STATUS_LABELS.get(row["status"], row["status"]),
            "reserved_until": row["reserved_until"],
            "public_url": f"/pack/{row['pack_id']}",
            "created_at": row["created_at"],
            "sold_at": row["sold_at"],
        }

        if include_admin_fields:
            data.update(
                {
                    "buyer_domain": row["buyer_domain"],
                    "buyer_email": row["buyer_email"],
                    "buyer_name": row["buyer_name"],
                    "buyer_nip": row["buyer_nip"],
                    "buyer_address": row["buyer_address"],
                    "payment_method": row["payment_method"],
                    "stripe_session_id": row["stripe_session_id"],
                    "stripe_payment_intent": row["stripe_payment_intent"],
                    "stripe_invoice_url": row["stripe_invoice_url"],
                    "download_token": row["download_token"],
                }
            )

        if include_teaser_leads or include_full_leads:
            raw_leads = json.loads(row["leads_snapshot_json"] or "[]")
            if include_teaser_leads:
                data["teaser_leads"] = [mask_lead_for_teaser(l) for l in raw_leads]
            if include_full_leads:
                data["full_leads"] = raw_leads

        return data

    def get_pack(
        self,
        pack_id: str,
        include_teaser_leads: bool = False,
        include_full_leads: bool = False,
        include_admin_fields: bool = False,
    ) -> Optional[dict[str, Any]]:
        self.release_expired_reservations()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM packs WHERE pack_id = ?", (pack_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_dict(
            row,
            include_teaser_leads=include_teaser_leads,
            include_full_leads=include_full_leads,
            include_admin_fields=include_admin_fields,
        )

    def get_pack_by_session(self, session_id: str) -> Optional[dict[str, Any]]:
        self.release_expired_reservations()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM packs WHERE stripe_session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row, include_admin_fields=True)

    def list_packs(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        self.release_expired_reservations()
        clauses = ["1=1"]
        params: list[Any] = []
        if status and status != "Wszystkie":
            clauses.append("status = ?")
            params.append(status)
        sql = f"SELECT * FROM packs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r, include_admin_fields=True) for r in rows]

    def find_latest_available_pack_for_city(self, city: Optional[str]) -> Optional[dict[str, Any]]:
        self.release_expired_reservations()
        with self._connect() as conn:
            if city and city != "Wszystkie":
                row = conn.execute(
                    "SELECT * FROM packs WHERE status = 'available' AND city = ? ORDER BY created_at DESC LIMIT 1",
                    (city,),
                ).fetchone()
                if row:
                    return self._row_to_dict(row)
            row = conn.execute(
                "SELECT * FROM packs WHERE status = 'available' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_price(self, pack_id: str, new_price_pln: int) -> dict[str, Any]:
        if new_price_pln < 10:
            raise ValueError("Minimalna cena paczki to 10 PLN.")
        self.release_expired_reservations()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM packs WHERE pack_id = ?", (pack_id,)
            ).fetchone()
            if not row:
                raise PackNotFoundError("Nie znaleziono paczki.")
            if row["status"] != "available":
                raise PackUnavailableError(
                    "Nie można zmienić ceny paczki, która jest zarezerwowana lub sprzedana."
                )
            conn.execute(
                "UPDATE packs SET final_price_pln = ? WHERE pack_id = ?",
                (int(new_price_pln), pack_id),
            )
            conn.commit()
        return self.get_pack(pack_id, include_admin_fields=True)  # type: ignore[return-value]

    def delete_pack(self, pack_id: str) -> bool:
        self.release_expired_reservations()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM packs WHERE pack_id = ?", (pack_id,)
            ).fetchone()
            if not row:
                return False
            if row["status"] == "sold":
                raise PackUnavailableError(
                    "Nie można usunąć sprzedanej paczki (zachowujemy historię i dane do faktury)."
                )
            conn.execute("DELETE FROM packs WHERE pack_id = ?", (pack_id,))
            conn.commit()
            return True

    def reserve_pack(
        self, pack_id: str, session_id: str, minutes: int = 20
    ) -> dict[str, Any]:
        """Atomically reserve an exclusive pack during checkout to prevent double-selling."""
        now = _utc_now()
        now_iso = _utc_iso(now)
        until_iso = _utc_iso(now + timedelta(minutes=minutes))

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, reserved_until, exclusive FROM packs WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                raise PackNotFoundError("Nie znaleziono paczki.")

            status = row["status"]
            res_until = row["reserved_until"]

            if status == "sold":
                conn.rollback()
                raise PackUnavailableError(
                    "Ta paczka została już wykupiona na wyłączność przez inną agencję."
                )
            if status == "reserved" and res_until and res_until >= now_iso:
                conn.rollback()
                raise PackUnavailableError(
                    "Inna agencja jest właśnie w trakcie opłacania tej paczki. Spróbuj ponownie za kilkanaście minut."
                )

            conn.execute(
                """
                UPDATE packs
                SET status = 'reserved', reserved_until = ?, stripe_session_id = ?
                WHERE pack_id = ?
                """,
                (until_iso, session_id, pack_id),
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        return self.get_pack(pack_id, include_admin_fields=True)  # type: ignore[return-value]

    def release_reservation_by_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE packs
                SET status = 'available', reserved_until = NULL, stripe_session_id = NULL
                WHERE stripe_session_id = ? AND status = 'reserved'
                """,
                (session_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_pack_sold(
        self,
        pack_id: str,
        stripe_session_id: str,
        buyer_email: str = "",
        buyer_name: str = "",
        buyer_nip: Optional[str] = None,
        buyer_address: str = "",
        payment_method: str = "blik",
        stripe_payment_intent: Optional[str] = None,
        stripe_invoice_url: Optional[str] = None,
        buyer_db: Optional[BuyerDatabase] = None,
    ) -> dict[str, Any]:
        """Idempotently mark pack as sold, generate download_token, and sync Buyer CRM."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM packs WHERE pack_id = ?", (pack_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                raise PackNotFoundError("Nie znaleziono paczki.")

            # Idempotency: webhook + /success redirect may both invoke fulfillment
            if row["status"] == "sold":
                if row["stripe_session_id"] == stripe_session_id:
                    # Update invoice URL if webhook delivered it after /success
                    if stripe_invoice_url and not row["stripe_invoice_url"]:
                        conn.execute(
                            "UPDATE packs SET stripe_invoice_url = ? WHERE pack_id = ?",
                            (stripe_invoice_url, pack_id),
                        )
                        conn.commit()
                    else:
                        conn.rollback()
                    return self.get_pack(pack_id, include_admin_fields=True)  # type: ignore[return-value]
                conn.rollback()
                raise PackUnavailableError(
                    "Ta paczka została już sprzedana w innej transakcji."
                )

            token = secrets.token_urlsafe(32)
            sold_at = _utc_iso()
            clean_nip = (
                validate_polish_nip(buyer_nip) or buyer_nip.strip()
                if buyer_nip
                else None
            )

            conn.execute(
                """
                UPDATE packs
                SET status = 'sold',
                    reserved_until = NULL,
                    stripe_session_id = ?,
                    stripe_payment_intent = ?,
                    stripe_invoice_url = ?,
                    download_token = ?,
                    buyer_email = ?,
                    buyer_name = ?,
                    buyer_nip = ?,
                    buyer_address = ?,
                    payment_method = ?,
                    sold_at = ?
                WHERE pack_id = ?
                """,
                (
                    stripe_session_id,
                    stripe_payment_intent,
                    stripe_invoice_url,
                    token,
                    buyer_email.strip(),
                    buyer_name.strip(),
                    clean_nip,
                    buyer_address.strip(),
                    payment_method,
                    sold_at,
                    pack_id,
                ),
            )
            linked_buyer_domain = row["buyer_domain"]
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        # Sync agency CRM status to 'paid' if linked or email domain matches a known buyer
        if buyer_db is not None:
            candidate_domains = []
            if linked_buyer_domain:
                candidate_domains.append(linked_buyer_domain)
            if "@" in buyer_email:
                email_dom = buyer_email.split("@", 1)[1].strip().lower()
                if email_dom not in candidate_domains:
                    candidate_domains.append(email_dom)
            for dom in candidate_domains:
                try:
                    if buyer_db.get_buyer(dom):
                        buyer_db.update_status(
                            dom,
                            "paid",
                            notes=f"Opłacona paczka: {pack_id} ({payment_method.upper()})",
                        )
                        break
                except Exception as exc:
                    logger.debug("CRM buyer sync skipped for %s: %s", dom, exc)

        return self.get_pack(pack_id, include_admin_fields=True)  # type: ignore[return-value]

    def validate_download_token(
        self, pack_id: str, token: str
    ) -> Optional[dict[str, Any]]:
        """Validate download_token with constant-time comparison and return full pack."""
        if not token:
            return None
        pack = self.get_pack(
            pack_id, include_full_leads=True, include_admin_fields=True
        )
        if not pack or pack["status"] != "sold" or not pack.get("download_token"):
            return None
        if not secrets.compare_digest(pack["download_token"], token):
            return None
        return pack

    @staticmethod
    def compute_store_kpis(packs: list[dict[str, Any]]) -> dict[str, int]:
        available = [p for p in packs if p["status"] == "available"]
        reserved = [p for p in packs if p["status"] == "reserved"]
        sold = [p for p in packs if p["status"] == "sold"]
        return {
            "total_packs": len(packs),
            "available_packs": len(available),
            "reserved_packs": len(reserved),
            "sold_packs": len(sold),
            "realized_revenue_pln": sum(p["final_price_pln"] for p in sold),
            "open_inventory_pln": sum(p["final_price_pln"] for p in available),
            "total_pipeline_pln": sum(p["pipeline_pln"] for p in available),
        }


# ---------------------------------------------------------------------------
# StripeBillingService (Live Stripe + Safe Local Sandbox Mode)
# ---------------------------------------------------------------------------


class StripeBillingService:
    """
    Handles Stripe Checkout (PLN non-VAT, BLIK + Card + P24, NIP tax_id_collection,
    Stripe Invoice PDF) and webhook processing, with automatic local sandbox simulation
    when STRIPE_SECRET_KEY is not set.
    """

    def __init__(
        self,
        pack_db: PackDatabase,
        buyer_db: Optional[BuyerDatabase] = None,
    ) -> None:
        self.pack_db = pack_db
        self.buyer_db = buyer_db

    @property
    def secret_key(self) -> str:
        return os.getenv("STRIPE_SECRET_KEY", "").strip()

    @property
    def webhook_secret(self) -> str:
        return os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

    @property
    def is_live_configured(self) -> bool:
        return bool(self.secret_key)

    def create_checkout_session(
        self, pack_id: str, base_url: str
    ) -> dict[str, Any]:
        pack = self.pack_db.get_pack(pack_id)
        if not pack:
            raise PackNotFoundError("Nie znaleziono paczki.")
        if pack["status"] != "available":
            raise PackUnavailableError(
                "Ta paczka jest już niedostępna (zarezerwowana lub sprzedana na wyłączność)."
            )

        if not self.is_live_configured:
            raise RuntimeError(
                "STRIPE_SECRET_KEY nie jest ustawiony — użyj trybu symulacji na stronie paczki lub dodaj klucz Stripe w zmiennych środowiskowych."
            )

        stripe.api_key = self.secret_key
        clean_base = os.getenv("PUBLIC_BASE_URL", base_url).rstrip("/")

        # Create a temporary reservation key, then bind the actual Stripe session ID
        temp_key = f"pending_{secrets.token_hex(8)}"
        self.pack_db.reserve_pack(pack_id, session_id=temp_key, minutes=30)

        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                locale="pl",
                payment_method_types=["blik", "card", "p24"],
                line_items=[
                    {
                        "price_data": {
                            "currency": "pln",
                            "unit_amount": int(pack["final_price_pln"]) * 100,
                            "product_data": {
                                "name": f"SignalLead PL (Wyłączność): {pack['title']}",
                                "description": (
                                    f"{pack['lead_count']} firm B2B · Pipeline ~{pack['pipeline_pln']:,} PLN · "
                                    f"Pełny plik CSV/JSON + walidowane NIP-y i Icebreakery (sprzedaż bez VAT)"
                                ).replace(",", " "),
                            },
                        },
                        "quantity": 1,
                    }
                ],
                billing_address_collection="required",
                tax_id_collection={"enabled": True},
                invoice_creation={
                    "enabled": True,
                    "invoice_data": {
                        "description": (
                            f"Paczka leadów sygnałowych na wyłączność: {pack['title']} "
                            f"(sprzedaż zwolniona z VAT / rachunek bez VAT)"
                        ),
                        "metadata": {"pack_id": pack_id},
                    },
                },
                metadata={"pack_id": pack_id},
                success_url=f"{clean_base}/pack/{pack_id}/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{clean_base}/pack/{pack_id}?canceled=1",
                expires_at=int(time.time()) + 1800,
            )
            # Update reservation with real Stripe session ID
            with self.pack_db._connect() as conn:
                conn.execute(
                    "UPDATE packs SET stripe_session_id = ? WHERE pack_id = ? AND stripe_session_id = ?",
                    (session.id, pack_id, temp_key),
                )
                conn.commit()
            return {
                "mode": "stripe",
                "session_id": session.id,
                "checkout_url": session.url,
            }
        except Exception:
            self.pack_db.release_reservation_by_session(temp_key)
            raise

    def simulate_test_checkout(
        self,
        pack_id: str,
        buyer_email: str,
        buyer_name: str,
        buyer_nip: Optional[str] = None,
        buyer_address: str = "",
        payment_method: str = "blik",
    ) -> dict[str, Any]:
        """Local end-to-end checkout simulation when STRIPE_SECRET_KEY is not configured."""
        if self.is_live_configured:
            raise PermissionError(
                "Tryb symulacji jest wyłączony, gdy skonfigurowano STRIPE_SECRET_KEY."
            )
        if not buyer_email or "@" not in buyer_email:
            raise ValueError("Podaj poprawny adres e-mail nabywcy.")
        if not buyer_name or len(buyer_name.strip()) < 2:
            raise ValueError("Podaj nazwę firmy / agencji nabywcy.")
        if buyer_nip and buyer_nip.strip():
            valid_nip = validate_polish_nip(buyer_nip)
            if not valid_nip:
                raise ValueError(
                    "Podany NIP ma nieprawidłową sumę kontrolną (sprawdź 10 cyfr)."
                )
            buyer_nip = valid_nip

        session_id = f"sim_{secrets.token_hex(12)}"
        self.pack_db.reserve_pack(pack_id, session_id=session_id, minutes=10)
        sold_pack = self.pack_db.mark_pack_sold(
            pack_id=pack_id,
            stripe_session_id=session_id,
            buyer_email=buyer_email,
            buyer_name=buyer_name,
            buyer_nip=buyer_nip,
            buyer_address=buyer_address,
            payment_method=payment_method if payment_method in ("blik", "card", "p24") else "blik",
            stripe_payment_intent=f"pi_sim_{secrets.token_hex(8)}",
            stripe_invoice_url=None,
            buyer_db=self.buyer_db,
        )
        return {
            "mode": "sandbox",
            "session_id": session_id,
            "pack_id": pack_id,
            "download_token": sold_pack["download_token"],
            "success_url": f"/pack/{pack_id}/success?session_id={session_id}",
        }

    def _extract_session_fulfillment(self, session: Any) -> dict[str, Any]:
        """Extract buyer details, Polish NIP, invoice PDF, and payment method from a Stripe session dict/object."""
        if not isinstance(session, dict):
            session = session.to_dict_recursive() if hasattr(session, "to_dict_recursive") else dict(session)

        metadata = session.get("metadata") or {}
        pack_id = metadata.get("pack_id")
        cust = session.get("customer_details") or {}
        tax_ids = cust.get("tax_ids") or []
        buyer_nip = None
        for tid in tax_ids:
            val = tid.get("value") if isinstance(tid, dict) else None
            if val:
                buyer_nip = val
                break

        invoice = session.get("invoice")
        invoice_url = None
        if isinstance(invoice, dict):
            invoice_url = invoice.get("invoice_pdf") or invoice.get("hosted_invoice_url")
        elif isinstance(invoice, str) and self.is_live_configured:
            try:
                stripe.api_key = self.secret_key
                inv_obj = stripe.Invoice.retrieve(invoice)
                invoice_url = getattr(inv_obj, "invoice_pdf", None) or getattr(
                    inv_obj, "hosted_invoice_url", None
                )
            except Exception:
                invoice_url = None

        pm_types = session.get("payment_method_types") or ["blik"]
        payment_method = pm_types[0] if pm_types else "blik"
        pi = session.get("payment_intent")
        pi_id = pi.get("id") if isinstance(pi, dict) else pi

        return {
            "pack_id": pack_id,
            "stripe_session_id": session.get("id", ""),
            "payment_status": session.get("payment_status", ""),
            "buyer_email": cust.get("email") or session.get("customer_email") or "",
            "buyer_name": cust.get("name") or "",
            "buyer_nip": buyer_nip,
            "buyer_address": _format_stripe_address(cust.get("address")),
            "payment_method": payment_method,
            "stripe_payment_intent": pi_id,
            "stripe_invoice_url": invoice_url,
        }

    def verify_and_fulfill_session(
        self, session_id: str, expected_pack_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """
        Verify a completed checkout session (sandbox or real Stripe) and return the
        fulfilled pack including download_token for the /success page.
        """
        if not session_id:
            return None

        # 1. Check if already fulfilled in SQLite (works for both sandbox and webhook-fulfilled Stripe)
        existing = self.pack_db.get_pack_by_session(session_id)
        if existing and existing["status"] == "sold":
            if expected_pack_id and existing["pack_id"] != expected_pack_id:
                return None
            return existing

        # 2. If live Stripe is configured, verify directly with Stripe API (handles webhook race)
        if self.is_live_configured and not session_id.startswith("sim_"):
            stripe.api_key = self.secret_key
            session = stripe.checkout.Session.retrieve(
                session_id, expand=["invoice", "payment_intent"]
            )
            info = self._extract_session_fulfillment(session)
            if info["payment_status"] != "paid":
                return None
            pack_id = info["pack_id"] or expected_pack_id
            if not pack_id or (expected_pack_id and pack_id != expected_pack_id):
                return None
            return self.pack_db.mark_pack_sold(
                pack_id=pack_id,
                stripe_session_id=info["stripe_session_id"],
                buyer_email=info["buyer_email"],
                buyer_name=info["buyer_name"],
                buyer_nip=info["buyer_nip"],
                buyer_address=info["buyer_address"],
                payment_method=info["payment_method"],
                stripe_payment_intent=info["stripe_payment_intent"],
                stripe_invoice_url=info["stripe_invoice_url"],
                buyer_db=self.buyer_db,
            )

        return None

    def process_stripe_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a parsed Stripe webhook event dict (used by live webhook and tests)."""
        event_type = event.get("type", "")
        data_obj = (event.get("data") or {}).get("object") or {}

        if event_type == "checkout.session.completed":
            info = self._extract_session_fulfillment(data_obj)
            pack_id = info.get("pack_id")
            if not pack_id:
                return {"handled": False, "reason": "missing_pack_id"}
            if info.get("payment_status") not in ("paid", "no_payment_required"):
                return {"handled": False, "reason": "not_paid"}
            sold = self.pack_db.mark_pack_sold(
                pack_id=pack_id,
                stripe_session_id=info["stripe_session_id"],
                buyer_email=info["buyer_email"],
                buyer_name=info["buyer_name"],
                buyer_nip=info["buyer_nip"],
                buyer_address=info["buyer_address"],
                payment_method=info["payment_method"],
                stripe_payment_intent=info["stripe_payment_intent"],
                stripe_invoice_url=info["stripe_invoice_url"],
                buyer_db=self.buyer_db,
            )
            return {"handled": True, "event": event_type, "pack_id": sold["pack_id"]}

        if event_type == "checkout.session.expired":
            session_id = data_obj.get("id", "")
            released = self.pack_db.release_reservation_by_session(session_id)
            return {
                "handled": True,
                "event": event_type,
                "released": released,
            }

        return {"handled": False, "event": event_type}

    def handle_webhook(
        self, payload_bytes: bytes, sig_header: Optional[str]
    ) -> dict[str, Any]:
        """Verify Stripe signature (when webhook secret is set) and process event."""
        if self.webhook_secret:
            if not sig_header:
                raise ValueError("Brak nagłówka Stripe-Signature.")
            event = stripe.Webhook.construct_event(
                payload=payload_bytes,
                sig_header=sig_header,
                secret=self.webhook_secret,
            )
            event_dict = (
                event.to_dict_recursive()
                if hasattr(event, "to_dict_recursive")
                else dict(event)
            )
        else:
            event_dict = json.loads(payload_bytes.decode("utf-8"))
        return self.process_stripe_event(event_dict)
