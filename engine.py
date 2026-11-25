"""
SignalLead PL — Multi-Agent Polish B2B Signal Lead Engine.

Agents:
  1. HarvesterAgent       — Discovers Polish B2B domains via OpenStreetMap Overpass API
                            with a curated Polish B2B fallback pool per city & industry.
  2. SignalAuditorAgent   — Live technical, SEO, Ads/tracking auditor & Polish B2B contact
                            extractor (homepage + /kontakt + /o-nas + privacy/about links),
                            including mathematical Polish NIP checksum validation, scoring,
                            buyer persona matching, pricing & Polish cold-mail icebreakers.
  3. LeadDatabase         — SQLite storage (signallead.db) with upsert by domain,
                            Demo/Teaser masking, KPI aggregation, and UTF-8-SIG CSV export.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("signallead.engine")

# ---------------------------------------------------------------------------
# Constants: Polish Cities, Industries, Overpass BBoxes & Curated Fallback
# ---------------------------------------------------------------------------

CITIES: dict[str, tuple[float, float, float, float]] = {
    "Warszawa": (52.10, 20.85, 52.35, 21.25),
    "Kraków": (49.97, 19.80, 50.12, 20.15),
    "Wrocław": (51.04, 16.85, 51.19, 17.16),
    "Poznań": (52.33, 16.80, 52.49, 17.06),
    "Gdańsk": (54.28, 18.45, 54.44, 18.78),
    "Katowice": (50.18, 18.89, 50.30, 19.12),
    "Łódź": (51.68, 19.32, 51.85, 19.62),
}

INDUSTRY_CONFIG: dict[str, dict[str, Any]] = {
    "Kancelarie Prawne/Podatkowe": {
        "osm_filters": [
            '["office"="lawyer"]',
            '["office"="tax_advisor"]',
            '["office"="notary"]',
            '["office"="accountant"]',
        ],
        "base_contract_pln": 9500,
    },
    "Kliniki Medyczne/Stomatologia": {
        "osm_filters": [
            '["amenity"="dentist"]',
            '["amenity"="clinic"]',
            '["healthcare"="dentist"]',
            '["healthcare"="clinic"]',
        ],
        "base_contract_pln": 12500,
    },
    "Architektura/Budownictwo": {
        "osm_filters": [
            '["office"="architect"]',
            '["office"="construction_company"]',
            '["office"="engineer"]',
        ],
        "base_contract_pln": 11500,
    },
    "Nieruchomości": {
        "osm_filters": [
            '["office"="estate_agent"]',
            '["office"="property_management"]',
        ],
        "base_contract_pln": 11000,
    },
    "Hotele B2B": {
        "osm_filters": [
            '["tourism"="hotel"]',
        ],
        "base_contract_pln": 14000,
    },
    "Produkcja/Logistyka": {
        "osm_filters": [
            '["office"="logistics"]',
            '["office"="company"]',
            '["industrial"]',
        ],
        "base_contract_pln": 15000,
    },
}

INDUSTRIES: list[str] = list(INDUSTRY_CONFIG.keys())

# Curated fallback pool of real Polish B2B company domains per city & industry.
# Used when Overpass API is rate-limited, unreachable, or yields fewer results than requested.
FALLBACK_DOMAINS: dict[str, dict[str, list[dict[str, str]]]] = {
    "Warszawa": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria DZP", "url": "https://www.dzp.pl"},
            {"company_name": "Kancelaria Wardyński i Wspólnicy", "url": "https://wardynski.com.pl"},
            {"company_name": "Kancelaria Traple Konarski Podrecki", "url": "https://www.traple.pl"},
            {"company_name": "Kancelaria SSW Pragmatic Solutions", "url": "https://ssw.solutions"},
            {"company_name": "Kancelaria Gessel", "url": "https://gessel.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Enel-Med Stomatologia", "url": "https://enel.pl"},
            {"company_name": "Klinika Ambroziak", "url": "https://klinikaambroziak.pl"},
            {"company_name": "Centrum Medyczne Damiana", "url": "https://www.damian.pl"},
            {"company_name": "Carolina Medical Center", "url": "https://carolina.pl"},
            {"company_name": "Villa Nova Dental Clinic", "url": "https://www.villanova.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "JEMS Architekci", "url": "https://jems.pl"},
            {"company_name": "APA Wojciechowski", "url": "https://www.apa.com.pl"},
            {"company_name": "Kuryłowicz & Associates", "url": "https://www.apaka.com.pl"},
            {"company_name": "Mostostal Warszawa", "url": "https://www.mostostal.waw.pl"},
            {"company_name": "Budimex SA", "url": "https://www.budimex.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "Dom Development", "url": "https://www.domd.pl"},
            {"company_name": "Robyg", "url": "https://robyg.pl"},
            {"company_name": "Hamilton May", "url": "https://www.hamiltonmay.pl"},
            {"company_name": "Partners International", "url": "https://partnersinternational.pl"},
            {"company_name": "Lions Estate", "url": "https://lionsestate.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Hotel Bristol Warszawa", "url": "https://www.hotelbristolwarsaw.pl"},
            {"company_name": "Hotel Bellotto", "url": "https://www.hotelbellotto.pl"},
            {"company_name": "Polonia Palace Hotel", "url": "https://www.poloniapalace.com"},
            {"company_name": "Hotel Metropol", "url": "https://www.hotelmetropol.com.pl"},
            {"company_name": "Arche Hotel Krakowska", "url": "https://www.archehotelkrakowska.pl"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Raben Group", "url": "https://polska.raben-group.com"},
            {"company_name": "Pekaes", "url": "https://www.pekaes.pl"},
            {"company_name": "Rohlig Suus Logistics", "url": "https://www.suus.com"},
            {"company_name": "Fiege Polska", "url": "https://www.fiege.pl"},
            {"company_name": "FM Logistic Polska", "url": "https://www.fmlogistic.pl"},
        ],
    },
    "Kraków": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria Kubas Kos Gałkowski", "url": "https://kkg.pl"},
            {"company_name": "Kancelaria Barta Litwiński", "url": "https://www.bartalitwinski.pl"},
            {"company_name": "Kancelaria SPCG", "url": "https://www.spcg.pl"},
            {"company_name": "Kancelaria KBiW", "url": "https://www.kbiw.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Indexmedica Kraków", "url": "https://www.indexmedica.pl"},
            {"company_name": "Stomatologia Cichoń", "url": "https://stomatologiacichon.pl"},
            {"company_name": "Centrum Medyczne iMed24", "url": "https://imed24.pl"},
            {"company_name": "Klinika Krakowskie Centrum Stomatologii", "url": "https://www.kcs.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "Ingarden & Ewý Architekci", "url": "https://www.iea.com.pl"},
            {"company_name": "NS Moon Studio", "url": "https://nsmoonstudio.pl"},
            {"company_name": "Biuro Architektoniczne B2 Studio", "url": "https://b2studio.com.pl"},
            {"company_name": "Chemobudowa Kraków", "url": "https://www.chemobudowa.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "Leach & Lang", "url": "https://www.leachandlang.pl"},
            {"company_name": "Nowodworski Estates", "url": "https://nowodworskiestates.pl"},
            {"company_name": "Atal SA", "url": "https://atal.pl"},
            {"company_name": "Ekoba Nieruchomości", "url": "https://www.ekoba.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Hotel Stary Kraków", "url": "https://stary.hotel.com.pl"},
            {"company_name": "Hotel Copernicus", "url": "https://copernicus.hotel.com.pl"},
            {"company_name": "Hotel Rubinstein", "url": "https://rubinstein.pl"},
            {"company_name": "Qubus Hotel Kraków", "url": "https://www.qubushotel.com"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Wawel SA", "url": "https://www.wawel.com.pl"},
            {"company_name": "Canpack Group", "url": "https://www.canpack.com"},
            {"company_name": "Tele-Fonika Kable", "url": "https://www.tfkable.com"},
            {"company_name": "Comarch Logistics", "url": "https://www.comarch.pl"},
        ],
    },
    "Wrocław": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria Olesiński i Wspólnicy", "url": "https://olesinski.com"},
            {"company_name": "Kancelaria SDZLEGAL Schindhelm", "url": "https://www.sdzlegal.pl"},
            {"company_name": "Kancelaria Prawna Chałas", "url": "https://www.chwp.pl"},
            {"company_name": "Kancelaria Dudkowiak", "url": "https://www.dudkowiak.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Klinika Ortognatyka Wrocław", "url": "https://www.ortognatyka.pl"},
            {"company_name": "Klinika Dental Salon", "url": "https://www.dentalsalon.pl"},
            {"company_name": "Centrum Medyczne Mediconcept", "url": "https://mediconcept.pl"},
            {"company_name": "Klinika Noa Clinic", "url": "https://noaclinic.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "Maćków Pracownia Projektowa", "url": "https://www.mackow.pl"},
            {"company_name": "Major Architekci", "url": "https://majorarchitekci.pl"},
            {"company_name": "Archicom", "url": "https://www.archicom.pl"},
            {"company_name": "KGHM Cuprum", "url": "https://www.cuprum.wroc.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "WGN Nieruchomości", "url": "https://www.wgn.pl"},
            {"company_name": "Vantage Development", "url": "https://vantage-sa.pl"},
            {"company_name": "Profit Development", "url": "https://www.profitdevelopment.com.pl"},
            {"company_name": "Jot-Be Nieruchomości", "url": "https://www.jot-be.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Hotel Monopol Wrocław", "url": "https://monopolwroclaw.hotel.com.pl"},
            {"company_name": "Art Hotel Wrocław", "url": "https://www.arthotel.pl"},
            {"company_name": "Hotel Jana Pawła II", "url": "https://www.hotel-jp2.pl"},
            {"company_name": "Hotel Europejski Wrocław", "url": "https://www.hoteleuropejski.pl"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Selena Group", "url": "https://www.selena.com"},
            {"company_name": "Rawlplug", "url": "https://www.rawlplug.com"},
            {"company_name": "Hasco-Lek", "url": "https://www.hasco-lek.pl"},
            {"company_name": "Wabco / ZF Wrocław", "url": "https://www.zf.com"},
        ],
    },
    "Poznań": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria Sójka Maciak Mataczyński", "url": "https://www.smmlegal.pl"},
            {"company_name": "Kancelaria PragmatIQ", "url": "https://pragmatiq.pl"},
            {"company_name": "Kancelaria WKB", "url": "https://wkb.pl"},
            {"company_name": "Kancelaria Piszcz i Wspólnicy", "url": "https://www.piszcz.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Rehasport Clinic", "url": "https://www.rehasport.pl"},
            {"company_name": "Candeo Clinic Poznań", "url": "https://www.candeo.pl"},
            {"company_name": "Centrum Medyczne Certus", "url": "https://www.certus.med.pl"},
            {"company_name": "Klinika Stomatologiczna Stankowscy", "url": "https://stankowscy.com.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "Ultra Architects", "url": "https://www.ultra-architects.pl"},
            {"company_name": "CDF Architekci", "url": "https://www.cdf.net.pl"},
            {"company_name": "Pekabex", "url": "https://www.pekabex.pl"},
            {"company_name": "PTB Nickel", "url": "https://www.ptbnickel.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "Nickel Development", "url": "https://www.nickel.com.pl"},
            {"company_name": "Monday Development", "url": "https://www.mondaydevelopment.pl"},
            {"company_name": "UWI Inwestycje", "url": "https://uwi.com.pl"},
            {"company_name": "Agencja Nieruchomości Haus", "url": "https://www.haus.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Hotel Bazar Poznański", "url": "https://www.bazarpoznanski.pl"},
            {"company_name": "Hotel Blow Up Hall 5050", "url": "https://www.blowuphall5050.com"},
            {"company_name": "Hotel Andersia", "url": "https://www.andersiahotel.pl"},
            {"company_name": "Hotel Moderno Poznań", "url": "https://www.hotelmoderno.pl"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Solaris Bus & Coach", "url": "https://www.solarisbus.com"},
            {"company_name": "Amica SA", "url": "https://www.amica.pl"},
            {"company_name": "Lisner", "url": "https://www.lisner.pl"},
            {"company_name": "Grupa MTP", "url": "https://www.mtp.pl"},
        ],
    },
    "Gdańsk": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria Głuchowski Siemiątkowski Zwara", "url": "https://www.kancelaria.eu"},
            {"company_name": "Kancelaria Prawnej Misiewicz Mosek", "url": "https://www.mmplegal.pl"},
            {"company_name": "Kancelaria Kawczyński Kieszkowski", "url": "https://www.kkadwokaci.pl"},
            {"company_name": "Kancelaria radców prawnych BGS", "url": "https://www.bgslegal.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Klinika Vivadental Gdańsk", "url": "https://www.vivadental.pl"},
            {"company_name": "Swissmed Centrum Zdrowia", "url": "https://www.swissmed.com.pl"},
            {"company_name": "Klinika Medyczna Eterna", "url": "https://www.eterna.med.pl"},
            {"company_name": "Prodent Gdańsk", "url": "https://www.prodent.gda.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "Roark Studio", "url": "https://www.roarkstudio.pl"},
            {"company_name": "Studio Kwadrat", "url": "https://www.studiokwadrat.pl"},
            {"company_name": "Allcon Budownictwo", "url": "https://www.allcon.pl"},
            {"company_name": "Doraco", "url": "https://www.doraco.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "Invest Komfort", "url": "https://www.investkomfort.pl"},
            {"company_name": "Inpro SA", "url": "https://www.inpro.com.pl"},
            {"company_name": "Hossa", "url": "https://www.hossa.gda.pl"},
            {"company_name": "Euro Styl", "url": "https://www.eurostyl.com.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Hotel Gdańsk Boutique", "url": "https://www.hotelgdansk.com.pl"},
            {"company_name": "Hotel Podewils Gdańsk", "url": "https://www.podewils.pl"},
            {"company_name": "Hotel Hanza Gdańsk", "url": "https://www.hotelhanza.pl"},
            {"company_name": "Hotel Grano Gdańsk", "url": "https://www.granohotels.pl"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Port Gdańsk", "url": "https://www.portgdansk.pl"},
            {"company_name": "Remontowa Holding", "url": "https://www.remontowa.com.pl"},
            {"company_name": "LPP Logistics", "url": "https://www.lpplogistics.com"},
            {"company_name": "Satel Gdańsk", "url": "https://www.satel.pl"},
        ],
    },
    "Katowice": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria Prawne Ślązak Zapiór", "url": "https://www.kancelaria-szip.pl"},
            {"company_name": "Kancelaria Krzyżagórska Łoboda", "url": "https://www.kll.pl"},
            {"company_name": "Kancelaria Duraj Reck", "url": "https://www.durajreck.pl"},
            {"company_name": "Kancelaria KSP Legal", "url": "https://www.ksplegal.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Klinika Weiss Klinik", "url": "https://www.weissklinik.pl"},
            {"company_name": "Klinika Dentim Clinic", "url": "https://www.dentim.pl"},
            {"company_name": "Klinika Silesia Med", "url": "https://www.silesiamed.pl"},
            {"company_name": "Centrum Medyczne Angelius", "url": "https://www.angelius.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "Medusa Group", "url": "https://www.medusagroup.pl"},
            {"company_name": "KWK Promes", "url": "https://www.kwkpromes.pl"},
            {"company_name": "Konior Studio", "url": "https://www.koniorstudio.pl"},
            {"company_name": "Tdj Estate", "url": "https://www.tdjestate.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "Opal Maksimum", "url": "https://www.opalmaksimum.pl"},
            {"company_name": "Millenium Inwestycje", "url": "https://www.millenium-inwestycje.pl"},
            {"company_name": "Activ Investment", "url": "https://www.activ-investment.eu"},
            {"company_name": "Domy Śląskie", "url": "https://www.domyslaskie.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Hotel Monopol Katowice", "url": "https://monopolkatowice.hotel.com.pl"},
            {"company_name": "Hotel Diament Plaza Katowice", "url": "https://www.hotelediament.pl"},
            {"company_name": "Hotel Angelo Katowice", "url": "https://www.viennahouse.com"},
            {"company_name": "Hotel Silesian Katowice", "url": "https://www.silesianhotel.pl"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Famur / Grenevia", "url": "https://www.grenevia.com"},
            {"company_name": "Stalprofil SA", "url": "https://www.stalprofil.com.pl"},
            {"company_name": "Cognor Holding", "url": "https://www.cognor.eu"},
            {"company_name": "Grupa Kęty", "url": "https://www.grupakety.com"},
        ],
    },
    "Łódź": {
        "Kancelarie Prawne/Podatkowe": [
            {"company_name": "Kancelaria Fortak & Karasiński", "url": "https://www.fklegal.pl"},
            {"company_name": "Kancelaria Mariański Group", "url": "https://www.marianskigroup.pl"},
            {"company_name": "Kancelaria Brzezińska Narolski", "url": "https://www.bnlegal.pl"},
            {"company_name": "Kancelaria Prawna Płaza Głąb", "url": "https://www.plazaglab.pl"},
        ],
        "Kliniki Medyczne/Stomatologia": [
            {"company_name": "Salve Medica Łódź", "url": "https://www.salvemedica.pl"},
            {"company_name": "Klinika Dermed Łódź", "url": "https://www.dermed.pl"},
            {"company_name": "Klinika Stomatologiczna Nawrocki", "url": "https://www.nawrockiclinic.com"},
            {"company_name": "Centrum Medyczne All-Med", "url": "https://www.all-med.pl"},
        ],
        "Architektura/Budownictwo": [
            {"company_name": "Design Lab Group", "url": "https://www.designlabgroup.pl"},
            {"company_name": "Reform Architekt", "url": "https://www.reformarchitekt.pl"},
            {"company_name": "Varitex Łódź", "url": "https://www.varitex.pl"},
            {"company_name": "Mosty Łódź", "url": "https://www.mostylodz.pl"},
        ],
        "Nieruchomości": [
            {"company_name": "Budomal Łódź", "url": "https://www.budomal.pl"},
            {"company_name": "Apro Investment", "url": "https://www.apro.com.pl"},
            {"company_name": "Tree Development Group", "url": "https://www.treedevelopment.pl"},
            {"company_name": "Vision Development", "url": "https://www.visiondevelopment.pl"},
        ],
        "Hotele B2B": [
            {"company_name": "Vienna House Andel's Łódź", "url": "https://www.andelslodz.com"},
            {"company_name": "Hotel Tobaco Łódź", "url": "https://www.hoteltobaco.pl"},
            {"company_name": "Hotel Stare Kino Łódź", "url": "https://www.cinemahotel.pl"},
            {"company_name": "Hotel Ambasador Premium", "url": "https://www.hotelambasador.pl"},
        ],
        "Produkcja/Logistyka": [
            {"company_name": "Pelion SA", "url": "https://www.pelion.eu"},
            {"company_name": "Ceramika Paradyż", "url": "https://www.paradyz.com"},
            {"company_name": "Tubądzin Group", "url": "https://www.tubadzin.pl"},
            {"company_name": "Atlas Group Łódź", "url": "https://www.atlas.com.pl"},
        ],
    },
}

# Domains to skip during harvesting (directories, social networks, aggregators)
SKIP_HARVEST_DOMAINS = {
    "facebook.com",
    "fb.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "google.com",
    "goo.gl",
    "maps.google.com",
    "booking.com",
    "airbnb.com",
    "tripadvisor.com",
    "znanylekarz.pl",
    "booksy.com",
    "panoramafirm.pl",
    "pkt.pl",
    "aleo.com",
    "oferteo.pl",
    "wikipedia.org",
}

# Junk / technical email domains to exclude during B2B extraction
JUNK_EMAIL_DOMAINS = {
    "example.com",
    "example.pl",
    "test.com",
    "test.pl",
    "sentry.io",
    "sentry.wixpress.com",
    "wixpress.com",
    "wix.com",
    "wordpress.org",
    "wordpress.com",
    "w3.org",
    "schema.org",
    "cloudflare.com",
    "googleapis.com",
    "google.com",
    "gstatic.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "gravatar.com",
    "jquery.com",
    "jsdelivr.net",
    "bootstrapcdn.com",
    "fontawesome.com",
    "domain.com",
    "yourdomain.com",
    "twojadomena.pl",
    "nazwafirmy.pl",
    "domena.pl",
    "email.com",
    "mail.com",
}

ASSET_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".mp4",
    ".webm",
)

NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36 SignalLeadPL/1.0"
)


# ---------------------------------------------------------------------------
# Helpers: URL normalization, Polish NIP validation, Contact cleaning
# ---------------------------------------------------------------------------


def normalize_url(raw_url: str) -> tuple[str, str]:
    """Return (canonical_url, clean_domain) for a user-supplied URL or bare domain."""
    url = raw_url.strip()
    if not url:
        raise ValueError("URL nie może być pusty.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().strip(".")
    if not host or "." not in host:
        raise ValueError(f"Nieprawidłowa domena w adresie: {raw_url}")
    clean_domain = host[4:] if host.startswith("www.") else host
    scheme = parsed.scheme.lower() if parsed.scheme in ("http", "https") else "https"
    canonical = f"{scheme}://{host}"
    return canonical, clean_domain


def validate_polish_nip(raw_nip: str) -> Optional[str]:
    """
    Validate a Polish 10-digit NIP using official checksum weights
    (6, 5, 7, 2, 3, 4, 5, 6, 7) modulo 11.
    Returns clean 10-digit NIP string if valid, otherwise None.
    """
    digits = re.sub(r"\D", "", raw_nip)
    if len(digits) != 10:
        return None
    # Reject trivial sequences (e.g. 0000000000 or 1111111111)
    if len(set(digits)) == 1:
        return None
    checksum = sum(int(digits[i]) * NIP_WEIGHTS[i] for i in range(9)) % 11
    if checksum == 10 or checksum != int(digits[9]):
        return None
    return digits


def extract_polish_nip(text: str) -> Optional[str]:
    """
    Extract and mathematically validate a Polish NIP from page text.
    Prioritizes candidates appearing next to 'NIP' label, then formatted patterns.
    """
    if not text:
        return None

    # 1. Candidates explicitly labeled with NIP
    labeled_pattern = re.compile(
        r"(?:NIP|Tax\s*ID|VAT\s*ID)\s*[:#\-]?\s*(?:PL\s*)?([0-9][0-9\-\s]{8,14}[0-9])",
        re.IGNORECASE,
    )
    for match in labeled_pattern.finditer(text):
        valid = validate_polish_nip(match.group(1))
        if valid:
            return valid

    # 2. Explicitly hyphenated Polish NIP formats (XXX-XXX-XX-XX or XXX-XX-XX-XXX)
    hyphen_pattern = re.compile(
        r"\b(?:PL)?(\d{3}-\d{3}-\d{2}-\d{2}|\d{3}-\d{2}-\d{2}-\d{3})\b"
    )
    for match in hyphen_pattern.finditer(text):
        valid = validate_polish_nip(match.group(1))
        if valid:
            return valid

    return None


def extract_emails(html_text: str, visible_text: str, domain: str) -> list[str]:
    """Extract clean B2B emails, filtering out technical/junk domains and assets."""
    candidates: set[str] = set()
    email_re = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,10}\b")

    for source in (html_text, visible_text):
        for found in email_re.findall(source):
            email = found.strip().lower().rstrip(".")
            if email.endswith(ASSET_EXTENSIONS):
                continue
            local, _, host = email.partition("@")
            if not local or not host or len(local) > 64 or len(email) > 120:
                continue
            if host in JUNK_EMAIL_DOMAINS or any(
                host.endswith(f".{junk}") for junk in JUNK_EMAIL_DOMAINS
            ):
                continue
            if any(
                token in local
                for token in ("noreply", "no-reply", "donotreply", "mailer-daemon", "u003e")
            ):
                continue
            candidates.add(email)

    preferred_prefixes = (
        "biuro",
        "kontakt",
        "kancelaria",
        "rejestracja",
        "recepcja",
        "sekretariat",
        "info",
        "zarzad",
        "sprzedaz",
        "marketing",
        "hello",
        "office",
    )

    def sort_key(e: str) -> tuple[int, int, str]:
        local, _, host = e.partition("@")
        same_domain = 0 if (host == domain or host.endswith(f".{domain}")) else 1
        prefix_rank = 0 if any(local.startswith(p) for p in preferred_prefixes) else 1
        return (same_domain, prefix_rank, e)

    return sorted(candidates, key=sort_key)[:5]


def extract_polish_phones(
    tel_links: list[str], visible_text: str, found_nip: Optional[str] = None
) -> list[str]:
    """Extract and normalize Polish phone numbers to '+48 XXX XXX XXX' format."""
    cleaned: list[str] = []
    seen: set[str] = set()

    def try_add(raw: str) -> None:
        digits = re.sub(r"\D", "", raw)
        if digits.startswith("0048"):
            digits = digits[4:]
        elif digits.startswith("48") and len(digits) == 11:
            digits = digits[2:]
        if len(digits) != 9:
            return
        # Valid Polish mobile/landline numbers never start with 0
        if digits[0] == "0":
            return
        # Avoid false positives like repeated digits or partial NIP
        if len(set(digits)) <= 2:
            return
        if found_nip and digits in found_nip:
            return
        formatted = f"+48 {digits[0:3]} {digits[3:6]} {digits[6:9]}"
        if formatted not in seen:
            seen.add(formatted)
            cleaned.append(formatted)

    # 1. Explicit tel: links first (highest confidence)
    for href in tel_links:
        try_add(href)

    # 2. Polish phone patterns in text (with +48 or standard 3-3-3 / 2-3-2-2 spacing)
    phone_patterns = [
        re.compile(r"(?:\+48|0048)[\s\-./()]*(\d[\d\s\-./()]{7,13}\d)"),
        re.compile(
            r"(?:tel\.?|telefon|kom\.?|infolinia|zadzwoń)\s*[:\-]?\s*(?:\+48\s*)?(\d{2,3}[\s\-]\d{2,3}[\s\-]\d{2,3}(?:[\s\-]\d{2})?)",
            re.IGNORECASE,
        ),
        re.compile(r"\b([5-8]\d{2}[\s\-]\d{3}[\s\-]\d{3})\b"),
    ]
    for pattern in phone_patterns:
        for match in pattern.finditer(visible_text):
            try_add(match.group(1))
            if len(cleaned) >= 4:
                return cleaned

    return cleaned[:4]


def extract_social_links(soup: BeautifulSoup) -> dict[str, str]:
    """Extract company Facebook, Instagram, and LinkedIn profile URLs."""
    socials: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        low = href.lower()
        if "sharer" in low or "share.php" in low or "intent/" in low:
            continue
        if "facebook.com/" in low and "facebook" not in socials:
            if not low.rstrip("/").endswith("facebook.com"):
                socials["facebook"] = href.split("?")[0]
        elif "instagram.com/" in low and "instagram" not in socials:
            if not low.rstrip("/").endswith("instagram.com"):
                socials["instagram"] = href.split("?")[0]
        elif "linkedin.com/" in low and "linkedin" not in socials:
            if "/company/" in low or "/in/" in low:
                socials["linkedin"] = href.split("?")[0]
    return socials


def detect_cms(html_raw: str, headers: dict[str, str], soup: BeautifulSoup) -> str:
    """Detect website CMS/framework with explicit support for Polish B2B platforms."""
    low = html_raw.lower()
    headers_low = {k.lower(): v.lower() for k, v in headers.items()}
    generator_meta = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
    generator = (generator_meta.get("content", "") if generator_meta else "").lower()

    if "woocommerce" in low or "wc-blocks" in low or "wp-content/plugins/woocommerce" in low:
        return "WooCommerce"
    if "wordpress" in generator or "wp-content/" in low or "wp-includes/" in low:
        return "WordPress"
    if "prestashop" in low or "/modules/ps_" in low or "prestashop" in generator:
        return "PrestaShop"
    if "shoper.pl" in low or "shoper" in generator or "/skins/user/" in low:
        return "Shoper"
    if "wixstatic.com" in low or "x-wix-request-id" in headers_low or "wix.com" in generator:
        return "Wix"
    if "joomla" in generator or "/media/jui/" in low or "/components/com_" in low:
        return "Joomla Legacy"
    if "__next_data__" in low or "/_next/static/" in low or "next-head-count" in low:
        return "Next.js"
    if "cdn.shopify.com" in low or "shopify.theme" in low:
        return "Shopify"
    if "webflow" in generator or "data-wf-site" in low:
        return "Webflow"
    if "drupal" in generator or "/sites/default/files" in low:
        return "Drupal"
    return "Custom / HTML"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class SignalItem:
    category: str  # "Web Dev" | "SEO" | "Ads / Marketing"
    code: str
    label: str
    severity: str  # "critical" | "high" | "medium"
    points: int


@dataclass
class LeadRecord:
    domain: str
    url: str
    company_name: str
    city: str
    industry: str
    http_status: int
    has_ssl: bool
    ttfb_ms: int
    has_viewport: bool
    cms: str
    has_meta_description: bool
    h1_count: int
    has_schema: bool
    has_gtm: bool
    has_ga4: bool
    has_meta_pixel: bool
    has_tiktok_pixel: bool
    nip: Optional[str]
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)
    signals: list[dict[str, Any]] = field(default_factory=list)
    lead_score: int = 0
    buyer_persona: str = "Agencja Digital Marketing"
    lead_price_pln: int = 59
    estimated_agency_contract_pln: int = 5000
    icebreaker: str = ""
    audit_status: str = "ok"
    audited_subpages: list[str] = field(default_factory=list)
    audited_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Agent 1: HarvesterAgent
# ---------------------------------------------------------------------------


class HarvesterAgent:
    """Discovers Polish B2B domains by city and industry via OSM Overpass + curated fallback."""

    OVERPASS_ENDPOINTS = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    )

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    async def harvest(
        self, city: str, industry: str, limit: int = 8
    ) -> list[dict[str, str]]:
        """
        Return up to `limit` target dicts: [{"company_name": ..., "url": ..., "source": ...}].
        Queries OpenStreetMap Overpass API first, then fills remaining slots from curated Polish pool.
        """
        if city not in CITIES:
            city = "Warszawa"
        if industry not in INDUSTRY_CONFIG:
            industry = INDUSTRIES[0]

        results: list[dict[str, str]] = []
        seen_domains: set[str] = set()

        # 1. Try OpenStreetMap Overpass API
        overpass_targets = await self._query_overpass(city, industry, limit=limit * 2)
        for item in overpass_targets:
            try:
                canonical, domain = normalize_url(item["url"])
            except ValueError:
                continue
            if domain in seen_domains or any(
                domain == skip or domain.endswith(f".{skip}")
                for skip in SKIP_HARVEST_DOMAINS
            ):
                continue
            seen_domains.add(domain)
            results.append(
                {
                    "company_name": item.get("company_name") or domain,
                    "url": canonical,
                    "domain": domain,
                    "city": city,
                    "industry": industry,
                    "source": "osm_overpass",
                }
            )
            if len(results) >= limit:
                return results

        # 2. Top up from curated Polish B2B fallback pool
        fallback_list = FALLBACK_DOMAINS.get(city, {}).get(industry, [])
        for item in fallback_list:
            try:
                canonical, domain = normalize_url(item["url"])
            except ValueError:
                continue
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            results.append(
                {
                    "company_name": item["company_name"],
                    "url": canonical,
                    "domain": domain,
                    "city": city,
                    "industry": industry,
                    "source": "curated_pl_pool",
                }
            )
            if len(results) >= limit:
                break

        return results

    async def _query_overpass(
        self, city: str, industry: str, limit: int = 16
    ) -> list[dict[str, str]]:
        south, west, north, east = CITIES[city]
        bbox = f"{south},{west},{north},{east}"
        osm_filters = INDUSTRY_CONFIG[industry]["osm_filters"]

        union_parts = []
        for f in osm_filters:
            union_parts.append(f'node{f}["website"]({bbox});')
            union_parts.append(f'way{f}["website"]({bbox});')
            union_parts.append(f'node{f}["contact:website"]({bbox});')

        query = (
            f"[out:json][timeout:10];"
            f"({''.join(union_parts)});"
            f"out tags {limit};"
        )

        async with httpx.AsyncClient(
            timeout=self.timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            for endpoint in self.OVERPASS_ENDPOINTS:
                try:
                    resp = await client.post(endpoint, data={"data": query})
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    elements = data.get("elements", [])
                    found: list[dict[str, str]] = []
                    for el in elements:
                        tags = el.get("tags", {})
                        raw_url = tags.get("website") or tags.get("contact:website")
                        if not raw_url:
                            continue
                        name = tags.get("name") or tags.get("brand") or ""
                        found.append({"company_name": name, "url": raw_url})
                    if found:
                        return found
                except Exception as exc:
                    logger.debug("Overpass endpoint %s failed: %s", endpoint, exc)
                    continue
        return []


# ---------------------------------------------------------------------------
# Agent 2 & 3: SignalAuditorAgent
# ---------------------------------------------------------------------------


class SignalAuditorAgent:
    """
    Performs live Web Dev, SEO, and Ads/Marketing signal audits on Polish B2B websites,
    extracts contact data + validated NIP from homepage & subpages, scores and prices the lead,
    and crafts a personalized Polish cold-email icebreaker.
    """

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    async def audit(
        self,
        raw_url: str,
        city: str = "Polska",
        industry: str = "B2B",
        hint_company_name: Optional[str] = None,
    ) -> LeadRecord:
        canonical_url, domain = normalize_url(raw_url)
        audited_subpages: list[str] = []

        # Fetch homepage (try HTTPS first, fallback to HTTP if SSL fails)
        homepage_resp, final_url, ttfb_ms, has_ssl, ssl_error = await self._fetch_homepage(
            canonical_url, domain
        )

        if homepage_resp is None:
            # Site unreachable / broken — still valuable diagnostic record if DNS resolves or server fails
            signals = [
                SignalItem(
                    category="Web Dev",
                    code="site_unreachable",
                    label="Strona niedostępna lub błąd połączenia SSL/serwera",
                    severity="critical",
                    points=30,
                )
            ]
            lead_score = 45
            return LeadRecord(
                domain=domain,
                url=canonical_url,
                company_name=hint_company_name or domain,
                city=city,
                industry=industry,
                http_status=0,
                has_ssl=False,
                ttfb_ms=ttfb_ms,
                has_viewport=False,
                cms="Nieznany",
                has_meta_description=False,
                h1_count=0,
                has_schema=False,
                has_gtm=False,
                has_ga4=False,
                has_meta_pixel=False,
                has_tiktok_pixel=False,
                nip=None,
                emails=[],
                phones=[],
                social_links={},
                signals=[asdict(s) for s in signals],
                lead_score=lead_score,
                buyer_persona="Software House / Agencja Web Dev",
                lead_price_pln=59,
                estimated_agency_contract_pln=9000,
                icebreaker=(
                    f"Dzień dobry, próbowałem wejść na stronę {domain} i niestety "
                    f"przeglądarka zwraca błąd połączenia/SSL ({ssl_error or 'timeout'}). "
                    f"Klienci trafiający z Google widzą dziś błąd zamiast Państwa oferty — "
                    f"mogę podesłać krótką diagnozę techniczną?"
                ),
                audit_status=f"error: {ssl_error or 'unreachable'}",
                audited_subpages=[],
            )

        html_home = homepage_resp.text or ""
        headers_home = dict(homepage_resp.headers)
        soup_home = BeautifulSoup(html_home, "lxml")

        # Determine subpages to inspect for NIP, emails, phones (/kontakt, /o-nas + discovered)
        subpage_urls = self._discover_subpages(final_url, domain, soup_home)
        subpage_htmls = await self._fetch_subpages(subpage_urls)
        audited_subpages = list(subpage_htmls.keys())

        # Combine text for B2B contact & NIP extraction
        all_html_chunks = [html_home] + list(subpage_htmls.values())
        all_soups = [soup_home] + [
            BeautifulSoup(chunk, "lxml") for chunk in subpage_htmls.values()
        ]
        combined_html = "\n".join(all_html_chunks)
        combined_visible_text = "\n".join(
            s.get_text(separator=" ", strip=True) for s in all_soups
        )
        tel_links = [
            a["href"]
            for s in all_soups
            for a in s.find_all("a", href=True)
            if a["href"].strip().lower().startswith("tel:")
        ]

        # Extract company name
        company_name = self._extract_company_name(
            soup_home, domain, hint_company_name
        )

        # --- Technical / Web Dev analysis ---
        cms = detect_cms(html_home, headers_home, soup_home)
        viewport_tag = soup_home.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
        has_viewport = bool(viewport_tag and viewport_tag.get("content", "").strip())

        # --- SEO analysis ---
        meta_desc_tag = soup_home.find(
            "meta", attrs={"name": re.compile(r"^description$", re.I)}
        )
        meta_desc_content = (
            meta_desc_tag.get("content", "").strip() if meta_desc_tag else ""
        )
        has_meta_description = len(meta_desc_content) >= 20

        h1_tags = [
            h.get_text(strip=True)
            for h in soup_home.find_all("h1")
            if h.get_text(strip=True)
        ]
        h1_count = len(h1_tags)

        schema_scripts = soup_home.find_all(
            "script", attrs={"type": re.compile(r"application/ld\+json", re.I)}
        )
        has_schema = bool(schema_scripts)

        # --- Ads / Marketing tracking analysis (check homepage + scripts) ---
        low_home = html_home.lower()
        has_gtm = bool(
            "googletagmanager.com/gtm.js" in low_home
            or re.search(r"\bgtm-[a-z0-9]{5,}\b", html_home, re.I)
        )
        has_ga4 = bool(
            "googletagmanager.com/gtag/js" in low_home
            or re.search(r"['\"]G-[A-Z0-9]{7,12}['\"]", html_home)
            or "gtag('config'" in low_home
            or 'gtag("config"' in low_home
        )
        has_meta_pixel = bool(
            "connect.facebook.net" in low_home
            and "fbevents.js" in low_home
            or "fbq('init'" in low_home
            or 'fbq("init"' in low_home
            or "facebook.com/tr?id=" in low_home
        )
        has_tiktok_pixel = bool(
            "analytics.tiktok.com" in low_home or "ttq.load" in low_home
        )

        # --- B2B Contact & NIP extraction ---
        nip = extract_polish_nip(combined_visible_text) or extract_polish_nip(combined_html)
        emails = extract_emails(combined_html, combined_visible_text, domain)
        phones = extract_polish_phones(tel_links, combined_visible_text, found_nip=nip)
        social_links = extract_social_links(soup_home)
        for s in all_soups[1:]:
            for k, v in extract_social_links(s).items():
                social_links.setdefault(k, v)

        # --- Assemble Signals & Scoring ---
        signals: list[SignalItem] = []

        # Web Dev Signals
        if not has_ssl:
            signals.append(
                SignalItem(
                    category="Web Dev",
                    code="no_ssl",
                    label="Brak certyfikatu SSL / przekierowania na HTTPS",
                    severity="critical",
                    points=24,
                )
            )
        if ttfb_ms > 1400:
            severity = "critical" if ttfb_ms > 2500 else "high"
            pts = 18 if ttfb_ms > 2500 else 13
            signals.append(
                SignalItem(
                    category="Web Dev",
                    code="slow_ttfb",
                    label=f"Wolny czas odpowiedzi serwera (TTFB {ttfb_ms} ms > 1400 ms)",
                    severity=severity,
                    points=pts,
                )
            )
        if not has_viewport:
            signals.append(
                SignalItem(
                    category="Web Dev",
                    code="no_viewport",
                    label="Brak responsywności mobilnej (<meta name='viewport'>)",
                    severity="critical",
                    points=18,
                )
            )
        if cms in ("Joomla Legacy", "Wix"):
            signals.append(
                SignalItem(
                    category="Web Dev",
                    code="legacy_cms",
                    label=f"Ograniczony lub przestarzały silnik strony ({cms})",
                    severity="high",
                    points=12,
                )
            )

        # SEO Signals
        if not has_meta_description:
            signals.append(
                SignalItem(
                    category="SEO",
                    code="missing_meta_description",
                    label="Brak lub pusty tag <meta name='description'>",
                    severity="high",
                    points=11,
                )
            )
        if h1_count == 0:
            signals.append(
                SignalItem(
                    category="SEO",
                    code="missing_h1",
                    label="Brak nagłówka <h1> na stronie głównej",
                    severity="high",
                    points=11,
                )
            )
        elif h1_count > 1:
            signals.append(
                SignalItem(
                    category="SEO",
                    code="multiple_h1",
                    label=f"Wielokrotne nagłówki <h1> ({h1_count} szt. — rozmyta struktura SEO)",
                    severity="medium",
                    points=8,
                )
            )
        if not has_schema:
            signals.append(
                SignalItem(
                    category="SEO",
                    code="missing_schema",
                    label="Brak danych strukturalnych Schema.org (JSON-LD)",
                    severity="high",
                    points=11,
                )
            )

        # Ads / Marketing Signals
        # Note: When GTM is present in static HTML, GA4 and Meta Pixel are frequently
        # injected dynamically via GTM JS container — suppress false-positive claims!
        if not has_gtm:
            signals.append(
                SignalItem(
                    category="Ads / Marketing",
                    code="missing_gtm",
                    label="Brak Google Tag Manager (GTM)",
                    severity="medium",
                    points=9,
                )
            )
            if not has_ga4:
                signals.append(
                    SignalItem(
                        category="Ads / Marketing",
                        code="missing_ga4",
                        label="Brak analityki Google Analytics 4 (GA4)",
                        severity="high",
                        points=12,
                    )
                )
            if not has_meta_pixel:
                signals.append(
                    SignalItem(
                        category="Ads / Marketing",
                        code="missing_meta_pixel",
                        label="Brak Meta Pixel (utracony remarketing Facebook/Instagram Ads)",
                        severity="high",
                        points=10,
                    )
                )
        if not has_tiktok_pixel and not has_gtm:
            signals.append(
                SignalItem(
                    category="Ads / Marketing",
                    code="missing_tiktok_pixel",
                    label="Brak TikTok Pixel (niewykorzystany kanał zasięgowy)",
                    severity="medium",
                    points=5,
                )
            )

        # Category point sums
        web_pts = sum(s.points for s in signals if s.category == "Web Dev")
        seo_pts = sum(s.points for s in signals if s.category == "SEO")
        ads_pts = sum(s.points for s in signals if s.category == "Ads / Marketing")

        # Contactability bonus (makes the lead ready to pitch immediately)
        contact_bonus = 0
        if emails:
            contact_bonus += 6
        if phones:
            contact_bonus += 4
        if nip:
            contact_bonus += 5

        raw_score = web_pts + seo_pts + ads_pts + contact_bonus
        lead_score = max(10, min(99, raw_score))

        # Buyer Persona classification
        buyer_persona = self._classify_buyer(
            web_pts=web_pts,
            seo_pts=seo_pts,
            ads_pts=ads_pts,
            lead_score=lead_score,
            signals=signals,
        )

        # Lead pricing tier (59 / 99 / 149 PLN)
        if lead_score >= 75:
            lead_price_pln = 149
        elif lead_score >= 55:
            lead_price_pln = 99
        else:
            lead_price_pln = 59

        # Estimated agency contract value (PLN)
        estimated_contract = self._estimate_contract_value(
            industry=industry,
            web_pts=web_pts,
            seo_pts=seo_pts,
            ads_pts=ads_pts,
            cms=cms,
        )

        # Personalized Polish B2B Icebreaker
        icebreaker = self._generate_icebreaker(
            company_name=company_name,
            domain=domain,
            city=city,
            buyer_persona=buyer_persona,
            signals=signals,
            ttfb_ms=ttfb_ms,
        )

        return LeadRecord(
            domain=domain,
            url=final_url,
            company_name=company_name,
            city=city,
            industry=industry,
            http_status=homepage_resp.status_code,
            has_ssl=has_ssl,
            ttfb_ms=ttfb_ms,
            has_viewport=has_viewport,
            cms=cms,
            has_meta_description=has_meta_description,
            h1_count=h1_count,
            has_schema=has_schema,
            has_gtm=has_gtm,
            has_ga4=has_ga4,
            has_meta_pixel=has_meta_pixel,
            has_tiktok_pixel=has_tiktok_pixel,
            nip=nip,
            emails=emails,
            phones=phones,
            social_links=social_links,
            signals=[asdict(s) for s in signals],
            lead_score=lead_score,
            buyer_persona=buyer_persona,
            lead_price_pln=lead_price_pln,
            estimated_agency_contract_pln=estimated_contract,
            icebreaker=icebreaker,
            audit_status="ok",
            audited_subpages=audited_subpages,
        )

    async def _fetch_homepage(
        self, canonical_url: str, domain: str
    ) -> tuple[Optional[httpx.Response], str, int, bool, Optional[str]]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        https_url = f"https://{domain}" if not canonical_url.startswith("https://") else canonical_url

        # 1. Try HTTPS with strict SSL verification
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                verify=True,
                headers=headers,
            ) as client:
                resp = await client.get(https_url)
                ttfb_ms = int((time.perf_counter() - t0) * 1000)
                final_url = str(resp.url)
                has_ssl = final_url.startswith("https://")
                return resp, final_url, ttfb_ms, has_ssl, None
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException) as ssl_exc:
            ssl_err_msg = type(ssl_exc).__name__

        # 2. Fallback to HTTP (signals missing/broken SSL!)
        http_url = f"http://{domain}"
        t1 = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                verify=False,
                headers=headers,
            ) as client:
                resp = await client.get(http_url)
                ttfb_ms = int((time.perf_counter() - t1) * 1000)
                final_url = str(resp.url)
                # Even if redirected to https with invalid cert, SSL failed strict check
                return resp, final_url, ttfb_ms, False, ssl_err_msg
        except Exception as exc:
            ttfb_ms = int((time.perf_counter() - t1) * 1000)
            return None, canonical_url, ttfb_ms, False, f"{type(exc).__name__}"

    def _discover_subpages(
        self, base_url: str, domain: str, soup: BeautifulSoup
    ) -> list[str]:
        """Collect /kontakt, /o-nas and discovered Polish contact/about/privacy links."""
        parsed_base = urlparse(base_url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        candidates: list[str] = [
            f"{origin}/kontakt",
            f"{origin}/o-nas",
        ]
        seen: set[str] = set(candidates)

        keywords = (
            "kontakt",
            "contact",
            "o-nas",
            "o-firmie",
            "polityka-prywatnosci",
            "rodo",
            "dane-firmy",
        )
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
            if any(kw in path_low for kw in keywords) and full not in seen:
                seen.add(full)
                candidates.append(full)
                if len(candidates) >= 4:
                    break

        return candidates[:4]

    async def _fetch_subpages(self, urls: list[str]) -> dict[str, str]:
        """Fetch key subpages concurrently with short timeouts."""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
        }
        results: dict[str, str] = {}

        async def fetch_one(client: httpx.AsyncClient, url: str) -> None:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and "text/html" in resp.headers.get(
                    "content-type", ""
                ):
                    results[url] = resp.text[:350_000]
            except Exception:
                return

        async with httpx.AsyncClient(
            timeout=6.0, follow_redirects=True, verify=False, headers=headers
        ) as client:
            await asyncio.gather(*(fetch_one(client, u) for u in urls))

        return results

    def _extract_company_name(
        self, soup: BeautifulSoup, domain: str, hint: Optional[str]
    ) -> str:
        if hint and hint.strip() and hint.strip().lower() != domain.lower():
            return hint.strip()

        og_site = soup.find("meta", attrs={"property": "og:site_name"})
        if og_site and og_site.get("content", "").strip():
            name = og_site["content"].strip()
            if 2 <= len(name) <= 80:
                return name

        # Try JSON-LD Organization name
        for script in soup.find_all(
            "script", attrs={"type": re.compile(r"application/ld\+json", re.I)}
        ):
            try:
                payload = json.loads(script.string or "")
                nodes = payload if isinstance(payload, list) else [payload]
                for node in nodes:
                    if isinstance(node, dict):
                        if "@graph" in node and isinstance(node["@graph"], list):
                            nodes.extend(node["@graph"])
                        ntype = str(node.get("@type", "")).lower()
                        if any(
                            t in ntype
                            for t in ("organization", "localbusiness", "corporation", "hotel", "dentist", "legalservice")
                        ):
                            n = str(node.get("name", "")).strip()
                            if 2 <= len(n) <= 80:
                                return n
            except Exception:
                continue

        if soup.title and soup.title.string:
            raw_title = soup.title.string.strip()
            # Split on common separators and pick the brand segment
            parts = re.split(r"\s*[\|\-–—•»]\s*", raw_title)
            candidate = parts[0].strip() if parts else raw_title
            if 2 <= len(candidate) <= 70:
                return candidate

        return domain.split(".")[0].replace("-", " ").title()

    def _classify_buyer(
        self,
        web_pts: int,
        seo_pts: int,
        ads_pts: int,
        lead_score: int,
        signals: list[SignalItem],
    ) -> str:
        seo_count = sum(1 for s in signals if s.category == "SEO")
        ads_count = sum(1 for s in signals if s.category == "Ads / Marketing")

        if web_pts >= 22 and web_pts >= seo_pts and web_pts >= ads_pts:
            return "Software House / Agencja Web Dev"
        if lead_score >= 68 and seo_count >= 2 and ads_count >= 2:
            return "Agencja Performance & SEO 360"
        if seo_pts >= ads_pts and seo_count >= 2:
            return "Agencja SEO / SEM"
        return "Agencja Digital Marketing"

    def _estimate_contract_value(
        self,
        industry: str,
        web_pts: int,
        seo_pts: int,
        ads_pts: int,
        cms: str,
    ) -> int:
        base = INDUSTRY_CONFIG.get(industry, {}).get("base_contract_pln", 9000)
        scope_value = 0
        if web_pts >= 20:
            scope_value += 10000
        elif web_pts >= 10:
            scope_value += 4500

        if seo_pts >= 20:
            scope_value += 8000
        elif seo_pts >= 10:
            scope_value += 4000

        if ads_pts >= 20:
            scope_value += 6500
        elif ads_pts >= 10:
            scope_value += 3000

        if cms in ("WooCommerce", "PrestaShop", "Shoper"):
            scope_value += 3500

        total = int((base * 0.45) + scope_value)
        # Round nicely to nearest 500 PLN
        return max(3500, int(round(total / 500.0) * 500))

    def _generate_icebreaker(
        self,
        company_name: str,
        domain: str,
        city: str,
        buyer_persona: str,
        signals: list[SignalItem],
        ttfb_ms: int,
    ) -> str:
        from human_copy import build_human_icebreaker

        return build_human_icebreaker(
            company_name=company_name,
            domain=domain,
            city=city,
            signals=signals,
            ttfb_ms=ttfb_ms,
        )


# ---------------------------------------------------------------------------
# Teaser / Demo Masking
# ---------------------------------------------------------------------------


def _mask_domain(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) < 2:
        return domain[:2] + "*****"
    stem = parts[0]
    tld = ".".join(parts[1:])
    visible = stem[:2] if len(stem) >= 3 else stem[:1]
    return f"{visible}*****.{tld}"


def _mask_company_name(name: str) -> str:
    words = name.split()
    if not words:
        return "Firma *****"
    masked_words = []
    for i, w in enumerate(words):
        if i == 0:
            prefix = w[:2] if len(w) >= 3 else w[:1]
            masked_words.append(f"{prefix}*****")
        elif w.lower() in ("sp.", "z", "o.o.", "s.a.", "sa", "s.k.", "sp.k."):
            masked_words.append(w)
        else:
            masked_words.append("****")
    return " ".join(masked_words)


def _mask_nip(nip: Optional[str]) -> Optional[str]:
    if not nip or len(nip) < 10:
        return None
    return f"{nip[:3]}*****{nip[-2:]}"


def _mask_email(email: str) -> str:
    local, _, host = email.partition("@")
    tld = host.split(".")[-1] if "." in host else "pl"
    prefix = local[:1] if local else "k"
    return f"{prefix}.***@***.{tld}"


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 9:
        tail = digits[-2:]
        first = digits[-9]
        return f"+48 {first}** *** *{tail}"
    return "+48 *** *** ***"


def mask_lead_for_teaser(lead: dict[str, Any]) -> dict[str, Any]:
    """
    Create a Demo/Teaser copy of a lead record:
    masks company identity, domain, NIP, emails, phones, and URLs,
    while keeping 100% of diagnostic signals, score, contract value, and pricing.
    """
    masked = dict(lead)
    orig_domain = lead.get("domain", "")
    orig_name = lead.get("company_name", "")
    masked_domain = _mask_domain(orig_domain)
    masked_name = _mask_company_name(orig_name)

    masked["domain"] = masked_domain
    masked["url"] = f"https://{masked_domain}"
    masked["company_name"] = masked_name
    masked["nip"] = _mask_nip(lead.get("nip"))
    masked["emails"] = [_mask_email(e) for e in lead.get("emails", [])]
    masked["phones"] = [_mask_phone(p) for p in lead.get("phones", [])]
    masked["social_links"] = {
        k: "[odblokowane po zakupie paczki]"
        for k in lead.get("social_links", {}).keys()
    }
    masked["audited_subpages"] = [
        "[ukryte w trybie Demo]" for _ in lead.get("audited_subpages", [])
    ]

    ice = lead.get("icebreaker", "")
    if orig_domain:
        ice = ice.replace(orig_domain, masked_domain)
    if orig_name:
        ice = ice.replace(orig_name, masked_name)
    masked["icebreaker"] = ice
    masked["is_teaser"] = True
    return masked


# ---------------------------------------------------------------------------
# Agent 4: LeadDatabase (SQLite + CSV Export + KPIs)
# ---------------------------------------------------------------------------


class LeadDatabase:
    """SQLite persistence layer for SignalLead PL."""

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
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT UNIQUE NOT NULL,
                    url TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    city TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    http_status INTEGER NOT NULL DEFAULT 0,
                    has_ssl INTEGER NOT NULL DEFAULT 0,
                    ttfb_ms INTEGER NOT NULL DEFAULT 0,
                    has_viewport INTEGER NOT NULL DEFAULT 0,
                    cms TEXT NOT NULL DEFAULT 'Custom / HTML',
                    has_meta_description INTEGER NOT NULL DEFAULT 0,
                    h1_count INTEGER NOT NULL DEFAULT 0,
                    has_schema INTEGER NOT NULL DEFAULT 0,
                    has_gtm INTEGER NOT NULL DEFAULT 0,
                    has_ga4 INTEGER NOT NULL DEFAULT 0,
                    has_meta_pixel INTEGER NOT NULL DEFAULT 0,
                    has_tiktok_pixel INTEGER NOT NULL DEFAULT 0,
                    nip TEXT,
                    emails_json TEXT NOT NULL DEFAULT '[]',
                    phones_json TEXT NOT NULL DEFAULT '[]',
                    social_links_json TEXT NOT NULL DEFAULT '{}',
                    signals_json TEXT NOT NULL DEFAULT '[]',
                    lead_score INTEGER NOT NULL DEFAULT 0,
                    buyer_persona TEXT NOT NULL,
                    lead_price_pln INTEGER NOT NULL DEFAULT 59,
                    estimated_agency_contract_pln INTEGER NOT NULL DEFAULT 5000,
                    icebreaker TEXT NOT NULL DEFAULT '',
                    audit_status TEXT NOT NULL DEFAULT 'ok',
                    audited_subpages_json TEXT NOT NULL DEFAULT '[]',
                    audited_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_score ON leads (lead_score DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_city ON leads (city)"
            )
            conn.commit()

    def upsert_lead(self, lead: LeadRecord) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO leads (
                    domain, url, company_name, city, industry,
                    http_status, has_ssl, ttfb_ms, has_viewport, cms,
                    has_meta_description, h1_count, has_schema,
                    has_gtm, has_ga4, has_meta_pixel, has_tiktok_pixel,
                    nip, emails_json, phones_json, social_links_json,
                    signals_json, lead_score, buyer_persona,
                    lead_price_pln, estimated_agency_contract_pln,
                    icebreaker, audit_status, audited_subpages_json, audited_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?
                )
                ON CONFLICT(domain) DO UPDATE SET
                    url=excluded.url,
                    company_name=excluded.company_name,
                    city=excluded.city,
                    industry=excluded.industry,
                    http_status=excluded.http_status,
                    has_ssl=excluded.has_ssl,
                    ttfb_ms=excluded.ttfb_ms,
                    has_viewport=excluded.has_viewport,
                    cms=excluded.cms,
                    has_meta_description=excluded.has_meta_description,
                    h1_count=excluded.h1_count,
                    has_schema=excluded.has_schema,
                    has_gtm=excluded.has_gtm,
                    has_ga4=excluded.has_ga4,
                    has_meta_pixel=excluded.has_meta_pixel,
                    has_tiktok_pixel=excluded.has_tiktok_pixel,
                    nip=excluded.nip,
                    emails_json=excluded.emails_json,
                    phones_json=excluded.phones_json,
                    social_links_json=excluded.social_links_json,
                    signals_json=excluded.signals_json,
                    lead_score=excluded.lead_score,
                    buyer_persona=excluded.buyer_persona,
                    lead_price_pln=excluded.lead_price_pln,
                    estimated_agency_contract_pln=excluded.estimated_agency_contract_pln,
                    icebreaker=excluded.icebreaker,
                    audit_status=excluded.audit_status,
                    audited_subpages_json=excluded.audited_subpages_json,
                    audited_at=excluded.audited_at
                """,
                (
                    lead.domain,
                    lead.url,
                    lead.company_name,
                    lead.city,
                    lead.industry,
                    lead.http_status,
                    int(lead.has_ssl),
                    lead.ttfb_ms,
                    int(lead.has_viewport),
                    lead.cms,
                    int(lead.has_meta_description),
                    lead.h1_count,
                    int(lead.has_schema),
                    int(lead.has_gtm),
                    int(lead.has_ga4),
                    int(lead.has_meta_pixel),
                    int(lead.has_tiktok_pixel),
                    lead.nip,
                    json.dumps(lead.emails, ensure_ascii=False),
                    json.dumps(lead.phones, ensure_ascii=False),
                    json.dumps(lead.social_links, ensure_ascii=False),
                    json.dumps(lead.signals, ensure_ascii=False),
                    lead.lead_score,
                    lead.buyer_persona,
                    lead.lead_price_pln,
                    lead.estimated_agency_contract_pln,
                    lead.icebreaker,
                    lead.audit_status,
                    json.dumps(lead.audited_subpages, ensure_ascii=False),
                    lead.audited_at,
                ),
            )
            conn.commit()
        return lead.to_dict()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "domain": row["domain"],
            "url": row["url"],
            "company_name": row["company_name"],
            "city": row["city"],
            "industry": row["industry"],
            "http_status": row["http_status"],
            "has_ssl": bool(row["has_ssl"]),
            "ttfb_ms": row["ttfb_ms"],
            "has_viewport": bool(row["has_viewport"]),
            "cms": row["cms"],
            "has_meta_description": bool(row["has_meta_description"]),
            "h1_count": row["h1_count"],
            "has_schema": bool(row["has_schema"]),
            "has_gtm": bool(row["has_gtm"]),
            "has_ga4": bool(row["has_ga4"]),
            "has_meta_pixel": bool(row["has_meta_pixel"]),
            "has_tiktok_pixel": bool(row["has_tiktok_pixel"]),
            "nip": row["nip"],
            "emails": json.loads(row["emails_json"] or "[]"),
            "phones": json.loads(row["phones_json"] or "[]"),
            "social_links": json.loads(row["social_links_json"] or "{}"),
            "signals": json.loads(row["signals_json"] or "[]"),
            "lead_score": row["lead_score"],
            "buyer_persona": row["buyer_persona"],
            "lead_price_pln": row["lead_price_pln"],
            "estimated_agency_contract_pln": row["estimated_agency_contract_pln"],
            "icebreaker": row["icebreaker"],
            "audit_status": row["audit_status"],
            "audited_subpages": json.loads(row["audited_subpages_json"] or "[]"),
            "audited_at": row["audited_at"],
            "is_teaser": False,
        }

    def list_leads(
        self,
        min_score: int = 0,
        city: Optional[str] = None,
        industry: Optional[str] = None,
        buyer_persona: Optional[str] = None,
        search: Optional[str] = None,
        teaser: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["lead_score >= ?"]
        params: list[Any] = [min_score]

        if city and city != "Wszystkie":
            clauses.append("city = ?")
            params.append(city)
        if industry and industry != "Wszystkie":
            clauses.append("industry = ?")
            params.append(industry)
        if buyer_persona and buyer_persona != "Wszystkie":
            clauses.append("buyer_persona = ?")
            params.append(buyer_persona)
        if search and search.strip():
            like = f"%{search.strip()}%"
            clauses.append(
                "(domain LIKE ? OR company_name LIKE ? OR city LIKE ? OR industry LIKE ? OR cms LIKE ?)"
            )
            params.extend([like, like, like, like, like])

        sql = f"SELECT * FROM leads WHERE {' AND '.join(clauses)} ORDER BY lead_score DESC, audited_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        leads = [self._row_to_dict(r) for r in rows]
        if teaser:
            return [mask_lead_for_teaser(lead) for lead in leads]
        return leads

    def delete_lead(self, domain: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM leads WHERE domain = ?", (domain,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def compute_kpis(leads: list[dict[str, Any]]) -> dict[str, Any]:
        total_leads = len(leads)
        hot_leads = sum(1 for lead in leads if lead.get("lead_score", 0) >= 75)
        pack_value_pln = sum(lead.get("lead_price_pln", 0) for lead in leads)
        bundle_price_pln = int(round(pack_value_pln * 0.75))
        total_pipeline_pln = sum(
            lead.get("estimated_agency_contract_pln", 0) for lead in leads
        )
        with_nip = sum(1 for lead in leads if lead.get("nip"))
        with_email = sum(1 for lead in leads if lead.get("emails"))
        avg_score = (
            round(sum(lead.get("lead_score", 0) for lead in leads) / total_leads, 1)
            if total_leads
            else 0.0
        )
        return {
            "total_leads": total_leads,
            "hot_leads_75_plus": hot_leads,
            "pack_value_pln": pack_value_pln,
            "bundle_price_pln": bundle_price_pln,
            "bundle_savings_pln": pack_value_pln - bundle_price_pln,
            "total_contract_pipeline_pln": total_pipeline_pln,
            "with_nip_count": with_nip,
            "with_email_count": with_email,
            "avg_score": avg_score,
        }

    @staticmethod
    def export_csv(leads: list[dict[str, Any]], teaser: bool = False) -> str:
        """Export leads to UTF-8-SIG CSV ready for Polish Excel."""
        rows = [
            mask_lead_for_teaser(l) if (teaser and not l.get("is_teaser")) else l
            for l in leads
        ]
        output = io.StringIO()
        # UTF-8 BOM for Polish characters in Microsoft Excel
        output.write("\ufeff")
        fieldnames = [
            "Tryb",
            "Firma",
            "Domena",
            "Miasto",
            "Branża",
            "Lead Score",
            "Cena Leada (PLN)",
            "Szacowany Kontrakt Agencji (PLN)",
            "Idealny Kupiec",
            "NIP",
            "E-maile",
            "Telefony",
            "CMS",
            "SSL",
            "TTFB (ms)",
            "Mobile Viewport",
            "Meta Description",
            "Liczba H1",
            "Schema.org",
            "GTM",
            "GA4",
            "Meta Pixel",
            "TikTok Pixel",
            "Wykryte Sygnały",
            "Social Media",
            "Gotowy Icebreaker (Cold Mail)",
            "Data Audytu",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for l in rows:
            signal_labels = " | ".join(
                f"[{s['category']}] {s['label']}" for s in l.get("signals", [])
            )
            socials_str = " | ".join(
                f"{k}: {v}" for k, v in l.get("social_links", {}).items()
            )
            writer.writerow(
                {
                    "Tryb": "DEMO / TEASER" if teaser else "PEŁNE DANE",
                    "Firma": l.get("company_name", ""),
                    "Domena": l.get("domain", ""),
                    "Miasto": l.get("city", ""),
                    "Branża": l.get("industry", ""),
                    "Lead Score": l.get("lead_score", 0),
                    "Cena Leada (PLN)": l.get("lead_price_pln", 0),
                    "Szacowany Kontrakt Agencji (PLN)": l.get(
                        "estimated_agency_contract_pln", 0
                    ),
                    "Idealny Kupiec": l.get("buyer_persona", ""),
                    "NIP": l.get("nip") or "",
                    "E-maile": ", ".join(l.get("emails", [])),
                    "Telefony": ", ".join(l.get("phones", [])),
                    "CMS": l.get("cms", ""),
                    "SSL": "TAK" if l.get("has_ssl") else "BRAK",
                    "TTFB (ms)": l.get("ttfb_ms", 0),
                    "Mobile Viewport": "TAK" if l.get("has_viewport") else "BRAK",
                    "Meta Description": "TAK" if l.get("has_meta_description") else "BRAK",
                    "Liczba H1": l.get("h1_count", 0),
                    "Schema.org": "TAK" if l.get("has_schema") else "BRAK",
                    "GTM": "TAK" if l.get("has_gtm") else "BRAK",
                    "GA4": "TAK" if l.get("has_ga4") else "BRAK",
                    "Meta Pixel": "TAK" if l.get("has_meta_pixel") else "BRAK",
                    "TikTok Pixel": "TAK" if l.get("has_tiktok_pixel") else "BRAK",
                    "Wykryte Sygnały": signal_labels,
                    "Social Media": socials_str,
                    "Gotowy Icebreaker (Cold Mail)": l.get("icebreaker", ""),
                    "Data Audytu": l.get("audited_at", ""),
                }
            )
        return output.getvalue()


# ---------------------------------------------------------------------------
# High-level Orchestrator Helper
# ---------------------------------------------------------------------------


async def run_harvest_and_audit(
    city: str,
    industry: str,
    limit: int = 5,
    db: Optional[LeadDatabase] = None,
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    """Harvest Polish B2B domains for city + industry, audit concurrently, persist to DB."""
    harvester = HarvesterAgent()
    auditor = SignalAuditorAgent()
    targets = await harvester.harvest(city=city, industry=industry, limit=limit)

    semaphore = asyncio.Semaphore(concurrency)

    async def worker(target: dict[str, str]) -> dict[str, Any]:
        async with semaphore:
            record = await auditor.audit(
                raw_url=target["url"],
                city=target["city"],
                industry=target["industry"],
                hint_company_name=target.get("company_name"),
            )
            if db is not None:
                return db.upsert_lead(record)
            return record.to_dict()

    audited = await asyncio.gather(*(worker(t) for t in targets))
    return sorted(audited, key=lambda x: x["lead_score"], reverse=True)
