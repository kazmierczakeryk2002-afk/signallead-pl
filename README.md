# SignalLead PL — Autonomiczny Silnik Leadów Sygnałowych B2B

**SignalLead PL** to wieloagentowy system do wyszukiwania polskich firm B2B z kosztownymi lukami technicznymi, SEO i marketingowymi na stronie WWW, ich wyceny oraz sprzedaży agencjom w modelu **Teaser-First** (darmowa zamaskowana próbka Demo → płatność → odblokowanie pełnej paczki CSV/JSON).

---

## Architektura MVP

| Moduł | Rola |
|---|---|
| `engine.py` | **Silnik wieloagentowy:** `HarvesterAgent` (OpenStreetMap Overpass API + polska pula domen B2B), `SignalAuditorAgent` (audyt live strony głównej + `/kontakt`, `/o-nas`, ekstrakcja kontaktów i matematyczna walidacja NIP, scoring 0–99 pkt, klasyfikacja kupca, wycena leada i kontraktu, generator polskiego Icebreakera), `LeadDatabase` (SQLite, maskowanie Teaser, eksport CSV UTF-8-SIG). |
| `buyer_finder.py` | **Moduł Buyer Finder (Etap 2 · Moduł 1):** `AgencyHarvesterAgent` (wyszukiwanie polskich agencji SEO/SEM, Performance 360, Digital i Software House'ów + skan live usług, e-maili, telefonów, NIP i LinkedIn), `OutreachOfferGenerator` (generator spersonalizowanych ofert Teaser-First — Cold Mail + LinkedIn DM + link `/pack/{id}`), `BuyerDatabase` (CRM statusów outreach w SQLite + eksport CSV). |
| `pack_store.py` + `pack_page.html` | **Moduł Pack Store & Stripe Billing (Etap 2 · Moduł 2):** `PackDatabase` (zamrożone paczki na wyłączność 1:1, atomowa rezerwacja, wycena dynamiczna `-25%` + korekta ręczna PLN bez VAT, bezpieczny `download_token`), `StripeBillingService` (Stripe Checkout: BLIK + Karta + Przelewy24, zbieranie NIP i danych do faktury, webhooki + bezpieczny tryb Sandbox), publiczna strona sprzedażowa `/pack/{id}`. |
| `app.py` | **Backend FastAPI:** REST API (`/api/leads`, `/api/buyers/*`, `/api/packs/*`, `/api/webhooks/stripe`, `/pack/{id}`, `/health`) + serwowanie panelu webowego. |
| `dashboard.html` | **Panel Webowy (Tailwind Dark UI):** 3 zakładki (**Leady Sygnałowe** + **Buyer Finder** + **Paczki & Sprzedaż**), KPI paczek i przychodu, przełącznik **PEŁNE DANE vs DEMO / TEASER**, skanery live, CRM agencji, generator ofert i kreator paczek sprzedażowych. |
| `cli.py` | **Narzędzie wsadowe CLI:** uruchamianie audytów, harvestera leadów, Buyer Findera, generatora ofert i eksportu CSV z terminala. |
| `Dockerfile` | Gotowy obraz produkcyjny (Python 3.12-slim) pod Railway, Render, Fly.io lub własny VPS. |

---

## Wykrywane sygnały zakupowe

- **Web Dev Signals:** brak SSL/HTTPS, wolny czas odpowiedzi serwera (`TTFB > 1400 ms`), brak responsywności mobilnej (`<meta name="viewport">`), detekcja CMS (`WordPress`, `WooCommerce`, `PrestaShop`, `Shoper`, `Wix`, `Joomla Legacy`, `Next.js`).
- **SEO Signals:** brak `<meta name="description">`, brak lub wielokrotne nagłówki `<h1>`, brak danych strukturalnych `Schema.org` (`application/ld+json`).
- **Ads / Marketing Signals:** brak Google Tag Manager (`GTM`), brak Google Analytics 4 (`GA4`), brak `Meta Pixel`, brak `TikTok Pixel`.
- **Dane B2B (ze strony głównej + `/kontakt` + `/o-nas`):** matematycznie walidowany polski **NIP** (wagi `6,5,7,2,3,4,5,6,7` mod 11), e-maile B2B (bez domen śmieciowych/technicznych), telefony `+48 XXX XXX XXX`, linki Social Media (FB/IG/LinkedIn).

---

## Szybki start lokalnie

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1. Zasilić bazę testową z CLI (przykład: Warszawa / Kancelarie)
python cli.py --harvest --city "Warszawa" --industry "Kancelarie Prawne/Podatkowe" --limit 5

# 2. Znaleźć agencje (Buyer Finder) w tym samym mieście
python cli.py --find-buyers --city "Warszawa" --limit 5

# 3. Uruchomić panel webowy FastAPI
uvicorn app:app --host 0.0.0.0 --port 8000
```

Panel dostępny pod adresem `http://localhost:8000`.

---

## Przykłady użycia CLI

```bash
# Audyt pojedynczej polskiej domeny na żywo
python cli.py --url https://www.dzp.pl --city Warszawa --industry "Kancelarie Prawne/Podatkowe"

# Batch harvest po mieście i branży
python cli.py --harvest --city Kraków --industry "Kliniki Medyczne/Stomatologia" --limit 6

# Eksport paczki w trybie DEMO / TEASER (do wysyłki agencji jako darmowa próbka)
python cli.py --list --teaser --min-score 55 --export-csv demo_krakow.csv

# Eksport pełnej paczki po opłaceniu przez klienta
python cli.py --list --min-score 55 --export-csv pelna_paczka_krakow.csv

# Buyer Finder — wyszukanie polskich agencji + eksport Outreach CSV z gotowymi mailami
python cli.py --find-buyers --city Kraków --limit 5 --export-csv outreach_agencje_krakow.csv

# Wygenerowanie pojedynczej oferty Teaser-First dla wybranej agencji
python cli.py --generate-offer delante.pl
```

---

## Konfiguracja Stripe (BLIK · Karta · Przelewy24 · PLN bez VAT)

Bez ustawionych zmiennych środowiskowych aplikacja uruchamia się w bezpiecznym **Trybie Testowym (Sandbox)**, umożliwiając przetestowanie całego przepływu zakupu i odblokowania CSV lokalnie.

Aby przełączyć sklep `/pack/{id}` na prawdziwe płatności Stripe:

```bash
export STRIPE_SECRET_KEY="sk_live_..."          # lub sk_test_... na start
export STRIPE_WEBHOOK_SECRET="whsec_..."        # z Stripe Dashboard -> Webhooks
export PUBLIC_BASE_URL="https://twojadomena.pl" # adres publiczny do redirectów i linków w mailach
```

W panelu Stripe utwórz Webhook wskazujący na `POST https://twojadomena.pl/api/webhooks/stripe` z nasłuchem zdarzeń:
- `checkout.session.completed` (automatyczne oznaczenie paczki jako `sold`, blokada dla innych agencji, wygenerowanie `download_token`, zapis NIP/danych do faktury i zmiana statusu agencji w CRM na `Opłacona`),
- `checkout.session.expired` (zwolnienie 20-minutowej rezerwacji, jeśli kupujący przerwał płatność).

---

## API Reference

| Metoda | Endpoint | Opis |
|---|---|---|
| `GET` | `/` | Panel Webowy Dashboard (Leady Sygnałowe + Buyer Finder + Paczki & Sprzedaż) |
| `GET` | `/pack/{pack_id}` | Publiczna strona sprzedażowa paczki na wyłączność (podgląd Teaser + zakup) |
| `GET` | `/pack/{pack_id}/success` | Strona po płatności z odblokowanym pobieraniem pełnego CSV/JSON |
| `GET` | `/health` | Healthcheck dla Railway / Render / Fly.io |
| `GET` | `/api/meta` | Lista obsługiwanych miast, branż, typów agencji, statusów CRM i trybu Stripe |
| `GET` | `/api/leads?min_score=0&teaser=false` | Lista leadów + KPI (obsługuje `city`, `industry`, `buyer_persona`, `search`) |
| `POST` | `/api/audit` | Audyt pojedynczej domeny na żywo (`{"url": "...", "city": "...", "industry": "..."}`) |
| `POST` | `/api/harvest` | Batch harvest + audyt (`{"city": "Warszawa", "industry": "...", "limit": 5}`) |
| `GET` | `/api/export/csv?teaser=true\|false` | Pobierz paczkę CSV (UTF-8-SIG, gotową pod polski Excel) |
| `GET` | `/api/export/json?teaser=true\|false` | Pobierz paczkę JSON z KPI |
| `GET` | `/api/buyers` | Lista agencji (Buyer Finder) + KPI outreach |
| `POST` | `/api/buyers/harvest` | Wyszukaj i wzbogać polskie agencje (`{"city": "Kraków", "limit": 5}`) |
| `POST` | `/api/buyers/add` | Dodaj i przeskanuj pojedynczą agencję na żywo |
| `POST` | `/api/buyers/{domain}/offer` | Wygeneruj spersonalizowaną ofertę Teaser-First (Cold Mail + LinkedIn DM + link `/pack/{id}`) |
| `PATCH` | `/api/buyers/{domain}/status` | Zaktualizuj status CRM (`new`, `teaser_sent`, `replied`, `paid`, `rejected`) |
| `GET` | `/api/buyers/export/csv` | Eksport agencji wraz z gotowymi cold mailami i DM (UTF-8-SIG CSV) |
| `GET` / `POST` | `/api/packs` | Lista paczek sprzedażowych + KPI przychodu / Utworzenie paczki z korektą ceny |
| `PATCH` / `DELETE` | `/api/packs/{pack_id}/price` | Zmiana ceny dostępnej paczki / Usunięcie niesprzedanej paczki |
| `POST` | `/api/packs/{pack_id}/checkout` | Utworzenie sesji Stripe Checkout (PLN, BLIK/Karta/P24, NIP, wyłączność) |
| `GET` | `/api/packs/{pack_id}/download/csv?token=...` | Pobranie pełnego pliku CSV po walidacji `download_token` |
| `POST` | `/api/webhooks/stripe` | Webhook Stripe obsługujący `checkout.session.completed` i `checkout.session.expired` |

---

## Wdrożenie 1 kliknięciem (Docker / Railway / Render / Fly.io)

### Docker
```bash
docker build -t signallead-pl .
docker run -d -p 8000:8000 -v $(pwd)/data:/app/data -e SIGNALLEAD_DB_PATH=/app/data/signallead.db signallead-pl
```

### Railway / Render / Fly.io
1. Podłącz to repozytorium GitHub.
2. Wybierz build z `Dockerfile` (zmienna środowiskowa `PORT` jest obsługiwana automatycznie).
3. Opcjonalnie podepnij wolumen trwały (Volume) i ustaw `SIGNALLEAD_DB_PATH=/data/signallead.db`.
