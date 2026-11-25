"""
SignalLead PL - Natural Polish Business Correspondence Engine.

Writes concise, calm, polite Polish business emails, LinkedIn DMs, and lead
icebreakers indistinguishable from real human B2B correspondence:
  - Zero em-dashes
  - Zero parenthetical stat dumps like "(2 szt.)" or "(bez VAT)"
  - Zero exclamation-mark spam or AI flattery/slang
  - Proper Polish declensions for cities and industries
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

# Proper Polish locative ("w ..." / "we ...")
CITY_IN: dict[str, str] = {
    "Warszawa": "w Warszawie",
    "Kraków": "w Krakowie",
    "Wrocław": "we Wrocławiu",
    "Poznań": "w Poznaniu",
    "Gdańsk": "w Gdańsku",
    "Katowice": "w Katowicach",
    "Łódź": "w Łodzi",
    "Polska": "w Polsce",
}

# Proper Polish genitive ("z ...")
CITY_FROM: dict[str, str] = {
    "Warszawa": "z Warszawy",
    "Kraków": "z Krakowa",
    "Wrocław": "z Wrocławia",
    "Poznań": "z Poznania",
    "Gdańsk": "z Gdańska",
    "Katowice": "z Katowic",
    "Łódź": "z Łodzi",
    "Polska": "z Polski",
}

# Natural Polish industry names: (genitive plural, capitalized subject plural, short plural)
INDUSTRY_HUMAN: dict[str, tuple[str, str, str]] = {
    "Kancelarie Prawne/Podatkowe": ("kancelarii prawnych", "Kancelarie", "kancelarie"),
    "Kliniki Medyczne/Stomatologia": ("klinik stomatologicznych", "Kliniki", "kliniki"),
    "Architektura/Budownictwo": ("pracowni architektonicznych i firm budowlanych", "Firmy budowlane", "firmy"),
    "Nieruchomości": ("biur nieruchomości", "Firmy z nieruchomości", "firmy"),
    "Hotele B2B": ("hoteli", "Hotele", "hotele"),
    "Produkcja/Logistyka": ("firm produkcyjnych i logistycznych", "Firmy produkcyjne", "firmy"),
}

# Single concise human focus phrase per agency type (never dumps 3 scraped DB tags)
AGENCY_FOCUS_HUMAN: dict[str, str] = {
    "Software House / Agencja Web Dev": "tworzeniem i rozwojem stron",
    "Agencja Performance & SEO 360": "marketingiem i SEO",
    "Agencja SEO / SEM": "SEO i kampaniami Google Ads",
    "Agencja Digital Marketing": "marketingiem internetowym",
}


def _pick_variant(seed: str, count: int) -> int:
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % max(1, count)


def pl_plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return f"{n} {one}"
    mod10 = n % 10
    mod100 = n % 100
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return f"{n} {few}"
    return f"{n} {many}"


def city_locative(city: Optional[str]) -> str:
    if not city or city == "Wszystkie":
        return "w Polsce"
    return CITY_IN.get(city, f"w {city}")


def city_genitive(city: Optional[str]) -> str:
    if not city or city == "Wszystkie":
        return "z Polski"
    return CITY_FROM.get(city, f"z {city}")


def format_zl(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " zł"


def humanize_services(services: list[str], agency_type: str) -> str:
    """Return one concise, natural Polish phrase describing an agency's focus."""
    s = set(services)
    if "SEO / Pozycjonowanie" in s and "Google Ads / SEM" in s:
        return "SEO i reklamą w Google"
    if "SEO / Pozycjonowanie" in s:
        return "SEO i widocznością stron"
    if "Software House / Custom IT" in s or "Web Dev / WordPress" in s:
        return "tworzeniem i poprawą stron"
    if "Meta Ads / Social Ads" in s:
        return "kampaniami reklamowymi"
    return AGENCY_FOCUS_HUMAN.get(agency_type, "marketingiem internetowym")


def humanize_signal_for_owner(code: str, ttfb_ms: int = 0) -> str:
    """Plain, respectful Polish description of a technical gap without parentheses."""
    if code == "no_ssl":
        return "strona otwiera się bez szyfrowania HTTPS, przez co przeglądarka pokazuje ostrzeżenie"
    if code == "slow_ttfb":
        sec = f"{ttfb_ms / 1000:.1f}".replace(".", ",")
        return f"serwer odpowiada dość wolno, ponad {sec} sekundy na start"
    if code == "no_viewport":
        return "strona nie dopasowuje się poprawnie do ekranów telefonów"
    if code == "legacy_cms":
        return "strona działa na starszym silniku CMS, co utrudnia jej rozwój i pozycjonowanie"
    if code == "missing_meta_description":
        return "brakuje opisów meta widocznych w wynikach Google"
    if code == "missing_h1":
        return "na stronie głównej brakuje nagłówka H1 dla wyszukiwarki"
    if code == "multiple_h1":
        return "na stronie głównej jest kilka osobnych nagłówków H1"
    if code == "missing_schema":
        return "brakuje danych strukturalnych Schema.org dla Google"
    if code == "missing_ga4":
        return "nie widać podpiętego Google Analytics 4"
    if code == "missing_gtm":
        return "nie ma wdrożonego Google Tag Managera"
    if code == "missing_meta_pixel":
        return "brakuje pixela Mety do mierzenia ruchu z reklam"
    if code == "missing_tiktok_pixel":
        return "brakuje podstawowej analityki pod kampanie w social mediach"
    return "widać kilka podstawowych braków technicznych na stronie"


def humanize_signal_short(label_or_code: str) -> str:
    """Short, plain Polish phrase for agency business emails (no parentheses, no jargon)."""
    low = label_or_code.lower()
    if "ssl" in low or "https" in low:
        return "brak SSL"
    if "ttfb" in low or "wolny" in low:
        return "wolną odpowiedź serwera"
    if "viewport" in low or "mobil" in low:
        return "brak wersji mobilnej"
    if "joomla" in low or "wix" in low or "przestarza" in low:
        return "starszy CMS"
    if "description" in low:
        return "brak meta opisów"
    if "h1" in low:
        return "błędy nagłówków H1"
    if "schema" in low:
        return "brak Schema.org"
    if "ga4" in low or "analytics" in low:
        return "brak GA4"
    if "gtm" in low or "tag manager" in low:
        return "brak GTM"
    if "meta pixel" in low:
        return "brak pixela Mety"
    if "tiktok" in low:
        return "brak pixela TikTok"
    return "braki techniczne na stronie"


def _join_pl(items: list[str]) -> str:
    unique: list[str] = []
    for item in items:
        if item not in unique:
            unique.append(item)
    if not unique:
        return "kilka podstawowych braków na stronie"
    cleaned: list[str] = []
    prev_was_brak = False
    for item in unique:
        if item.startswith("brak ") and prev_was_brak:
            cleaned.append(item[5:])
        else:
            cleaned.append(item)
            prev_was_brak = item.startswith("brak ")
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} i {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} i {cleaned[-1]}"


# ---------------------------------------------------------------------------
# 1. Human Polish Business Icebreaker for End B2B Company
# ---------------------------------------------------------------------------


def build_human_icebreaker(
    company_name: str,
    domain: str,
    city: str,
    signals: list[Any],
    ttfb_ms: int = 0,
) -> str:
    """Generate a calm, polite, human Polish business email opener (zero em-dashes or parentheses)."""
    v = _pick_variant(domain, 3)

    if not signals:
        openings = [
            f"Dzień dobry, zajrzałem dziś na {domain} i technicznie strona wygląda bardzo dobrze, ale widzę dwie drobne rzeczy, które mogą poprawić liczbę zapytań z Google. Czy mogę krótko napisać o co chodzi?",
            f"Dzień dobry, przeglądałem dziś strony firm {city_locative(city)} i trafiłem na {domain}. Podstawa działa sprawnie, mam jednak dwie małe uwagi pod widoczność w wyszukiwarce. Czy mogę przesłać krótką notatkę?",
            f"Dzień dobry, sprawdziłem stronę {domain} pod kątem SEO i szybkości. Działa bez większych błędów, ale widzę dwie proste rzeczy do dopracowania. Czy ktoś u Państwa zajmuje się obecnie stroną?",
        ]
        return openings[v]

    severity_rank = {"critical": 0, "high": 1, "medium": 2}
    top = sorted(
        signals,
        key=lambda s: (
            severity_rank.get(
                s["severity"] if isinstance(s, dict) else s.severity, 3
            ),
            -(s["points"] if isinstance(s, dict) else s.points),
        ),
    )[:2]

    phrases = [
        humanize_signal_for_owner(
            s["code"] if isinstance(s, dict) else s.code, ttfb_ms=ttfb_ms
        )
        for s in top
    ]
    gaps = _join_pl(phrases)

    if v == 0:
        return (
            f"Dzień dobry, wszedłem dziś na {domain} i zauważyłem, że {gaps}. "
            f"Czy ktoś u Państwa zajmuje się teraz opieką nad stroną, czy mogę przesłać krótką notatkę co warto poprawić?"
        )
    if v == 1:
        return (
            f"Dzień dobry, sprawdzałem dziś kilka stron firm {city_locative(city)} i trafiłem na {domain}. "
            f"Zauważyłem, że {gaps}, przez co część osób wchodzących z Google może od razu wychodzić. "
            f"Mogę wysłać w mailu krótką listę rzeczy do poprawy?"
        )
    return (
        f"Dzień dobry, przy sprawdzaniu strony {domain} zauważyłem, że {gaps}. "
        f"Zwykle takie kwestie da się poprawić bez przebudowy całej strony. "
        f"Jeśli temat jest u Państwa otwarty, chętnie prześlę kilka konkretnych uwag."
    )


# ---------------------------------------------------------------------------
# 2. Human Polish Business Cold Mail & LinkedIn DM for Agencies
# ---------------------------------------------------------------------------


def build_human_agency_outreach(
    agency_name: str,
    buyer_domain: str,
    services: list[str],
    agency_type: str,
    total_leads: int,
    hot_leads: int,
    with_nip: int,
    with_email: int,
    bundle_price_pln: int,
    pipeline_pln: int,
    top_signals: list[str],
    top_industries: list[str],
    used_city: Optional[str],
    sender_name: str = "Eryk",
    pack_url: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Build (subject, email_body, linkedin_dm) in plain, authentic Polish business style.
    Zero em-dashes, zero parentheses, zero exclamation marks, zero AI buzzwords.
    """
    v = _pick_variant(buyer_domain or agency_name, 3)
    focus = humanize_services(services, agency_type)
    city_in = city_locative(used_city)
    city_from = city_genitive(used_city)
    clean_sender = (
        sender_name.strip()
        if sender_name and sender_name.strip() != "Zespół SignalLead PL"
        else "Eryk"
    )

    primary_ind = top_industries[0] if top_industries else ""
    ind_gen, ind_subj_cap, _ = INDUSTRY_HUMAN.get(
        primary_ind, ("firm B2B", "Firmy", "firmy")
    )

    short_gaps = [humanize_signal_short(s) for s in top_signals[:3]]
    gaps_two = _join_pl(short_gaps[:2])
    gaps_three = _join_pl(short_gaps[:3])

    if total_leads == 0:
        subject = f"Pytanie ws. klientów dla {agency_name}"
        email_body = (
            f"Dzień dobry,\n\n"
            f"ponieważ w {agency_name} zajmujecie się {focus}, chciałem zapytać czy korzystacie "
            f"z gotowych list polskich firm z wykrytymi brakami technicznymi lub w analityce.\n\n"
            f"Przed wysłaniem uruchom najpierw skan firm w panelu, żeby mail zawierał konkretne liczby.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )
        linkedin_dm = (
            f"Dzień dobry, widzę, że w {agency_name} zajmujecie się {focus}. "
            f"Zbieram listy polskich firm z konkretnymi brakami na stronie na wyłączność dla jednej agencji. "
            f"Czy mogę podesłać anonimową próbkę w CSV?"
        )
        return subject, email_body, linkedin_dm

    if primary_ind == "Kancelarie Prawne/Podatkowe":
        count_ind = pl_plural(total_leads, "kancelaria", "kancelarie", "kancelarii")
        count_ind_acc = pl_plural(total_leads, "kancelarię", "kancelarie", "kancelarii")
        count_ind_gen = pl_plural(total_leads, "kancelarii", "kancelarii", "kancelarii")
    elif primary_ind == "Kliniki Medyczne/Stomatologia":
        count_ind = pl_plural(total_leads, "klinika", "kliniki", "klinik")
        count_ind_acc = pl_plural(total_leads, "klinikę", "kliniki", "klinik")
        count_ind_gen = pl_plural(total_leads, "kliniki", "klinik", "klinik")
    elif primary_ind == "Hotele B2B":
        count_ind = pl_plural(total_leads, "hotel", "hotele", "hoteli")
        count_ind_acc = pl_plural(total_leads, "hotel", "hotele", "hoteli")
        count_ind_gen = pl_plural(total_leads, "hotelu", "hoteli", "hoteli")
    else:
        count_ind = pl_plural(total_leads, "firma", "firmy", "firm")
        count_ind_acc = pl_plural(total_leads, "firmę", "firmy", "firm")
        count_ind_gen = pl_plural(total_leads, "firmy", "firm", "firm")

    price_str = format_zl(bundle_price_pln)
    link_line_a = f"Można ją też podejrzeć online tutaj: {pack_url}\n\n" if pack_url else ""
    link_line_b = f"Link do podglądu online: {pack_url}\n\n" if pack_url else ""

    if v == 0:
        subject = f"{ind_subj_cap} {city_in} - próbka w załączniku"
        email_body = (
            f"Dzień dobry,\n\n"
            f"sprawdziłem dziś kilka {ind_gen} {city_in} pod kątem technicznym i analityki. "
            f"Odłożyłem {count_ind_acc}, u których na stronie widać podstawowe rzeczy do poprawy, głównie {gaps_two}.\n\n"
            f"W załączniku wysyłam krótką próbkę w CSV z samymi wynikami audytu, na razie bez nazw firm. "
            f"Jeśli szukacie teraz nowych klientów z tej branży, mogę oddać pełną listę z domenami, mailami i NIP-ami "
            f"na wyłączność dla {agency_name} za {price_str} bez VAT. Po sprzedaży nie wysyłam jej nikomu więcej.\n\n"
            f"{link_line_a}"
            f"Dajcie znać, czy taki temat jest dla Was ciekawy.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )
    elif v == 1:
        subject = f"Lista {count_ind_gen} {city_in}"
        email_body = (
            f"Dzień dobry,\n\n"
            f"przejrzałem dziś strony {ind_gen} {city_in} i zebrałem {count_ind_acc}, "
            f"które mają konkretne rzeczy do poprawy, m.in. {gaps_three}. "
            f"Ponieważ w {agency_name} zajmujecie się {focus}, pomyślałem, że mogą to być dobre tematy do rozmowy.\n\n"
            f"Załączyłem plik CSV z samymi wynikami bez nazw firm, żeby można było ocenić jakość przed decyzją. "
            f"Pełna lista z odkrytymi danymi kosztuje {price_str} bez VAT i sprzedaję ją tylko jednej agencji.\n\n"
            f"{link_line_b}"
            f"Czy jesteście zainteresowani taką listą?\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )
    else:
        subject = f"Pytanie ws. firm {city_from}"
        email_body = (
            f"Dzień dobry,\n\n"
            f"przygotowałem dzisiaj krótką listę {count_ind_gen} {city_in}, "
            f"u których na stronie wyszły konkretne rzeczy do poprawy, w tym {gaps_two}. "
            f"Do większości mam już zebrane maile kontaktowe oraz numery NIP.\n\n"
            f"W załączniku przesyłam anonimową próbkę CSV, żebyście mogli sprawdzić jak to wygląda. "
            f"Jeśli pasuje to do Waszej oferty, pełną wersję na wyłączność dla {agency_name} "
            f"mogę przekazać za {price_str} bez VAT.\n\n"
            f"{link_line_a}"
            f"Jeśli temat jest nieaktualny, przepraszam za zajęcie czasu.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )

    if pack_url:
        linkedin_dm = (
            f"Dzień dobry, sprawdziłem dziś kilka {ind_gen} {city_in} i zebrałem {count_ind_acc} "
            f"z konkretnymi rzeczami do poprawy na stronie. Jeśli szukacie teraz klientów z tej branży, "
            f"anonimowy podgląd jest tutaj: {pack_url}"
        )
    else:
        linkedin_dm = (
            f"Dzień dobry, sprawdziłem dziś kilka {ind_gen} {city_in} i zebrałem {count_ind_acc}, "
            f"u których widać m.in. {gaps_two}. Mogę podesłać anonimową próbkę CSV, "
            f"żebyście zobaczyli czy to temat dla Was?"
        )

    return subject, email_body, linkedin_dm
