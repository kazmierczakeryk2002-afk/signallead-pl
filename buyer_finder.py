"""
SignalLead PL — Module 1 (Stage 2): Buyer Finder & Teaser-First Outreach Engine.

Finds direct B2B buyers for SignalLead PL packs (Polish marketing agencies,
SEO/SEM agencies, performance 360 agencies, and Software Houses), enriches them
live from their websites (emails, phones, NIP, LinkedIn, offered services),
tracks outreach CRM status in SQLite, and generates personalized Polish
Teaser-First cold emails & LinkedIn DMs tied directly to audited lead packs.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from engine import (
    CITIES,
    SKIP_HARVEST_DOMAINS,
    USER_AGENT,
    LeadDatabase,
    extract_emails,
    extract_polish_nip,
    extract_polish_phones,
    extract_social_links,
    normalize_url,
)

logger = logging.getLogger("signallead.buyer_finder")

AGENCY_TYPES: list[str] = [
    "Software House / Agencja Web Dev",
    "Agencja Performance & SEO 360",
    "Agencja SEO / SEM",
    "Agencja Digital Marketing",
]

OUTREACH_STATUSES: dict[str, str] = {
    "new": "Nowa",
    "teaser_sent": "Wysłano Demo",
    "replied": "Odpowiedziała",
    "paid": "Opłacona",
    "rejected": "Odrzucona",
}

# Service detection keywords on Polish agency websites
SERVICE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SEO / Pozycjonowanie": ("pozycjonowanie", "audyt seo", "agencja seo", "seo", "widoczność w google"),
    "Google Ads / SEM": ("google ads", "kampanie ads", "adwords", "sem", "linki sponsorowane"),
    "Meta Ads / Social Ads": ("meta ads", "facebook ads", "instagram ads", "tiktok ads", "social media ads"),
    "Analityka GA4 / GTM": ("google analytics", "ga4", "tag manager", "gtm", "analityka internetowa"),
    "Web Dev / WordPress": ("tworzenie stron", "strony internetowe", "wordpress", "woocommerce", "web development"),
    "Software House / Custom IT": ("software house", "aplikacje webowe", "dedykowane oprogramowanie", "react", "next.js", "python"),
    "E-commerce": ("sklepy internetowe", "e-commerce", "prestashop", "shopify", "shoper", "magento"),
}

# Curated pool of real Polish agencies & Software Houses per city (fallback + enrichment)
CURATED_POLISH_AGENCIES: dict[str, list[dict[str, str]]] = {
    "Warszawa": [
        {"agency_name": "Tigers", "url": "https://tigers.pl", "agency_type": "Agencja Digital Marketing"},
        {"agency_name": "Semcore", "url": "https://semcore.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "K2 Precise", "url": "https://www.k2precise.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "10Clouds", "url": "https://10clouds.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Whites", "url": "https://agencjawhites.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "SalesTube", "url": "https://salestube.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "Daftcode", "url": "https://daftcode.pl", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Elephate", "url": "https://www.elephate.com", "agency_type": "Agencja SEO / SEM"},
    ],
    "Kraków": [
        {"agency_name": "Delante", "url": "https://delante.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "DevaGroup", "url": "https://www.devagroup.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "Harbingers", "url": "https://harbingers.io", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "Pragmatic Coders", "url": "https://www.pragmaticcoders.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Verseo", "url": "https://www.verseo.pl", "agency_type": "Agencja Digital Marketing"},
        {"agency_name": "Miquido", "url": "https://www.miquido.com", "agency_type": "Software House / Agencja Web Dev"},
    ],
    "Wrocław": [
        {"agency_name": "Artefakt", "url": "https://www.artefakt.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "Grupa TENSE", "url": "https://www.grupa-tense.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "Monterail", "url": "https://www.monterail.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Divante", "url": "https://www.divante.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Semgence", "url": "https://semgence.com", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "Empressia", "url": "https://www.empressia.pl", "agency_type": "Agencja Digital Marketing"},
    ],
    "Poznań": [
        {"agency_name": "Widoczni", "url": "https://widoczni.com", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "Netguru", "url": "https://www.netguru.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Symetria UX", "url": "https://www.symetria.pl", "agency_type": "Agencja Digital Marketing"},
        {"agency_name": "Sunrise System", "url": "https://www.sunrisesystem.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "Merixstudio", "url": "https://www.merixstudio.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Empressia", "url": "https://www.empressia.pl", "agency_type": "Agencja Performance & SEO 360"},
    ],
    "Gdańsk": [
        {"agency_name": "Traffic Trends", "url": "https://traffictrends.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "Future Processing", "url": "https://www.future-processing.pl", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Digitalk", "url": "https://digitalk.pl", "agency_type": "Agencja Digital Marketing"},
        {"agency_name": "Mayko", "url": "https://mayko.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "Ideacto", "url": "https://ideacto.pl", "agency_type": "Software House / Agencja Web Dev"},
    ],
    "Katowice": [
        {"agency_name": "Silesia SEM", "url": "https://www.silesiasem.pl", "agency_type": "Agencja SEO / SEM"},
        {"agency_name": "The Software House", "url": "https://tsh.io", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Boldare", "url": "https://www.boldare.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Grupa iCEA", "url": "https://www.grupa-icea.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "More Bananas", "url": "https://www.morebananas.pl", "agency_type": "Agencja Digital Marketing"},
    ],
    "Łódź": [
        {"agency_name": "Bluerank", "url": "https://www.bluerank.pl", "agency_type": "Agencja Performance & SEO 360"},
        {"agency_name": "HTD Health", "url": "https://htdhealth.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "BinarApps", "url": "https://binarapps.com", "agency_type": "Software House / Agencja Web Dev"},
        {"agency_name": "Agencja Interaktywna KS", "url": "https://ks.pl", "agency_type": "Agencja Digital Marketing"},
        {"agency_name": "Semstorm", "url": "https://www.semstorm.com", "agency_type": "Agencja SEO / SEM"},
    ],
}

BUYER_EMAIL_PREFIXES = (
    "newbusiness",
    "sprzedaz",
    "sales",
    "wspolpraca",
    "partner",
    "hello",
    "kontakt",
    "biuro",
    "office",
    "agencja",
    "info",
)


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------


@dataclass
class AgencyBuyer:
    domain: str
    url: str
    agency_name: str
    city: str
    agency_type: str
    services: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    nip: Optional[str] = None
    social_links: dict[str, str] = field(default_factory=dict)
    http_status: int = 0
    outreach_status: str = "new"
    notes: str = ""
    source: str = "curated_pl_agencies"
    discovered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# AgencyHarvesterAgent
# ---------------------------------------------------------------------------


class AgencyHarvesterAgent:
    """
    Discovers Polish marketing/SEO/performance agencies and Software Houses
    and enriches them live by crawling homepage + /kontakt + /o-nas.
    """

    OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    async def discover_targets(
        self,
        city: str,
        agency_type: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        if city not in CITIES:
            city = "Warszawa"

        targets: list[dict[str, str]] = []
        seen: set[str] = set()

        # 1. Curated verified Polish agencies first (high B2B buyer relevance)
        curated = CURATED_POLISH_AGENCIES.get(city, [])
        filtered_curated = [
            item
            for item in curated
            if not agency_type
            or agency_type == "Wszystkie"
            or item["agency_type"] == agency_type
        ]
        # If strict filter yields fewer than limit, include all agencies from city as secondary
        pool = filtered_curated + [
            item for item in curated if item not in filtered_curated
        ]
        for item in pool:
            try:
                canonical, domain = normalize_url(item["url"])
            except ValueError:
                continue
            if domain in seen:
                continue
            seen.add(domain)
            targets.append(
                {
                    "agency_name": item["agency_name"],
                    "url": canonical,
                    "domain": domain,
                    "city": city,
                    "agency_type": item["agency_type"],
                    "source": "curated_pl_agencies",
                }
            )
            if len(targets) >= limit:
                return targets

        # 2. Top up via OpenStreetMap Overpass (advertising_agency / it offices)
        osm_targets = await self._query_overpass_agencies(city, limit=limit * 2)
        for item in osm_targets:
            try:
                canonical, domain = normalize_url(item["url"])
            except ValueError:
                continue
            if domain in seen or any(
                domain == skip or domain.endswith(f".{skip}")
                for skip in SKIP_HARVEST_DOMAINS
            ):
                continue
            seen.add(domain)
            targets.append(
                {
                    "agency_name": item.get("agency_name") or domain,
                    "url": canonical,
                    "domain": domain,
                    "city": city,
                    "agency_type": item.get("agency_type")
                    or (
                        agency_type
                        if agency_type and agency_type != "Wszystkie"
                        else "Agencja Digital Marketing"
                    ),
                    "source": "osm_overpass",
                }
            )
            if len(targets) >= limit:
                break

        return targets

    async def _query_overpass_agencies(
        self, city: str, limit: int = 12
    ) -> list[dict[str, str]]:
        south, west, north, east = CITIES[city]
        bbox = f"{south},{west},{north},{east}"
        query = (
            f"[out:json][timeout:8];"
            f'('
            f'node["office"="advertising_agency"]["website"]({bbox});'
            f'way["office"="advertising_agency"]["website"]({bbox});'
            f'node["office"="it"]["website"]({bbox});'
            f'way["office"="it"]["website"]({bbox});'
            f');out tags {limit};'
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers={"User-Agent": USER_AGENT}
            ) as client:
                resp = await client.post(self.OVERPASS_ENDPOINT, data={"data": query})
                if resp.status_code != 200:
                    return []
                elements = resp.json().get("elements", [])
                out: list[dict[str, str]] = []
                for el in elements:
                    tags = el.get("tags", {})
                    raw_url = tags.get("website") or tags.get("contact:website")
                    if not raw_url:
                        continue
                    office = tags.get("office", "")
                    inferred_type = (
                        "Software House / Agencja Web Dev"
                        if office == "it"
                        else "Agencja Digital Marketing"
                    )
                    out.append(
                        {
                            "agency_name": tags.get("name") or "",
                            "url": raw_url,
                            "agency_type": inferred_type,
                        }
                    )
                return out
        except Exception as exc:
            logger.debug("Overpass agency query failed: %s", exc)
            return []

    async def enrich_agency(
        self,
        raw_url: str,
        city: str = "Warszawa",
        hint_type: Optional[str] = None,
        hint_name: Optional[str] = None,
        source: str = "manual",
    ) -> AgencyBuyer:
        """Crawl agency homepage + contact/about pages to extract contacts, NIP, and services."""
        canonical_url, domain = normalize_url(raw_url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        }

        html_home = ""
        final_url = canonical_url
        http_status = 0

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                verify=False,
                headers=headers,
            ) as client:
                resp = await client.get(canonical_url)
                http_status = resp.status_code
                final_url = str(resp.url)
                if resp.status_code < 400:
                    html_home = resp.text[:400_000]
        except Exception as exc:
            logger.debug("Agency fetch failed for %s: %s", domain, exc)

        if not html_home:
            return AgencyBuyer(
                domain=domain,
                url=canonical_url,
                agency_name=hint_name or domain.split(".")[0].title(),
                city=city,
                agency_type=hint_type or "Agencja Digital Marketing",
                services=[],
                emails=[],
                phones=[],
                nip=None,
                social_links={},
                http_status=http_status,
                source=source,
            )

        soup_home = BeautifulSoup(html_home, "lxml")

        # Discover & fetch contact/about pages concurrently
        sub_urls = self._discover_agency_subpages(final_url, domain, soup_home)
        sub_htmls = await self._fetch_subpages(sub_urls)

        all_chunks = [html_home] + list(sub_htmls.values())
        all_soups = [soup_home] + [BeautifulSoup(c, "lxml") for c in sub_htmls.values()]
        combined_html = "\n".join(all_chunks)
        combined_text = "\n".join(s.get_text(" ", strip=True) for s in all_soups)
        tel_links = [
            a["href"]
            for s in all_soups
            for a in s.find_all("a", href=True)
            if a["href"].strip().lower().startswith("tel:")
        ]

        agency_name = self._extract_agency_name(soup_home, domain, hint_name)
        services = self._detect_services(combined_text)
        agency_type = self._classify_agency_type(services, hint_type)
        nip = extract_polish_nip(combined_text) or extract_polish_nip(combined_html)
        emails = self._prioritize_buyer_emails(
            extract_emails(combined_html, combined_text, domain), domain
        )
        phones = extract_polish_phones(tel_links, combined_text, found_nip=nip)
        social_links = extract_social_links(soup_home)
        for s in all_soups[1:]:
            for k, v in extract_social_links(s).items():
                social_links.setdefault(k, v)

        return AgencyBuyer(
            domain=domain,
            url=final_url,
            agency_name=agency_name,
            city=city,
            agency_type=agency_type,
            services=services,
            emails=emails,
            phones=phones,
            nip=nip,
            social_links=social_links,
            http_status=http_status,
            source=source,
        )

    def _discover_agency_subpages(
        self, base_url: str, domain: str, soup: BeautifulSoup
    ) -> list[str]:
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates = [f"{origin}/kontakt", f"{origin}/contact"]
        seen = set(candidates)

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            full = urljoin(base_url, href).split("#")[0].rstrip("/")
            p = urlparse(full)
            host = (p.hostname or "").lower()
            if not (host == domain or host.endswith(f".{domain}")):
                continue
            path_low = p.path.lower()
            if any(
                kw in path_low
                for kw in ("kontakt", "contact", "o-nas", "about", "oferta", "polityka")
            ) and full not in seen:
                seen.add(full)
                candidates.append(full)
                if len(candidates) >= 3:
                    break
        return candidates[:3]

    async def _fetch_subpages(self, urls: list[str]) -> dict[str, str]:
        results: dict[str, str] = {}
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"}

        async def fetch_one(client: httpx.AsyncClient, url: str) -> None:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and "text/html" in resp.headers.get(
                    "content-type", ""
                ):
                    results[url] = resp.text[:300_000]
            except Exception:
                return

        async with httpx.AsyncClient(
            timeout=5.5, follow_redirects=True, verify=False, headers=headers
        ) as client:
            await asyncio.gather(*(fetch_one(client, u) for u in urls))
        return results

    @staticmethod
    def _extract_agency_name(
        soup: BeautifulSoup, domain: str, hint: Optional[str]
    ) -> str:
        if hint and hint.strip() and hint.strip().lower() != domain.lower():
            return hint.strip()
        og = soup.find("meta", attrs={"property": "og:site_name"})
        if og and og.get("content", "").strip():
            val = og["content"].strip()
            if 2 <= len(val) <= 65:
                return val
        if soup.title and soup.title.string:
            parts = re.split(r"\s*[\|\-–—•]\s*", soup.title.string.strip())
            if parts and 2 <= len(parts[0].strip()) <= 65:
                return parts[0].strip()
        return domain.split(".")[0].replace("-", " ").title()

    @staticmethod
    def _detect_services(text: str) -> list[str]:
        low = text.lower()
        found: list[str] = []
        for label, keywords in SERVICE_KEYWORDS.items():
            if any(kw in low for kw in keywords):
                found.append(label)
        return found

    @staticmethod
    def _classify_agency_type(
        services: list[str], hint_type: Optional[str]
    ) -> str:
        if hint_type and hint_type in AGENCY_TYPES:
            return hint_type
        s = set(services)
        if "Software House / Custom IT" in s and "SEO / Pozycjonowanie" not in s:
            return "Software House / Agencja Web Dev"
        if "SEO / Pozycjonowanie" in s and (
            "Google Ads / SEM" in s or "Meta Ads / Social Ads" in s
        ):
            return "Agencja Performance & SEO 360"
        if "SEO / Pozycjonowanie" in s or "Google Ads / SEM" in s:
            return "Agencja SEO / SEM"
        if "Web Dev / WordPress" in s and "Meta Ads / Social Ads" not in s:
            return "Software House / Agencja Web Dev"
        return "Agencja Digital Marketing"

    @staticmethod
    def _prioritize_buyer_emails(emails: list[str], domain: str) -> list[str]:
        def rank(e: str) -> tuple[int, int, str]:
            local, _, host = e.partition("@")
            same = 0 if (host == domain or host.endswith(f".{domain}")) else 1
            pref = 0 if any(local.startswith(p) for p in BUYER_EMAIL_PREFIXES) else 1
            return (same, pref, e)

        return sorted(emails, key=rank)[:5]


# ---------------------------------------------------------------------------
# OutreachOfferGenerator (Teaser-First Sales Pitch Generator)
# ---------------------------------------------------------------------------


class OutreachOfferGenerator:
    """
    Matches audited SignalLead PL leads to a target Polish agency and builds
    a personalized Teaser-First Cold Email + LinkedIn DM + Demo CSV download link.
    """

    @staticmethod
    def _fmt_pln(amount: int) -> str:
        return f"{amount:,}".replace(",", " ") + " PLN"

    @staticmethod
    def _pl_plural(n: int, one: str, few: str, many: str) -> str:
        if n == 1:
            return f"{n} {one}"
        mod10 = n % 10
        mod100 = n % 100
        if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
            return f"{n} {few}"
        return f"{n} {many}"

    def generate(
        self,
        buyer: dict[str, Any],
        lead_db: LeadDatabase,
        pack_city: Optional[str] = None,
        min_score: int = 0,
        match_persona_only: bool = False,
        sender_name: str = "Zespół SignalLead PL",
        pack_url: Optional[str] = None,
        pack_price_pln: Optional[int] = None,
    ) -> dict[str, Any]:
        target_city = pack_city if (pack_city and pack_city != "Wszystkie") else buyer.get("city")
        persona_filter = buyer.get("agency_type") if match_persona_only else None

        # 1. Try exact city (+ optional persona)
        matched = lead_db.list_leads(
            min_score=min_score,
            city=target_city,
            buyer_persona=persona_filter,
            teaser=False,
        )
        scope_label = f"w mieście {target_city}"
        used_city_param = target_city

        # 2. If exact city has 0 leads in DB, broaden gracefully to all audited Polish leads
        if not matched:
            matched = lead_db.list_leads(
                min_score=min_score,
                buyer_persona=persona_filter,
                teaser=False,
            )
            scope_label = "w Polsce (najświeższa paczka B2B)"
            used_city_param = None

        # 3. If persona filter still yielded 0, broaden across all personas
        if not matched and match_persona_only:
            matched = lead_db.list_leads(min_score=min_score, teaser=False)
            scope_label = "w Polsce (najświeższa paczka B2B)"
            used_city_param = None

        kpis = LeadDatabase.compute_kpis(matched)

        # Summarize top recurring signals & industries in the matched pack
        signal_counter: Counter[str] = Counter()
        industry_counter: Counter[str] = Counter()
        city_counter: Counter[str] = Counter()
        for lead in matched:
            industry_counter[lead.get("industry", "B2B")] += 1
            city_counter[lead.get("city", "Polska")] += 1
            for sig in lead.get("signals", []):
                signal_counter[sig.get("label", "")] += 1

        top_signals = [label for label, _ in signal_counter.most_common(3) if label]
        top_industries = [ind for ind, _ in industry_counter.most_common(2) if ind]
        cities_in_pack = ", ".join(c for c, _ in city_counter.most_common(3))

        agency_name = buyer.get("agency_name", "Zespół")
        agency_type = buyer.get("agency_type", "Agencja Digital Marketing")
        services = buyer.get("services") or []
        services_hint = ", ".join(services[:3]) if services else agency_type

        total = kpis["total_leads"]
        hot = kpis["hot_leads_75_plus"]
        effective_price = pack_price_pln if pack_price_pln is not None else kpis["bundle_price_pln"]
        bundle_pln = self._fmt_pln(effective_price)
        regular_pln = self._fmt_pln(kpis["pack_value_pln"])
        pipeline_pln = self._fmt_pln(kpis["total_contract_pipeline_pln"])
        with_nip = kpis["with_nip_count"]
        with_email = kpis["with_email_count"]

        industries_text = " / ".join(top_industries) if top_industries else "B2B"
        bullets_md = "\n".join(f"• {s}" for s in top_signals) if top_signals else "• Konkretne luki techniczne, SEO i analityczne (GA4 / Meta Pixel)"

        if total == 0:
            subject = f"Paczka gorących leadów sygnałowych B2B dla {agency_name} (Demo Teaser)"
            email_body = (
                f"Cześć Zespole {agency_name},\n\n"
                f"Widziałem, że specjalizujecie się w obszarze {services_hint}. "
                f"W SignalLead PL codziennie skanujemy polskie firmy B2B pod kątem kosztownych błędów "
                f"(brak SSL, TTFB > 1400 ms, brak GA4/GTM/Meta Pixela, braki SEO/Schema) i budujemy "
                f"gotowe paczki leadów z walidowanym NIP, e-mailem oraz spersonalizowanym icebreakerem.\n\n"
                f"Uruchom najpierw Harvester w zakładce 'Leady Sygnałowe', aby wygenerować paczkę z konkretnymi liczbami dla tej agencji.\n\n"
                f"Pozdrawiam,\n{sender_name}"
            )
            linkedin_dm = (
                f"Cześć! Widziałem Wasze realizacje w {agency_name} ({services_hint}). "
                f"Generujemy paczki polskich firm B2B z wykrytymi na żywo błędami technicznymi/SEO/Ads + NIP i e-mailem. "
                f"Mogę podesłać darmowe Demo CSV dla Waszego miasta?"
            )
        else:
            city_prefix = cities_in_pack if used_city_param is None else target_city
            firms_nom = self._pl_plural(total, "firma", "firmy", "firm")
            companies_acc = self._pl_plural(total, "konkretną spółkę", "konkretne spółki", "konkretnych spółek")
            verified_firms = self._pl_plural(total, "zweryfikowana firma", "zweryfikowane firmy", "zweryfikowanych firm")
            hot_part = f"w tym {self._pl_plural(hot, 'gorący', 'gorące', 'gorących')} 75+ pkt, " if hot > 0 else ""
            short_sig = re.sub(r"\s*\([^)]*\)", "", top_signals[0]).strip().lower() if top_signals else "braki GA4/SEO"

            pack_link_block = (
                f"\nPodgląd online + zakup na wyłączność (BLIK / Karta / P24):\n{pack_url}\n"
                if pack_url
                else ""
            )

            subject = (
                f"{city_prefix}: {firms_nom} ({industries_text}) z wykrytymi błędami WWW/Ads "
                f"— pipeline ~{pipeline_pln} [Darmowe Demo CSV]"
            )
            email_body = (
                f"Dzień dobry Zespole {agency_name},\n\n"
                f"Trafiłem na Waszą stronę — widzę, że mocno działacie w obszarze: {services_hint}. "
                f"Zamiast wysyłać pustą ofertę, przeskanowaliśmy na żywo firmy B2B {scope_label} "
                f"({industries_text}) i wyłapaliśmy {companies_acc}, które dziś tracą zapytania "
                f"przez proste do naprawienia luki:\n\n"
                f"{bullets_md}\n\n"
                f"Kluczowe liczby tej paczki:\n"
                f"• {verified_firms} ({hot_part}{with_nip} z walidowanym NIP, {with_email} z bezpośrednim e-mailem B2B)\n"
                f"• Łączny szacowany potencjał kontraktów dla Waszej agencji: ~{pipeline_pln}\n"
                f"• Każdy lead ma gotowy mini-audyt techniczny + spersonalizowany Icebreaker do cold maila\n\n"
                f"Jak to działa (model Teaser-First — wyłączność 1:1, zero ryzyka):\n"
                f"W załączniku przesyłam darmowy plik Demo CSV z pełną listą wykrytych błędów, TTFB, CMS i wyceną "
                f"kontraktów (nazwy domen i kontakty są w próbce zamaskowane, np. in*****.pl).\n\n"
                f"Jeśli po przejrzeniu Demo uznacie, że te firmy pasują do Waszego pipeline'u, "
                f"pełną paczkę CSV/JSON z odkrytymi domenami, NIP-ami i e-mailami odblokowujemy na wyłączność "
                f"za {bundle_pln} (cena końcowa bez VAT; zamiast {regular_pln} — po zakupie paczka znika ze sprzedaży)."
                f"{pack_link_block}\n"
                f"Czy mam zarezerwować tę paczkę na wyłączność dla {agency_name} na 48h?\n\n"
                f"Pozdrawiam,\n{sender_name}"
            )
            dm_cta = (
                f"Tutaj możesz od razu podejrzeć darmowe Demo (zamaskowane domeny, pełny audyt): {pack_url}"
                if pack_url
                else "Mogę wrzucić tu darmowe Demo CSV (zamaskowane domeny, pełny audyt błędów), żebyście ocenili jakość?"
            )
            linkedin_dm = (
                f"Cześć! Widzę, że w {agency_name} dowozicie {services_hint}. "
                f"Właśnie przeskanowaliśmy {firms_nom} ({industries_text}, {city_prefix}) z konkretnymi lukami "
                f"(m.in. {short_sig}) — łączny pipeline ok. {pipeline_pln}. "
                f"{dm_cta}"
            )

        # Build direct 1-click Teaser CSV download URL matching this offer
        qs_parts = ["teaser=true", f"min_score={min_score}"]
        if used_city_param:
            qs_parts.append(f"city={quote(used_city_param)}")
        teaser_csv_url = f"/api/export/csv?{'&'.join(qs_parts)}"

        return {
            "buyer_domain": buyer.get("domain"),
            "agency_name": agency_name,
            "recipient_email": (buyer.get("emails") or [""])[0],
            "subject": subject,
            "email_body": email_body,
            "linkedin_dm": linkedin_dm,
            "scope_label": scope_label,
            "pack_city_used": used_city_param or "Wszystkie",
            "top_signals": top_signals,
            "top_industries": top_industries,
            "matched_kpis": kpis,
            "teaser_csv_url": teaser_csv_url,
            "pack_url": pack_url,
        }


# ---------------------------------------------------------------------------
# BuyerDatabase (SQLite persistence in signallead.db)
# ---------------------------------------------------------------------------


class BuyerDatabase:
    """SQLite CRM persistence for discovered agency buyers."""

    def __init__(self, db_path: str | Path = "signallead.db") -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buyers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT UNIQUE NOT NULL,
                    url TEXT NOT NULL,
                    agency_name TEXT NOT NULL,
                    city TEXT NOT NULL,
                    agency_type TEXT NOT NULL,
                    services_json TEXT NOT NULL DEFAULT '[]',
                    emails_json TEXT NOT NULL DEFAULT '[]',
                    phones_json TEXT NOT NULL DEFAULT '[]',
                    nip TEXT,
                    social_links_json TEXT NOT NULL DEFAULT '{}',
                    http_status INTEGER NOT NULL DEFAULT 0,
                    outreach_status TEXT NOT NULL DEFAULT 'new',
                    notes TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'curated_pl_agencies',
                    discovered_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buyers_city ON buyers (city)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buyers_status ON buyers (outreach_status)"
            )
            conn.commit()

    def upsert_buyer(self, buyer: AgencyBuyer) -> dict[str, Any]:
        with self._connect() as conn:
            # Preserve existing outreach_status and notes if buyer already exists
            existing = conn.execute(
                "SELECT outreach_status, notes FROM buyers WHERE domain = ?",
                (buyer.domain,),
            ).fetchone()
            status = existing["outreach_status"] if existing else buyer.outreach_status
            notes = existing["notes"] if existing else buyer.notes

            conn.execute(
                """
                INSERT INTO buyers (
                    domain, url, agency_name, city, agency_type,
                    services_json, emails_json, phones_json, nip,
                    social_links_json, http_status, outreach_status,
                    notes, source, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    url=excluded.url,
                    agency_name=excluded.agency_name,
                    city=excluded.city,
                    agency_type=excluded.agency_type,
                    services_json=excluded.services_json,
                    emails_json=excluded.emails_json,
                    phones_json=excluded.phones_json,
                    nip=excluded.nip,
                    social_links_json=excluded.social_links_json,
                    http_status=excluded.http_status,
                    source=excluded.source,
                    discovered_at=excluded.discovered_at
                """,
                (
                    buyer.domain,
                    buyer.url,
                    buyer.agency_name,
                    buyer.city,
                    buyer.agency_type,
                    json.dumps(buyer.services, ensure_ascii=False),
                    json.dumps(buyer.emails, ensure_ascii=False),
                    json.dumps(buyer.phones, ensure_ascii=False),
                    buyer.nip,
                    json.dumps(buyer.social_links, ensure_ascii=False),
                    buyer.http_status,
                    status,
                    notes,
                    buyer.source,
                    buyer.discovered_at,
                ),
            )
            conn.commit()
        return self.get_buyer(buyer.domain) or buyer.to_dict()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "domain": row["domain"],
            "url": row["url"],
            "agency_name": row["agency_name"],
            "city": row["city"],
            "agency_type": row["agency_type"],
            "services": json.loads(row["services_json"] or "[]"),
            "emails": json.loads(row["emails_json"] or "[]"),
            "phones": json.loads(row["phones_json"] or "[]"),
            "nip": row["nip"],
            "social_links": json.loads(row["social_links_json"] or "{}"),
            "http_status": row["http_status"],
            "outreach_status": row["outreach_status"],
            "notes": row["notes"] or "",
            "source": row["source"],
            "discovered_at": row["discovered_at"],
        }

    def get_buyer(self, domain: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM buyers WHERE domain = ?", (domain,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_buyers(
        self,
        city: Optional[str] = None,
        agency_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []

        if city and city != "Wszystkie":
            clauses.append("city = ?")
            params.append(city)
        if agency_type and agency_type != "Wszystkie":
            clauses.append("agency_type = ?")
            params.append(agency_type)
        if status and status != "Wszystkie":
            clauses.append("outreach_status = ?")
            params.append(status)
        if search and search.strip():
            like = f"%{search.strip()}%"
            clauses.append(
                "(agency_name LIKE ? OR domain LIKE ? OR city LIKE ? OR services_json LIKE ?)"
            )
            params.extend([like, like, like, like])

        sql = f"SELECT * FROM buyers WHERE {' AND '.join(clauses)} ORDER BY discovered_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_status(
        self, domain: str, status: str, notes: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        if status not in OUTREACH_STATUSES:
            raise ValueError(
                f"Nieprawidłowy status. Dozwolone: {', '.join(OUTREACH_STATUSES.keys())}"
            )
        with self._connect() as conn:
            if notes is not None:
                cur = conn.execute(
                    "UPDATE buyers SET outreach_status = ?, notes = ? WHERE domain = ?",
                    (status, notes, domain),
                )
            else:
                cur = conn.execute(
                    "UPDATE buyers SET outreach_status = ? WHERE domain = ?",
                    (status, domain),
                )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_buyer(domain)

    def delete_buyer(self, domain: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM buyers WHERE domain = ?", (domain,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def compute_kpis(buyers: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "total_buyers": len(buyers),
            "with_email_count": sum(1 for b in buyers if b.get("emails")),
            "with_nip_count": sum(1 for b in buyers if b.get("nip")),
            "teaser_sent_count": sum(
                1 for b in buyers if b.get("outreach_status") == "teaser_sent"
            ),
            "replied_count": sum(
                1 for b in buyers if b.get("outreach_status") == "replied"
            ),
            "paid_count": sum(
                1 for b in buyers if b.get("outreach_status") == "paid"
            ),
        }

    @staticmethod
    def export_outreach_csv(
        buyers: list[dict[str, Any]], lead_db: LeadDatabase, min_score: int = 0
    ) -> str:
        """
        Export agencies together with personalized Teaser-First Cold Mail Subject & Body
        as UTF-8-SIG CSV ready for Excel, Woodpecker, Instantly, or Smartlead.
        """
        gen = OutreachOfferGenerator()
        out = io.StringIO()
        out.write("\ufeff")
        fieldnames = [
            "Agencja",
            "Domena",
            "Miasto",
            "Typ Agencji",
            "Status Outreach",
            "E-mail Główny",
            "Wszystkie E-maile",
            "Telefon",
            "NIP",
            "LinkedIn",
            "Wykryte Usługi",
            "Dopasowanych Leadów",
            "Cena Paczki (-25% PLN)",
            "Pipeline Agencji (PLN)",
            "Temat Cold Maila",
            "Treść Cold Maila (Teaser-First)",
            "Wiadomość LinkedIn DM",
        ]
        writer = csv.DictWriter(out, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for b in buyers:
            offer = gen.generate(buyer=b, lead_db=lead_db, min_score=min_score)
            kpis = offer["matched_kpis"]
            emails = b.get("emails", [])
            writer.writerow(
                {
                    "Agencja": b.get("agency_name", ""),
                    "Domena": b.get("domain", ""),
                    "Miasto": b.get("city", ""),
                    "Typ Agencji": b.get("agency_type", ""),
                    "Status Outreach": OUTREACH_STATUSES.get(
                        b.get("outreach_status", "new"), "Nowa"
                    ),
                    "E-mail Główny": emails[0] if emails else "",
                    "Wszystkie E-maile": ", ".join(emails),
                    "Telefon": ", ".join(b.get("phones", [])),
                    "NIP": b.get("nip") or "",
                    "LinkedIn": b.get("social_links", {}).get("linkedin", ""),
                    "Wykryte Usługi": ", ".join(b.get("services", [])),
                    "Dopasowanych Leadów": kpis["total_leads"],
                    "Cena Paczki (-25% PLN)": kpis["bundle_price_pln"],
                    "Pipeline Agencji (PLN)": kpis["total_contract_pipeline_pln"],
                    "Temat Cold Maila": offer["subject"],
                    "Treść Cold Maila (Teaser-First)": offer["email_body"],
                    "Wiadomość LinkedIn DM": offer["linkedin_dm"],
                }
            )
        return out.getvalue()


# ---------------------------------------------------------------------------
# High-level Orchestrator Helper
# ---------------------------------------------------------------------------


async def run_buyer_harvest(
    city: str,
    agency_type: Optional[str] = None,
    limit: int = 5,
    buyer_db: Optional[BuyerDatabase] = None,
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    """Discover & live-enrich Polish agencies for city + agency_type, persist to DB."""
    harvester = AgencyHarvesterAgent()
    targets = await harvester.discover_targets(
        city=city, agency_type=agency_type, limit=limit
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(t: dict[str, str]) -> dict[str, Any]:
        async with semaphore:
            record = await harvester.enrich_agency(
                raw_url=t["url"],
                city=t["city"],
                hint_type=t.get("agency_type"),
                hint_name=t.get("agency_name"),
                source=t.get("source", "curated_pl_agencies"),
            )
            if buyer_db is not None:
                return buyer_db.upsert_buyer(record)
            return record.to_dict()

    enriched = await asyncio.gather(*(worker(t) for t in targets))
    return sorted(
        enriched,
        key=lambda x: (1 if x.get("emails") else 0, len(x.get("services", []))),
        reverse=True,
    )
