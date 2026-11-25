"""
SignalLead PL — Natural Polish Human Copywriting Engine (Anti-AI-Slop).

Ensures every customer-facing message (lead icebreakers, agency cold emails,
LinkedIn DMs) reads like a concise, respectful 1-on-1 message written by a real
Polish founder/specialist — proper Polish city & industry declensions, natural
technical phrasing, subtle deterministic variation, and zero corporate/AI clichés.
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

# Natural Polish industry names: (genitive plural "kilku ...", nominative plural "4 ...", short subject form)
INDUSTRY_HUMAN: dict[str, tuple[str, str, str]] = {
    "Kancelarie Prawne/Podatkowe": ("kancelarii prawnych", "kancelarie", "kancelarii"),
    "Kliniki Medyczne/Stomatologia": ("klinik stomatologicznych i medycznych", "kliniki", "klinik"),
    "Architektura/Budownictwo": ("pracowni architektonicznych i firm budowlanych", "firmy budowlane", "firm z budowlanki"),
    "Nieruchomości": ("biur nieruchomości i deweloperów", "firmy z nieruchomości", "firm z nieruchomości"),
    "Hotele B2B": ("hoteli i obiektów konferencyjnych", "hotele", "hoteli"),
    "Produkcja/Logistyka": ("firm produkcyjnych i logistycznych", "firmy produkcyjne", "firm z produkcji i logistyki"),
}

# Natural conversational phrasing for detected agency services
SERVICE_HUMAN: dict[str, str] = {
    "SEO / Pozycjonowanie": "SEO",
    "Google Ads / SEM": "Google Ads",
    "Meta Ads / Social Ads": "kampanie w social mediach",
    "Analityka GA4 / GTM": "analitykę",
    "Web Dev / WordPress": "strony WWW",
    "Software House / Custom IT": "projekty webowe i IT",
    "E-commerce": "e-commerce",
}

AGENCY_TYPE_HUMAN: dict[str, str] = {
    "Software House / Agencja Web Dev": "strony i aplikacje webowe",
    "Agencja Performance & SEO 360": "SEO i kampanie performance",
    "Agencja SEO / SEM": "SEO i Google Ads",
    "Agencja Digital Marketing": "marketing internetowy i pozyskiwanie klientów",
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
    return CITY_IN.get(city, f"w mieście {city}")


def format_zl(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " zł"


def humanize_services(services: list[str], agency_type: str) -> str:
    mapped: list[str] = []
    for s in services:
        h = SERVICE_HUMAN.get(s)
        if h and h not in mapped:
            mapped.append(h)
    if not mapped:
        return AGENCY_TYPE_HUMAN.get(agency_type, "marketing internetowy")
    top = mapped[:3]
    if len(top) == 1:
        return top[0]
    if len(top) == 2:
        return f"{top[0]} i {top[1]}"
    return f"{top[0]}, {top[1]} i {top[2]}"


def humanize_signal_for_owner(code: str, ttfb_ms: int = 0) -> str:
    """Plain, respectful Polish description of a website gap for the company owner."""
    if code == "no_ssl":
        return "strona otwiera się bez szyfrowania HTTPS, przez co Chrome pokazuje ostrzeżenie"
    if code == "slow_ttfb":
        sec = f"{ttfb_ms / 1000:.1f}".replace(".", ",")
        return f"serwer odpowiada dość wolno (ok. {sec} s zanim strona w ogóle ruszy)"
    if code == "no_viewport":
        return "strona nie skaluje się poprawnie na telefonach (brak ustawień mobile viewport)"
    if code == "legacy_cms":
        return "strona stoi jeszcze na starszym silniku CMS, który utrudnia rozbudowę i pozycjonowanie"
    if code == "missing_meta_description":
        return "brakuje opisów meta, przez co wizytówka w Google wygląda gorzej niż u konkurencji"
    if code == "missing_h1":
        return "na stronie głównej brakuje głównego nagłówka H1 dla wyszukiwarki"
    if code == "multiple_h1":
        return "nagłówki H1 są rozbite na kilka miejsc na stronie głównej, co osłabia SEO"
    if code == "missing_schema":
        return "brakuje danych strukturalnych Schema.org dla Google"
    if code == "missing_ga4":
        return "nie widać podpiętego Google Analytics 4"
    if code == "missing_gtm":
        return "nie ma wdrożonego Google Tag Managera"
    if code == "missing_meta_pixel":
        return "brakuje pixela Mety do zbierania ruchu na remarketing"
    if code == "missing_tiktok_pixel":
        return "nie ma podpiętej analityki pod kampanie social"
    return "widać kilka prostych luk technicznych na stronie"


def humanize_signal_short(label_or_code: str) -> str:
    """Short, natural Polish phrase for agency outreach emails (no raw system tags)."""
    low = label_or_code.lower()
    if "ssl" in low or "https" in low:
        return "brak SSL/HTTPS"
    if "ttfb" in low or "wolny" in low:
        return "wolny serwer (TTFB > 1,4 s)"
    if "viewport" in low or "mobil" in low:
        return "brak wersji mobilnej"
    if "joomla" in low or "wix" in low or "przestarza" in low:
        return "stary CMS"
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
    return "luki techniczne na stronie"


def _join_pl(items: list[str]) -> str:
    unique: list[str] = []
    for item in items:
        if item not in unique:
            unique.append(item)
    if not unique:
        return "kilka prostych braków na stronie"
    # Collapse repeated "brak X, brak Y" -> "brak X, Y" for natural Polish flow
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
    return f"{', '.join(cleaned[:-1])} oraz {cleaned[-1]}"


# ---------------------------------------------------------------------------
# 1. Human Icebreaker for End B2B Company (used by agency buyers)
# ---------------------------------------------------------------------------


def build_human_icebreaker(
    company_name: str,
    domain: str,
    city: str,
    signals: list[Any],
    ttfb_ms: int = 0,
) -> str:
    """Generate a natural, short Polish 1-on-1 icebreaker with zero AI slop."""
    v = _pick_variant(domain, 3)

    if not signals:
        openings = [
            f"Dzień dobry, zajrzałem dziś na {domain} i technicznie strona wygląda solidnie, ale widzę dwie drobne rzeczy, które potrafią podnieść liczbę zapytań z Google. Mogę je krótko wypisać w mailu?",
            f"Dzień dobry, przeglądałem dziś strony firm {city_locative(city)} i wpadłem na {domain}. Podstawa jest dobrze zrobiona, ale mam 2 szybkie pomysły pod konwersję — podesłać w kilku zdaniach?",
            f"Cześć, rzuciłem okiem na {domain} ({company_name}). Strona działa sprawnie, natomiast widzę małą rezerwę w pozyskiwaniu zapytań z wyszukiwarki — mogę podesłać krótką notatkę?",
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
            f"Dzień dobry, zajrzałem dziś na {domain} i rzuciły mi się w oczy dwie proste rzeczy: "
            f"{gaps}. Jeśli to u Państwa aktualny temat, mogę w 3 krótkich punktach napisać, "
            f"jak to najszybciej ogarnąć?"
        )
    if v == 1:
        return (
            f"Dzień dobry, przeglądałem dziś strony firm {city_locative(city)} i sprawdziłem przy okazji {domain}. "
            f"Widzę tam konkretny temat do poprawy: {gaps} — przez to część osób z Google może po prostu uciekać. "
            f"Mogę podesłać krótką notatkę, co warto poprawić w pierwszej kolejności?"
        )
    return (
        f"Dzień dobry, wpadłem na stronę {company_name} ({domain}) i zauważyłem, że {gaps}. "
        f"Zwykle da się to naprawić dość sprawnie bez przebudowy całej strony — "
        f"chcą Państwo rzucić okiem na krótką listę poprawek?"
    )


# ---------------------------------------------------------------------------
# 2. Human Teaser-First Cold Mail & LinkedIn DM for Polish Agencies
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
    Build (subject, email_body, linkedin_dm) written like a real human founder in Poland.
    Zero AI buzzwords, zero corporate templates, proper Polish grammar.
    """
    v = _pick_variant(buyer_domain or agency_name, 3)
    natural_services = humanize_services(services, agency_type)
    city_in = city_locative(used_city)
    clean_sender = (
        sender_name.strip()
        if sender_name and sender_name.strip() != "Zespół SignalLead PL"
        else "Eryk"
    )

    # Human industry phrasing
    primary_ind = top_industries[0] if top_industries else ""
    ind_gen, ind_few, ind_many = INDUSTRY_HUMAN.get(
        primary_ind, ("firm B2B", "firmy B2B", "firm B2B")
    )

    short_gaps = [humanize_signal_short(s) for s in top_signals[:3]]
    gaps_text = _join_pl(short_gaps[:2])
    gaps_email = _join_pl(short_gaps[:3])

    if total_leads == 0:
        subject = f"krótkie pytanie o nowych klientów dla {agency_name}"
        email_body = (
            f"Cześć,\n\n"
            f"wpadłem na stronę {agency_name} i widzę, że zajmujecie się głównie takimi tematami jak {natural_services}.\n\n"
            f"Wyszukuję na bieżąco polskie firmy B2B, które mają na stronie konkretne, kosztowne braki "
            f"(np. brak GA4/pixela, wolny serwer, brak SSL lub podstaw SEO) i składam z tego krótkie listy na wyłączność "
            f"dla jednej agencji.\n\n"
            f"Najpierw podeślij sobie kilka firm przez Harvester w panelu, żeby mail miał konkretne liczby dla tej agencji.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )
        linkedin_dm = (
            f"Cześć! Widzę, że w {agency_name} ogarniacie {natural_services}. "
            f"Zbieram listy polskich firm z konkretnymi brakami na stronie (GA4, pixel, szybkość, SEO) na wyłączność dla 1 agencji. "
            f"Podesłać Ci anonimową próbkę z Waszego miasta?"
        )
        return subject, email_body, linkedin_dm

    # Proper Polish count for industry/firms
    firms_phrase = pl_plural(total_leads, "firma", "firmy", "firm")
    if primary_ind == "Kancelarie Prawne/Podatkowe":
        count_ind = pl_plural(total_leads, "kancelaria", "kancelarie", "kancelarii")
        count_ind_acc = pl_plural(total_leads, "kancelarię", "kancelarie", "kancelarii")
    elif primary_ind == "Kliniki Medyczne/Stomatologia":
        count_ind = pl_plural(total_leads, "klinika", "kliniki", "klinik")
        count_ind_acc = pl_plural(total_leads, "klinikę", "kliniki", "klinik")
    elif primary_ind == "Hotele B2B":
        count_ind = pl_plural(total_leads, "hotel", "hotele", "hoteli")
        count_ind_acc = pl_plural(total_leads, "hotel", "hotele", "hoteli")
    else:
        count_ind = firms_phrase
        count_ind_acc = pl_plural(total_leads, "firmę", "firmy", "firm")

    price_str = format_zl(bundle_price_pln)
    link_sentence = (
        f"Podgląd online z możliwością odblokowania od ręki (BLIK/karta) wrzuciłem też tutaj: {pack_url}\n\n"
        if pack_url
        else ""
    )
    hot_note = (
        f" ({pl_plural(hot_leads, 'pilny przypadek', 'pilne przypadki', 'pilnych przypadków')} z dużymi brakami)"
        if hot_leads > 0
        else ""
    )

    if v == 0:
        subject = f"{count_ind} {city_in} — {gaps_text}"
        email_body = (
            f"Cześć,\n\n"
            f"wpadłem na stronę {agency_name} i widzę, że ogarniacie {natural_services}. "
            f"Akurat sprawdzałem dzisiaj kilka {ind_gen} {city_in} i trafiłem na {count_ind_acc}{hot_note}, "
            f"u których na stronie widać proste do naprawienia luki, m.in. {gaps_email}.\n\n"
            f"Wrzuciłem do załącznika darmową próbkę w CSV z samymi wynikami audytu (bez nazw firm), "
            f"żebyście mogli w pół minuty rzucić okiem, czy to w ogóle Wasz profil klienta.\n\n"
            f"Jeśli wyglądają sensownie, mogę oddać całą listę z domenami, NIP-ami ({with_nip} szt.) "
            f"i bezpośrednimi mailami ({with_email} szt.) na wyłączność tylko dla {agency_name} za {price_str} (bez VAT) "
            f"— po zakupie zdejmuję ją ze sprzedaży dla innych agencji.\n\n"
            f"{link_sentence}"
            f"Daj znać, czy zbieracie teraz takich klientów.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )
    elif v == 1:
        subject = f"krótko o {pl_plural(total_leads, 'firmie', 'firmach', 'firmach')} {city_in} (próbka w załączniku)"
        email_body = (
            f"Dzień dobry,\n\n"
            f"piszę krótko, bo widzę, że w {agency_name} zajmujecie się takimi tematami jak {natural_services}. "
            f"Przejrzałem dziś strony {ind_gen} {city_in} i odłożyłem {count_ind_acc}, "
            f"gdzie od razu widać konkretny punkt zaczepienia do rozmowy ({gaps_email}).\n\n"
            f"W załączniku jest bezpłatny podgląd w CSV z zamaskowanymi domenami — widać błędy, czasy ładowania i CMS, "
            f"więc łatwo ocenić jakość bez żadnego zobowiązania.\n\n"
            f"Pełną listę z odkrytymi danymi, walidowanymi NIP-ami i krótką notatką pod każdą firmę "
            f"sprzedaję tylko jednej agencji na wyłączność za {price_str} (cena końcowa bez VAT).\n\n"
            f"{link_sentence}"
            f"Jeśli temat jest dla Was ciekawy, odpisz proszę — mogę przytrzymać tę paczkę dla Was do jutra.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )
    else:
        subject = f"{count_ind} {city_in} z brakami na stronie — anonimowa próbka"
        email_body = (
            f"Cześć,\n\n"
            f"zerknąłem na ofertę {agency_name} ({natural_services}) i pomyślałem, że może Wam się przydać "
            f"świeża lista, którą dziś sprawdziłem {city_in}. Jest na niej {count_ind} ({ind_gen}) "
            f"z konkretnymi brakami technicznymi i marketingowymi: {gaps_email}.\n\n"
            f"Zamiast opisywać to w mailu, załączyłem darmowy plik CSV z samymi błędami (nazwy spółek są ukryte). "
            f"Przy większości mamy wyciągnięte maile ({with_email}/{total_leads}) i NIP ({with_nip}/{total_leads}).\n\n"
            f"Całość oddaję wyłącznie jednej agencji za {price_str} (bez VAT), żeby te same firmy nie lądowały u konkurencji.\n\n"
            f"{link_sentence}"
            f"Daj znać, czy w ogóle korzystacie z takich list, czy wszystko robicie u siebie.\n\n"
            f"Pozdrawiam,\n{clean_sender}"
        )

    if pack_url:
        linkedin_dm = (
            f"Cześć! Widzę, że w {agency_name} zajmujecie się tematami typu {natural_services}. "
            f"Sprawdziłem dziś parę {ind_gen} {city_in} i wyłapałem {count_ind_acc}, gdzie widać {gaps_text}. "
            f"Anonimową próbkę audytu wrzuciłem tutaj, jeśli chcesz rzucić okiem: {pack_url}"
        )
    else:
        linkedin_dm = (
            f"Cześć! Widzę, że w {agency_name} ogarniacie {natural_services}. "
            f"Sprawdziłem dziś kilka {ind_gen} {city_in} i trafiłem na {count_ind_acc}, u których widać {gaps_text}. "
            f"Wrzucić Ci tutaj anonimową próbkę w CSV (bez nazw firm), żebyś ocenił czy to Wasz typ klienta?"
        )

    return subject, email_body, linkedin_dm
