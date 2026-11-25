"""
SignalLead PL — Stage 2 / Module 3: Automated Outreach Mailer (Specialist #8).

Sends personalized Polish Teaser-First cold emails with a real Demo CSV attachment
and live /pack/{pack_id} sales link to discovered agencies, with built-in deliverability
safeguards:
  - Daily send limit (DAILY_SEND_LIMIT, default 20/day)
  - 7-day per-agency anti-spam cooldown (prevents duplicate emails)
  - Multi-provider transport: Resend API (recommended for cloud), SMTP (STARTTLS/SSL),
    or automatic local Sandbox / Dry-Run mode when no credentials are configured
  - Persistent SQLite send log (`outreach_logs` in signallead.db)
  - Automatic Buyer CRM status update to `teaser_sent`
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

import httpx

from buyer_finder import BuyerDatabase, OutreachOfferGenerator
from engine import LeadDatabase
from pack_store import PackDatabase, _slugify

logger = logging.getLogger("signallead.outreach_mailer")


class OutreachError(Exception):
    """Base error for outreach sending."""


class DailyLimitReachedError(OutreachError):
    """Raised when the daily cold email safety limit has been reached."""


class CooldownActiveError(OutreachError):
    """Raised when an agency was already emailed within the cooldown window."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).isoformat()


# ---------------------------------------------------------------------------
# OutreachDatabase (SQLite send history & rate-limit tracking)
# ---------------------------------------------------------------------------


class OutreachDatabase:
    """SQLite persistence for outreach email logs and anti-spam guardrails."""

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
                CREATE TABLE IF NOT EXISTS outreach_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    buyer_domain TEXT NOT NULL,
                    agency_name TEXT NOT NULL,
                    recipient_email TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    pack_id TEXT,
                    pack_url TEXT,
                    attachment_filename TEXT,
                    attachment_bytes INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT 'sandbox',
                    provider_message_id TEXT,
                    status TEXT NOT NULL DEFAULT 'simulated',
                    error_message TEXT,
                    sent_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_domain ON outreach_logs (buyer_domain)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_sent_at ON outreach_logs (sent_at DESC)"
            )
            conn.commit()

    def count_sent_today(self) -> int:
        day_start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM outreach_logs
                WHERE sent_at >= ? AND status IN ('sent', 'simulated')
                """,
                (day_start,),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def was_emailed_recently(self, buyer_domain: str, cooldown_days: int = 7) -> bool:
        cutoff = _utc_iso(_utc_now() - timedelta(days=cooldown_days))
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM outreach_logs
                WHERE buyer_domain = ? AND sent_at >= ? AND status IN ('sent', 'simulated')
                LIMIT 1
                """,
                (buyer_domain.strip().lower(), cutoff),
            ).fetchone()
        return row is not None

    def record_log(
        self,
        buyer_domain: str,
        agency_name: str,
        recipient_email: str,
        subject: str,
        body_text: str,
        provider: str,
        status: str,
        pack_id: Optional[str] = None,
        pack_url: Optional[str] = None,
        attachment_filename: Optional[str] = None,
        attachment_bytes: int = 0,
        provider_message_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> dict[str, Any]:
        sent_at = _utc_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO outreach_logs (
                    buyer_domain, agency_name, recipient_email, subject, body_text,
                    pack_id, pack_url, attachment_filename, attachment_bytes,
                    provider, provider_message_id, status, error_message, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    buyer_domain.strip().lower(),
                    agency_name,
                    recipient_email.strip(),
                    subject,
                    body_text,
                    pack_id,
                    pack_url,
                    attachment_filename,
                    attachment_bytes,
                    provider,
                    provider_message_id,
                    status,
                    error_message,
                    sent_at,
                ),
            )
            log_id = cur.lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT * FROM outreach_logs WHERE id = ?", (log_id,)
            ).fetchone()
        return dict(row) if row else {}

    def list_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outreach_logs ORDER BY sent_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def compute_kpis(self, daily_limit: int) -> dict[str, int]:
        sent_today = self.count_sent_today()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM outreach_logs GROUP BY status"
            ).fetchall()
        counts = {r["status"]: int(r["cnt"]) for r in rows}
        return {
            "daily_limit": daily_limit,
            "sent_today": sent_today,
            "remaining_today": max(0, daily_limit - sent_today),
            "total_sent": counts.get("sent", 0),
            "total_simulated": counts.get("simulated", 0),
            "total_failed": counts.get("failed", 0),
            "total_all": sum(counts.values()),
        }


# ---------------------------------------------------------------------------
# OutreachMailerService (Resend / SMTP / Sandbox)
# ---------------------------------------------------------------------------


class OutreachMailerService:
    """
    Sends personalized Teaser-First offers with real Demo CSV attachments,
    enforcing daily limits and 7-day anti-spam cooldowns.
    """

    RESEND_ENDPOINT = "https://api.resend.com/emails"

    def __init__(
        self,
        lead_db: LeadDatabase,
        buyer_db: BuyerDatabase,
        pack_db: PackDatabase,
        outreach_db: OutreachDatabase,
    ) -> None:
        self.lead_db = lead_db
        self.buyer_db = buyer_db
        self.pack_db = pack_db
        self.outreach_db = outreach_db
        self.offer_generator = OutreachOfferGenerator()

    @property
    def resend_api_key(self) -> str:
        return os.getenv("RESEND_API_KEY", "").strip()

    @property
    def smtp_host(self) -> str:
        return os.getenv("SMTP_HOST", "").strip()

    @property
    def smtp_port(self) -> int:
        try:
            return int(os.getenv("SMTP_PORT", "587"))
        except ValueError:
            return 587

    @property
    def smtp_user(self) -> str:
        return os.getenv("SMTP_USER", "").strip()

    @property
    def smtp_password(self) -> str:
        return os.getenv("SMTP_PASSWORD", "").strip()

    @property
    def smtp_use_ssl(self) -> bool:
        return os.getenv("SMTP_USE_SSL", "").lower() in ("1", "true", "yes") or self.smtp_port == 465

    @property
    def mail_from(self) -> str:
        return os.getenv(
            "MAIL_FROM", "SignalLead PL <kontakt@signallead.pl>"
        ).strip()

    @property
    def reply_to(self) -> Optional[str]:
        val = os.getenv("MAIL_REPLY_TO", "").strip()
        return val or None

    @property
    def daily_limit(self) -> int:
        try:
            return max(1, int(os.getenv("DAILY_SEND_LIMIT", "20")))
        except ValueError:
            return 20

    @property
    def cooldown_days(self) -> int:
        try:
            return max(0, int(os.getenv("OUTREACH_COOLDOWN_DAYS", "7")))
        except ValueError:
            return 7

    @property
    def active_provider(self) -> str:
        if self.resend_api_key:
            return "resend"
        if self.smtp_host:
            return "smtp"
        return "sandbox"

    def get_status(self, log_limit: int = 30) -> dict[str, Any]:
        return {
            "provider": self.active_provider,
            "is_live": self.active_provider in ("resend", "smtp"),
            "mail_from": self.mail_from,
            "cooldown_days": self.cooldown_days,
            "kpis": self.outreach_db.compute_kpis(self.daily_limit),
            "logs": self.outreach_db.list_logs(limit=log_limit),
        }

    def _build_demo_attachment(
        self,
        target_city: Optional[str],
        min_score: int,
        active_pack: Optional[dict[str, Any]],
    ) -> tuple[str, bytes]:
        if active_pack:
            full = self.pack_db.get_pack(
                active_pack["pack_id"], include_teaser_leads=True
            )
            teaser_leads = (full or {}).get("teaser_leads", [])
            csv_text = self.lead_db.export_csv(teaser_leads, teaser=True)
            filename = f"SignalLead_PL_{active_pack['pack_id']}_DEMO.csv"
            return filename, csv_text.encode("utf-8")

        leads = self.lead_db.list_leads(
            min_score=min_score,
            city=None if (not target_city or target_city == "Wszystkie") else target_city,
            teaser=True,
        )
        if not leads:
            leads = self.lead_db.list_leads(min_score=0, teaser=True)
        csv_text = self.lead_db.export_csv(leads, teaser=True)
        city_slug = _slugify(target_city or "polska", max_len=16)
        filename = f"SignalLead_PL_{city_slug}_DEMO.csv"
        return filename, csv_text.encode("utf-8")

    async def _dispatch_resend(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        attachment_filename: Optional[str],
        attachment_bytes: Optional[bytes],
    ) -> str:
        payload: dict[str, Any] = {
            "from": self.mail_from,
            "to": [to_email],
            "subject": subject,
            "text": body_text,
        }
        if self.reply_to:
            payload["reply_to"] = self.reply_to
        if attachment_filename and attachment_bytes:
            payload["attachments"] = [
                {
                    "filename": attachment_filename,
                    "content": base64.b64encode(attachment_bytes).decode("ascii"),
                }
            ]

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self.RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                raise OutreachError(
                    f"Resend API błąd HTTP {resp.status_code}: {resp.text[:300]}"
                )
            data = resp.json()
            return str(data.get("id") or f"resend_{secrets.token_hex(6)}")

    def _dispatch_smtp_sync(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        attachment_filename: Optional[str],
        attachment_bytes: Optional[bytes],
    ) -> str:
        msg = EmailMessage()
        msg["From"] = self.mail_from
        msg["To"] = to_email
        msg["Subject"] = subject
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg.set_content(body_text, charset="utf-8")

        if attachment_filename and attachment_bytes:
            msg.add_attachment(
                attachment_bytes,
                maintype="text",
                subtype="csv",
                filename=attachment_filename,
            )

        if self.smtp_use_ssl:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15) as server:
                if self.smtp_user and self.smtp_password:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.ehlo()
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException:
                    pass
                if self.smtp_user and self.smtp_password:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
        return f"smtp_{secrets.token_hex(6)}"

    async def send_to_buyer(
        self,
        buyer_domain: str,
        base_url: str,
        pack_city: Optional[str] = None,
        min_score: int = 0,
        match_persona_only: bool = False,
        sender_name: str = "Zespół SignalLead PL",
        custom_subject: Optional[str] = None,
        custom_body: Optional[str] = None,
        attach_demo_csv: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        buyer = self.buyer_db.get_buyer(buyer_domain.strip().lower())
        if not buyer:
            raise LookupError(f"Nie znaleziono agencji '{buyer_domain}' w bazie Buyer Finder.")

        emails = buyer.get("emails") or []
        if not emails:
            raise ValueError(
                f"Agencja {buyer['agency_name']} ({buyer['domain']}) nie ma wykrytego adresu e-mail."
            )
        recipient = emails[0]

        # Safeguard 1: Daily send limit
        sent_today = self.outreach_db.count_sent_today()
        if sent_today >= self.daily_limit:
            raise DailyLimitReachedError(
                f"Osiągnięto bezpieczny dzienny limit wysyłki ({sent_today}/{self.daily_limit}). "
                f"Zwiększ DAILY_SEND_LIMIT lub kontynuuj jutro, aby chronić reputację domeny."
            )

        # Safeguard 2: 7-day per-agency anti-spam cooldown
        if (
            not force
            and self.cooldown_days > 0
            and self.outreach_db.was_emailed_recently(
                buyer["domain"], cooldown_days=self.cooldown_days
            )
        ):
            raise CooldownActiveError(
                f"Agencja {buyer['agency_name']} ({buyer['domain']}) otrzymała już wiadomość "
                f"w ciągu ostatnich {self.cooldown_days} dni (ochrona anty-spamowa)."
            )

        target_city = (
            pack_city
            if (pack_city and pack_city != "Wszystkie")
            else buyer.get("city")
        )
        active_pack = self.pack_db.find_latest_available_pack_for_city(target_city)
        clean_base = os.getenv("PUBLIC_BASE_URL", base_url).rstrip("/")
        pack_url = f"{clean_base}/pack/{active_pack['pack_id']}" if active_pack else None
        pack_price = active_pack["final_price_pln"] if active_pack else None

        offer = self.offer_generator.generate(
            buyer=buyer,
            lead_db=self.lead_db,
            pack_city=pack_city,
            min_score=min_score,
            match_persona_only=match_persona_only,
            sender_name=sender_name,
            pack_url=pack_url,
            pack_price_pln=pack_price,
        )

        subject = (custom_subject or "").strip() or offer["subject"]
        body_text = (custom_body or "").strip() or offer["email_body"]

        att_name: Optional[str] = None
        att_bytes: Optional[bytes] = None
        if attach_demo_csv:
            att_name, att_bytes = self._build_demo_attachment(
                target_city=target_city,
                min_score=min_score,
                active_pack=active_pack,
            )

        provider = self.active_provider
        try:
            if provider == "resend":
                msg_id = await self._dispatch_resend(
                    to_email=recipient,
                    subject=subject,
                    body_text=body_text,
                    attachment_filename=att_name,
                    attachment_bytes=att_bytes,
                )
                status = "sent"
            elif provider == "smtp":
                msg_id = await asyncio.to_thread(
                    self._dispatch_smtp_sync,
                    recipient,
                    subject,
                    body_text,
                    att_name,
                    att_bytes,
                )
                status = "sent"
            else:
                msg_id = f"sandbox_{secrets.token_hex(6)}"
                status = "simulated"
        except Exception as exc:
            log = self.outreach_db.record_log(
                buyer_domain=buyer["domain"],
                agency_name=buyer["agency_name"],
                recipient_email=recipient,
                subject=subject,
                body_text=body_text,
                provider=provider,
                status="failed",
                pack_id=active_pack["pack_id"] if active_pack else None,
                pack_url=pack_url,
                attachment_filename=att_name,
                attachment_bytes=len(att_bytes) if att_bytes else 0,
                error_message=str(exc),
            )
            raise OutreachError(f"Błąd wysyłki ({provider}): {exc}") from exc

        log = self.outreach_db.record_log(
            buyer_domain=buyer["domain"],
            agency_name=buyer["agency_name"],
            recipient_email=recipient,
            subject=subject,
            body_text=body_text,
            provider=provider,
            status=status,
            pack_id=active_pack["pack_id"] if active_pack else None,
            pack_url=pack_url,
            attachment_filename=att_name,
            attachment_bytes=len(att_bytes) if att_bytes else 0,
            provider_message_id=msg_id,
        )

        # Auto-advance Buyer CRM status to 'teaser_sent' (without downgrading replied/paid)
        if buyer.get("outreach_status") == "new":
            self.buyer_db.update_status(
                buyer["domain"],
                "teaser_sent",
                notes=f"Wysłano Demo ({provider}: {att_name or 'bez załącznika'})",
            )

        return {
            "provider": provider,
            "status": status,
            "log": log,
        }

    async def send_batch(
        self,
        base_url: str,
        city: Optional[str] = None,
        agency_type: Optional[str] = None,
        limit: int = 5,
        min_score: int = 0,
        sender_name: str = "Zespół SignalLead PL",
        attach_demo_csv: bool = True,
    ) -> dict[str, Any]:
        """Send Teaser-First emails sequentially to up to `limit` eligible 'new' agencies."""
        candidates = self.buyer_db.list_buyers(
            city=None if (not city or city == "Wszystkie") else city,
            agency_type=None
            if (not agency_type or agency_type == "Wszystkie")
            else agency_type,
            status="new",
        )
        eligible = [b for b in candidates if b.get("emails")]
        remaining = max(0, self.daily_limit - self.outreach_db.count_sent_today())
        to_process = eligible[: min(limit, remaining)]

        results: list[dict[str, Any]] = []
        sent_count = 0
        skipped_count = 0
        failed_count = 0

        for buyer in to_process:
            try:
                res = await self.send_to_buyer(
                    buyer_domain=buyer["domain"],
                    base_url=base_url,
                    pack_city=city if (city and city != "Wszystkie") else buyer.get("city"),
                    min_score=min_score,
                    sender_name=sender_name,
                    attach_demo_csv=attach_demo_csv,
                )
                sent_count += 1
                results.append(
                    {
                        "domain": buyer["domain"],
                        "agency_name": buyer["agency_name"],
                        "recipient": buyer["emails"][0],
                        "status": res["status"],
                        "attachment": res["log"].get("attachment_filename"),
                    }
                )
            except CooldownActiveError as exc:
                skipped_count += 1
                results.append(
                    {
                        "domain": buyer["domain"],
                        "agency_name": buyer["agency_name"],
                        "status": "skipped_cooldown",
                        "detail": str(exc),
                    }
                )
            except DailyLimitReachedError as exc:
                skipped_count += 1
                results.append(
                    {
                        "domain": buyer["domain"],
                        "agency_name": buyer["agency_name"],
                        "status": "daily_limit",
                        "detail": str(exc),
                    }
                )
                break
            except Exception as exc:
                failed_count += 1
                results.append(
                    {
                        "domain": buyer["domain"],
                        "agency_name": buyer["agency_name"],
                        "status": "failed",
                        "detail": str(exc),
                    }
                )

            # Polite small pause for live providers
            if self.active_provider in ("resend", "smtp"):
                await asyncio.sleep(1.0)

        return {
            "provider": self.active_provider,
            "eligible_found": len(eligible),
            "sent_count": sent_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "kpis": self.outreach_db.compute_kpis(self.daily_limit),
            "results": results,
        }
