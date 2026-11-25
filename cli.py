#!/usr/bin/env python3
"""
SignalLead PL — Batch CLI Tool.

Examples:
  python cli.py --url https://www.dzp.pl --city Warszawa --industry "Kancelarie Prawne/Podatkowe"
  python cli.py --harvest --city Kraków --industry "Kliniki Medyczne/Stomatologia" --limit 5
  python cli.py --list --teaser --export-csv demo_pack.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from buyer_finder import (
    AGENCY_TYPES,
    OUTREACH_STATUSES,
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


def print_lead_summary(leads: list[dict[str, Any]], teaser: bool = False) -> None:
    if not leads:
        print("Brak leadów do wyświetlenia.")
        return

    kpis = LeadDatabase.compute_kpis(leads)
    mode_label = "DEMO / TEASER" if teaser else "PEŁNE DANE"

    print("\n" + "=" * 92)
    print(f" SignalLead PL  |  Tryb: {mode_label}")
    print(
        f" Leady: {kpis['total_leads']}  |  Gorące (75+): {kpis['hot_leads_75_plus']}  |  "
        f"Wartość paczki: {kpis['pack_value_pln']} PLN  |  "
        f"Pakiet (-25%): {kpis['bundle_price_pln']} PLN  |  "
        f"Pipeline: {kpis['total_contract_pipeline_pln']:,} PLN".replace(",", " ")
    )
    print("=" * 92)

    for idx, lead in enumerate(leads, 1):
        emails = ", ".join(lead.get("emails", [])[:2]) or "brak"
        phones = ", ".join(lead.get("phones", [])[:1]) or "brak"
        nip = lead.get("nip") or "brak"
        signals = lead.get("signals", [])
        top_sig = "; ".join(s["label"] for s in signals[:3]) or "brak krytycznych błędów"
        print(
            f"\n[{idx}] {lead['company_name']} ({lead['domain']}) — {lead['city']} | {lead['industry']}"
        )
        print(
            f"    Score: {lead['lead_score']}/99 | Cena: {lead['lead_price_pln']} PLN | "
            f"Kontrakt: ~{lead['estimated_agency_contract_pln']:,} PLN | Kupiec: {lead['buyer_persona']}".replace(
                ",", " "
            )
        )
        print(
            f"    Tech: CMS={lead['cms']} | SSL={'OK' if lead['has_ssl'] else 'BRAK'} | "
            f"TTFB={lead['ttfb_ms']}ms | HTTP={lead['http_status']}"
        )
        print(f"    Kontakt: NIP={nip} | Email={emails} | Tel={phones}")
        print(f"    Sygnały ({len(signals)}): {top_sig}")
        print(f"    Icebreaker: {lead['icebreaker']}")
    print()


def print_buyer_summary(buyers: list[dict[str, Any]]) -> None:
    if not buyers:
        print("Brak agencji w bazie Buyer Finder.")
        return

    kpis = BuyerDatabase.compute_kpis(buyers)
    print("\n" + "=" * 92)
    print(" SignalLead PL — Buyer Finder (Agencje / Kupcy Paczek)")
    print(
        f" Agencje: {kpis['total_buyers']}  |  Z e-mailem: {kpis['with_email_count']}  |  "
        f"Wysłano Demo: {kpis['teaser_sent_count']}  |  Odpowiedzi: {kpis['replied_count']}  |  "
        f"Opłacone: {kpis['paid_count']}"
    )
    print("=" * 92)

    for idx, b in enumerate(buyers, 1):
        emails = ", ".join(b.get("emails", [])[:2]) or "brak"
        phones = ", ".join(b.get("phones", [])[:1]) or "brak"
        services = ", ".join(b.get("services", [])[:4]) or "nie wykryto"
        status_pl = OUTREACH_STATUSES.get(b.get("outreach_status", "new"), "Nowa")
        print(
            f"\n[{idx}] {b['agency_name']} ({b['domain']}) — {b['city']} | {b['agency_type']}"
        )
        print(
            f"    Status: {status_pl} | NIP={b.get('nip') or 'brak'} | Email={emails} | Tel={phones}"
        )
        print(f"    Usługi: {services}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signallead",
        description="SignalLead PL — CLI do audytu polskich stron B2B, paczek Teaser-First i Buyer Findera.",
    )
    parser.add_argument("--url", type=str, help="Pojedyncza domena lub URL do audytu na żywo.")
    parser.add_argument(
        "--harvest",
        action="store_true",
        help="Uruchom HarvesterAgent dla wybranego miasta i branży.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Wyświetl aktualne leady zapisane w bazie SQLite.",
    )
    parser.add_argument(
        "--find-buyers",
        action="store_true",
        help="Wyszukaj i wzbogać polskie agencje (Buyer Finder) dla wybranego miasta.",
    )
    parser.add_argument(
        "--list-buyers",
        action="store_true",
        help="Wyświetl zapisane agencje z bazy Buyer Finder.",
    )
    parser.add_argument(
        "--agency-type",
        type=str,
        default="Wszystkie",
        help=f"Specjalizacja agencji ({', '.join(AGENCY_TYPES)} lub Wszystkie).",
    )
    parser.add_argument(
        "--generate-offer",
        type=str,
        default=None,
        help="Wygeneruj spersonalizowaną ofertę Teaser-First dla domeny agencji (np. delante.pl).",
    )
    parser.add_argument(
        "--send-outreach",
        type=str,
        default=None,
        help="Wyślij ofertę Teaser-First (+ załącznik Demo CSV) do agencji (Resend / SMTP / Sandbox).",
    )
    parser.add_argument(
        "--send-batch",
        action="store_true",
        help="Wyślij serię ofert Teaser-First do nowych agencji (z limitem dziennym i cooldownem).",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://localhost:8000",
        help="Publiczny adres serwera do linków /pack/{id} w wysyłanych mailach.",
    )
    parser.add_argument(
        "--city",
        type=str,
        default="Warszawa",
        help=f"Miasto docelowe ({', '.join(CITIES.keys())}). Domyślnie: Warszawa.",
    )
    parser.add_argument(
        "--industry",
        type=str,
        default=INDUSTRIES[0],
        help=f"Branża B2B ({', '.join(INDUSTRIES)}). Domyślnie: {INDUSTRIES[0]}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Liczba firm lub agencji do pobrania (domyślnie: 5).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Minimalny lead_score przy listowaniu/eksporcie/ofercie (0-99).",
    )
    parser.add_argument(
        "--teaser",
        action="store_true",
        help="Tryb DEMO / TEASER (maskuje nazwy firm, domeny, NIP i kontakty).",
    )
    parser.add_argument(
        "--export-csv",
        type=str,
        default=None,
        help="Ścieżka do pliku wyjściowego CSV (kodowanie UTF-8-SIG dla Excela).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="signallead.db",
        help="Ścieżka do bazy SQLite (domyślnie: signallead.db).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Wypisz wynik jako sformatowany JSON zamiast tabeli tekstowej.",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    db = LeadDatabase(args.db)
    buyer_db = BuyerDatabase(args.db)

    # --- Automated Outreach Mailer CLI modes ---
    if args.send_outreach or args.send_batch:
        from outreach_mailer import OutreachDatabase, OutreachMailerService
        from pack_store import PackDatabase

        pack_db = PackDatabase(args.db)
        outreach_db = OutreachDatabase(args.db)
        mailer = OutreachMailerService(
            lead_db=db, buyer_db=buyer_db, pack_db=pack_db, outreach_db=outreach_db
        )
        if args.send_outreach:
            res = await mailer.send_to_buyer(
                buyer_domain=args.send_outreach,
                base_url=args.base_url,
                min_score=args.min_score,
            )
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                log = res["log"]
                print(
                    f"[{res['status'].upper()} / {res['provider']}] -> {log['agency_name']} ({log['recipient_email']}) | "
                    f"Załącznik: {log['attachment_filename']} ({log['attachment_bytes']} B)"
                )
            return 0
        else:
            batch = await mailer.send_batch(
                base_url=args.base_url,
                city=args.city,
                agency_type=args.agency_type,
                limit=max(1, args.limit),
                min_score=args.min_score,
            )
            if args.json:
                print(json.dumps(batch, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Outreach Batch ({batch['provider']}): wysłano={batch['sent_count']}, "
                    f"pominięto={batch['skipped_count']}, błędów={batch['failed_count']} "
                    f"| Limit dzienny: {batch['kpis']['sent_today']}/{batch['kpis']['daily_limit']}"
                )
            return 0

    # --- Buyer Finder CLI modes ---
    if args.find_buyers:
        if args.city not in CITIES:
            print(
                f"Błąd: nieznane miasto '{args.city}'. Dostępne: {', '.join(CITIES.keys())}",
                file=sys.stderr,
            )
            return 2
        buyers = await run_buyer_harvest(
            city=args.city,
            agency_type=args.agency_type,
            limit=max(1, args.limit),
            buyer_db=buyer_db,
        )
        if args.export_csv:
            csv_text = buyer_db.export_outreach_csv(
                buyers, lead_db=db, min_score=args.min_score
            )
            out_path = Path(args.export_csv)
            out_path.write_text(csv_text, encoding="utf-8")
            print(f"Zapisano eksport Buyer Outreach CSV ({len(buyers)} agencji) -> {out_path.resolve()}")
        if args.json:
            print(json.dumps({"kpis": buyer_db.compute_kpis(buyers), "buyers": buyers}, ensure_ascii=False, indent=2))
        else:
            print_buyer_summary(buyers)
        return 0

    if args.list_buyers:
        buyers = buyer_db.list_buyers(
            agency_type=None if args.agency_type == "Wszystkie" else args.agency_type
        )
        if args.export_csv:
            csv_text = buyer_db.export_outreach_csv(
                buyers, lead_db=db, min_score=args.min_score
            )
            out_path = Path(args.export_csv)
            out_path.write_text(csv_text, encoding="utf-8")
            print(f"Zapisano eksport Buyer Outreach CSV ({len(buyers)} agencji) -> {out_path.resolve()}")
        if args.json:
            print(json.dumps({"kpis": buyer_db.compute_kpis(buyers), "buyers": buyers}, ensure_ascii=False, indent=2))
        else:
            print_buyer_summary(buyers)
        return 0

    if args.generate_offer:
        buyer = buyer_db.get_buyer(args.generate_offer.strip().lower())
        if not buyer:
            print(f"Błąd: nie znaleziono agencji '{args.generate_offer}' w bazie. Użyj najpierw --find-buyers.", file=sys.stderr)
            return 2
        offer = OutreachOfferGenerator().generate(
            buyer=buyer, lead_db=db, min_score=args.min_score
        )
        if args.json:
            print(json.dumps(offer, ensure_ascii=False, indent=2))
        else:
            print("\n" + "=" * 92)
            print(f" OFERTA TEASER-FIRST DLA: {offer['agency_name']} ({offer['recipient_email'] or 'brak e-maila'})")
            print("=" * 92)
            print(f"TEMAT:\n{offer['subject']}\n")
            print(f"COLD MAIL:\n{offer['email_body']}\n")
            print(f"LINKEDIN DM:\n{offer['linkedin_dm']}\n")
        return 0

    # --- Lead Engine CLI modes ---
    results: list[dict[str, Any]] = []

    if args.url:
        auditor = SignalAuditorAgent()
        record = await auditor.audit(
            raw_url=args.url,
            city=args.city,
            industry=args.industry,
        )
        saved = db.upsert_lead(record)
        results = [mask_lead_for_teaser(saved) if args.teaser else saved]
    elif args.harvest:
        if args.city not in CITIES:
            print(
                f"Błąd: nieznane miasto '{args.city}'. Dostępne: {', '.join(CITIES.keys())}",
                file=sys.stderr,
            )
            return 2
        if args.industry not in INDUSTRIES:
            print(
                f"Błąd: nieznana branża '{args.industry}'. Dostępne: {', '.join(INDUSTRIES)}",
                file=sys.stderr,
            )
            return 2
        audited = await run_harvest_and_audit(
            city=args.city,
            industry=args.industry,
            limit=max(1, args.limit),
            db=db,
        )
        results = [
            mask_lead_for_teaser(l) if args.teaser else l
            for l in audited
            if l["lead_score"] >= args.min_score
        ]
    elif args.list or args.export_csv:
        results = db.list_leads(
            min_score=args.min_score,
            teaser=args.teaser,
        )
    else:
        build_parser().print_help()
        return 0

    if args.export_csv:
        csv_text = db.export_csv(results, teaser=args.teaser)
        out_path = Path(args.export_csv)
        out_path.write_text(csv_text, encoding="utf-8")
        print(f"Zapisano eksport CSV ({len(results)} leadów) -> {out_path.resolve()}")

    if args.json:
        payload = {
            "mode": "teaser" if args.teaser else "full",
            "kpis": db.compute_kpis(results),
            "leads": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_lead_summary(results, teaser=args.teaser)

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
