# Checklista wdrożenia produkcyjnego 24/7 — SignalLead PL

Ten przewodnik przeprowadzi Cię krok po kroku od repozytorium GitHub do działającej 24/7 firmy z prawdziwymi płatnościami **BLIK / Karta / Przelewy24** (Stripe) oraz automatyczną wysyłką ofert z załącznikiem **Demo CSV** (Resend / SMTP).

---

## Krok 1 — Wybór chmury i podpięcie repozytorium (ok. 5 min)

### Opcja A: Railway (najszybsza i bardzo wygodna z wolumenem SQLite)
1. Wejdź na [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo** → wybierz `kazmierczakeryk2002-afk/signallead-pl`.
2. Railway automatycznie wykryje `Dockerfile` i `railway.json` (z healthcheckiem `/health`).
3. W zakładce **Volumes** kliknij **Add Volume** i ustaw ścieżkę montowania (Mount Path): **`/data`**.
4. W zakładce **Settings → Networking** kliknij **Generate Domain** (lub podepnij własną domenę, np. `app.signallead.pl`).

### Opcja B: Render (1-klik przez Blueprint `render.yaml`)
1. Wejdź na [render.com](https://render.com) → **New → Blueprint** → podłącz repozytorium `signallead-pl`.
2. Render automatycznie wczyta `render.yaml` (region **Frankfurt EU**, dysk trwały 1 GB w `/data`, automatyczne hasło `ADMIN_PASSWORD`).

---

## Krok 2 — Zmienne środowiskowe i bezpieczeństwo panelu (ok. 3 min)

W ustawieniach **Variables / Environment** wklej zmienne na bazie `.env.example`:

| Zmienna | Wartość | Po co? |
|---|---|---|
| `SIGNALLEAD_DB_PATH` | `/data/signallead.db` | Trwały zapis bazy między restartami/deployami |
| `PUBLIC_BASE_URL` | `https://twojadomena.pl` | Poprawne linki `/pack/{id}` w mailach i powroty ze Stripe |
| `ADMIN_USER` | `admin` | Login do panelu głównego (`/`) |
| `ADMIN_PASSWORD` | *(silne hasło)* | **Krytyczne!** Chroni Twój panel `/` i pełną bazę leadów przed osobami z zewnątrz. Strony sprzedażowe `/pack/{id}`, pobieranie CSV z tokenem i webhook Stripe pozostają publiczne dla klientów. |

> **Weryfikacja bezpieczeństwa:** Po wdrożeniu otwórz `https://twojadomena.pl/` w trybie incognito — przeglądarka zapyta o login i hasło. Otwarcie linku paczki `https://twojadomena.pl/pack/...` zadziała bez logowania.

---

## Krok 3 — Podpięcie Stripe (PLN bez VAT · BLIK · Karta · Przelewy24) (ok. 10 min)

1. Zaloguj się do [dashboard.stripe.com](https://dashboard.stripe.com) (najpierw możesz przejść ten krok w **Test Mode**, a po 1 próbnym zakupie przełączyć na **Live**).
2. W **Settings → Payment methods** włącz:
   - **BLIK**
   - **Przelewy24 (P24)**
   - **Cards (Karty)**
3. W **Developers → API keys** skopiuj **Secret key** (`sk_test_...` lub `sk_live_...`) → zapisz w Railway/Render jako `STRIPE_SECRET_KEY`.
4. W **Developers → Webhooks → Add endpoint**:
   - **Endpoint URL:** `https://twojadomena.pl/api/webhooks/stripe`
   - **Events to listen to:** zaznacz:
     - `checkout.session.completed`
     - `checkout.session.expired`
   - Po utworzeniu skopiuj **Signing secret** (`whsec_...`) → zapisz jako `STRIPE_WEBHOOK_SECRET`.
5. *(Wskazówka do faktur bez VAT):* W **Settings → Billing → Invoices** możesz dodać swoją stopkę rachunku (np. dane sprzedawcy i podstawę zwolnienia z VAT / działalność nierejestrowaną). Wszystkie dane nabywcy (Nazwa, NIP, Adres, E-mail) i tak zapisują się dodatkowo w tabeli **Paczki & Sprzedaż** w Twoim panelu.

---

## Krok 4 — Podpięcie Automatu Wysyłkowego (Resend API — zalecane) (ok. 10 min)

1. Załóż darmowe konto na [resend.com](https://resend.com) (3 000 maili/mies. za darmo, nie wymaga otwartych portów SMTP).
2. W **Domains → Add Domain** dodaj swoją domenę (lub dedykowaną subdomenę/domenę pod cold mail, żeby chronić główną domenę) i wklej 3 rekordy DNS (**SPF, DKIM, DMARC**) u swojego rejestratora (OVH / Cloudflare / LH.pl / cyber_Folks).
3. W **API Keys** wygeneruj klucz (`re_...`) i ustaw zmienne w Railway/Render:
   - `RESEND_API_KEY=re_...`
   - `MAIL_FROM=Eryk <kontakt@twojadomena.pl>`
   - `MAIL_REPLY_TO=kontakt@twojadomena.pl`
   - `DAILY_SEND_LIMIT=15` *(na pierwszy tydzień rozgrzewania domeny zacznij od 10–15 maili/dzień, potem podnieś do 20–30)*
   - `OUTREACH_COOLDOWN_DAYS=7`

*(Alternatywnie zamiast Resend możesz podać `SMTP_HOST`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD` dla skrzynki na Zoho / Google Workspace / OVH / cyber_Folks.)*

---

## Krok 5 — Checklista pierwszego uruchomienia (pierwsza sprzedaż)

1. Zaloguj się do panelu `https://twojadomena.pl/` (login `ADMIN_USER`, hasło `ADMIN_PASSWORD`).
2. W zakładce **Leady Sygnałowe** przeskanuj 1 wybrane miasto + 1 dochodową branżę (np. **Kraków → Kliniki Medyczne/Stomatologia** albo **Warszawa → Kancelarie**, limit 8–12 firm).
3. Kliknij **Utwórz paczkę sprzedażową →** (zakładka **Paczki & Sprzedaż**), sprawdź sugerowaną cenę (`-25%`) i ewentualnie zaokrąglij (np. `299 PLN` lub `349 PLN` na start pod pierwszą konwersję).
4. W zakładce **Buyer Finder** wyszukaj 5–8 agencji z tego samego miasta.
5. Wyślij najpierw **1 testowy mail na własny adres** (dodaj swoją domenę testowo lub sprawdź podgląd w modalu), sprawdź załącznik CSV w Excelu i link `/pack/{id}`.
6. Wyślij serię do 5 pierwszych agencji (każda dostanie krótki, ludzki mail z załącznikiem Demo CSV i linkiem do odblokowania paczki BLIK-iem na wyłączność).
