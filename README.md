# SignalLead PL — Autonomiczny Silnik Leadów Sygnałowych B2B

**SignalLead PL** to wieloagentowy system do wyszukiwania polskich firm B2B z kosztownymi lukami technicznymi, SEO i marketingowymi na stronie WWW, ich wyceny oraz sprzedaży agencjom w modelu **Teaser-First** (darmowa zamaskowana próbka Demo → płatność → odblokowanie pełnej paczki CSV/JSON).

---

## Architektura MVP

| Moduł | Rola |
|---|---|
| `engine.py` | **Silnik wieloagentowy:** `HarvesterAgent` (OpenStreetMap Overpass API + polska pula domen B2B), `SignalAuditorAgent` (audyt live strony głównej + `/kontakt`, `/o-nas`, ekstrakcja kontaktów i matematyczna walidacja NIP, scoring 0–99 pkt, klasyfikacja kupca, wycena leada i kontraktu, generator polskiego Icebreakera), `LeadDatabase` (SQLite, maskowanie Teaser, eksport CSV UTF-8-SIG). |
| `app.py` | **Backend FastAPI:** REST API (`/api/leads`, `/api/audit`, `/api/harvest`, `/api/export/csv`, `/api/export/json`, `/health`) + serwowanie panelu webowego. |
| `dashboard.html` | **Panel Webowy (Tailwind Dark UI):** KPI paczki (w tym cena pakietowa `-25%` i pipeline kontraktów), przełącznik **PEŁNE DANE vs DEMO / TEASER**, skaner pojedynczej domeny, harvester miast, podgląd mini-audytu i kopiowanie Icebreakera 1 kliknięciem. |
| `cli.py` | **Narzędzie wsadowe CLI:** uruchamianie audytów, harvestera i eksportu CSV z terminala lub crona. |
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

# 2. Uruchomić panel webowy FastAPI
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
```

---

## API Reference

| Metoda | Endpoint | Opis |
|---|---|---|
| `GET` | `/` | Panel Webowy Dashboard |
| `GET` | `/health` | Healthcheck dla Railway / Render / Fly.io |
| `GET` | `/api/meta` | Lista obsługiwanych miast, branż i profili kupców |
| `GET` | `/api/leads?min_score=0&teaser=false` | Lista leadów + KPI (obsługuje `city`, `industry`, `buyer_persona`, `search`) |
| `POST` | `/api/audit` | Audyt pojedynczej domeny na żywo (`{"url": "...", "city": "...", "industry": "..."}`) |
| `POST` | `/api/harvest` | Batch harvest + audyt (`{"city": "Warszawa", "industry": "...", "limit": 5}`) |
| `GET` | `/api/export/csv?teaser=true\|false` | Pobierz paczkę CSV (UTF-8-SIG, gotową pod polski Excel) |
| `GET` | `/api/export/json?teaser=true\|false` | Pobierz paczkę JSON z KPI |

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
