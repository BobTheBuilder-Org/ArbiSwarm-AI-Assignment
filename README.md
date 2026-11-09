# ArbiSwarm

Multi-market e-commerce arbitrage scanner and deal evaluation engine for Malaysian and international marketplaces.

ArbiSwarm searches across Carousell, Lazada, Mudah, Shopee Malaysia, and optional eBay/Etsy endpoints to identify price discrepancies and profitable resale opportunities. It combines fast deterministic rules (URL validation, brand/variant filtering, fee calculations) with targeted LLM assistance (query intent planning, batch description analysis, and image comparison via Google Gemini).

---

## Key Features

- **Multi-Marketplace Search**: Queries Carousell, Mudah, Lazada, and Shopee concurrently, with optional eBay and Etsy integration for international price comparison.
- **Hybrid Intelligence Architecture**:
  - **Deterministic safety gates**: Strict LEGO set number matching, variant classification, landed cost verification, and platform fee calculations.
  - **Selective LLM calls**: Query expansion, batch evidence extraction (grounded in direct listing quotes), and photo comparison.
- **Variant Cohort Clustering**: Automatically groups listings by variant, edition, and condition (e.g. distinguishing complete sets from minifigures or blind boxes) to derive accurate resale price medians.
- **Arbi Lens (Visual Search)**: Upload product photos to identify items and compare candidate listing images in a single bounded visual batch.
- **Deal Copilot (Interactive Assistant)**: Conversational assistant for filtering results, comparing offers across platforms, explaining fee breakdowns, and drafting seller negotiation messages.
- **Robust Quota & Error Handling**: Local RPM/RPD rate limiting, multi-key rotation, timeout budgets, and graceful fallback to offline heuristic matching when API limits are reached.

---

## Architecture

```text
User Search / Photo / Chat
            │
            ▼
┌───────────────────────────────┐
│     Adaptive Controller       │  <── Local query parsing or Gemini planner
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│   Concurrent Market Search    │  <── Carousell, Mudah, Lazada, Shopee, eBay, Etsy
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│    Deterministic Filters      │  <── URL validation, brand/variant/model matching, price caps
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│      Evidence Verification    │  <── Bounded Gemini batch (text or photo comparison)
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Variant Clustering & Pricing │  <── Median asking prices, fee calculations, risk scoring
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│    FastAPI API & Dashboard    │  <── Interactive web UI + Telegram alerts
└───────────────────────────────┘
```

The system uses a bounded batch controller: routine searches evaluate shortlisted candidates in a single batched LLM call with local caching, rather than executing slow, per-listing sequential chains.

---

## Getting Started

### Prerequisites
- Python 3.10+
- Node.js 20+ (for JavaScript tests)
- (Optional) Google Gemini API key for AI features
- (Optional) ReefAPI / Nexscope / eBay / Etsy keys for live scraping

### Installation

```bash
# Clone repository
git clone https://github.com/username/ArbiSwarm.git
cd ArbiSwarm

# Create and activate virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install dependencies and Playwright browser binaries
pip install -r requirements.txt
playwright install chromium

# Set up local configuration
cp .env.example .env
```

### Running the Application

To launch the FastAPI backend and web interface:
```bash
uvicorn api:app --reload
```
Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

Alternatively, to run the Streamlit test client:
```bash
streamlit run app.py
```

---

## Configuration & Environment Variables

ArbiSwarm works out-of-the-box using the bundled demo dataset and local heuristic matching even without external API credentials. To configure live scrapers and AI capabilities, edit `.env`:

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Primary Gemini API key | `""` (offline mode) |
| `GEMINI_API_KEYS` | Additional comma-separated keys for load distribution | `""` |
| `GEMINI_MODEL` | Primary model for batch analysis | `gemini-3.8-flash` |
| `GEMINI_FALLBACK_MODEL` | Fallback model for retry on transient errors | `gemini-3.6-flash` |
| `GEMINI_THINKING_LEVEL` | Reasoning effort (`low`, `medium`, `high`) | `low` |
| `GEMINI_LOCAL_RPM` | Local rate limit (requests per minute) | `5` |
| `GEMINI_LOCAL_RPD` | Local daily request limit | `20` |
| `REEF_API_KEY` | API key for Carousell & Mudah live retrieval | `""` |
| `NEXSCOPE_API_KEY` | API key for Shopee product search | `""` |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | Official eBay Browse API credentials | `""` |
| `ETSY_API_KEY` / `ETSY_SHARED_SECRET` | Official Etsy v3 API credentials | `""` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram bot credentials for buy alerts | `""` |
| `PLATFORM_FEE_PCT` | Estimated marketplace fee on resale | `0.05` (5%) |
| `SHIPPING_COST_MYR` | Default domestic shipping cost | `8.0` (RM8) |
| `MIN_MARGIN_PCT_TO_ALERT` | Minimum profit margin to trigger a buy alert | `20.0` (20%) |

### Multiple Key Rotation
Numbered slots can be assigned to dedicate credentials across roles:
```dotenv
GEMINI_API_KEY=primary_manager_key
GEMINI_API_KEY_2=analyst_key
GEMINI_API_KEY_3=vision_key
GEMINI_API_KEY_4=reviewer_key
GEMINI_API_KEY_5=chat_key
```

### Checking Setup & Quota
To test your environment configuration and verify Gemini connectivity:
```bash
# Verify local configuration without making provider requests
python scripts/check_gemini.py

# Send a single test verification request (consumes 1 quota unit)
python scripts/check_gemini.py --live
```

---

## Data Retrieval Modes

1. **Live Market**:
   - **Carousell & Mudah**: Handled via ReefAPI when configured; falls back to headless Chromium.
   - **Lazada**: Fetches public catalog JSON; falls back to browser rendering if challenged.
   - **Shopee**: Queries Nexscope API when configured; falls back to browser search.
2. **International Markets (eBay & Etsy)**:
   - Searches fixed-price items delivering to Malaysia.
   - Foreign currencies are converted to MYR using live reference rates ([Frankfurter API](https://frankfurter.dev/v1/)).
   - International items are marked for landed cost review (import duties, international shipping, and FX fees require checkout confirmation).
3. **Demo Snapshot**:
   - Bundled dataset (`cache/listings_raw.json`) containing `Labubu Macaron` listings for offline testing and reproducible demos.

---

## Arbi Lens (Photo Search)

Search by visual reference:
1. **Upload & Preprocess**: Upload JPG, PNG, or WebP images up to 6 MB. Transposes EXIF orientation, downsizes to max 1024px, and cleans metadata.
2. **Identification**: Calls Gemini Vision to extract product names, categories, and bounding boxes.
3. **Candidate Comparison**: Retrieves marketplace candidates and compares up to 6 listing images against the reference in a single batch.
4. **Categorization**: Classifies offers as `likely_match`, `similar`, `different`, or `unverified`.



## API Usage

### Search Endpoint

```bash
curl -X POST http://127.0.0.1:8000/api/search \
  -H 'content-type: application/json' \
  -d '{
    "query": "Labubu Macaron",
    "pricing_mode": "auto",
    "max_purchase_price": 60,
    "source_mode": "live",
    "marketplaces": ["carousell", "lazada", "mudah", "shopee"]
  }'
```

### Health & Runtime Status
- `GET /api/health`: Returns API health, data source configuration, and AI availability.
- `GET /api/agents/runtime`: Returns scheduler stats, key slots, and active/recent execution runs.

---

## Testing

Run the test suite across backend and frontend components:

```bash

# Python unit tests
python -m unittest discover -s tests -v

# Specific test suites
python -m unittest tests.test_core
python -m unittest tests.test_copilot
python -m unittest tests.test_matching
python -m unittest tests.test_gemini_reliability

# Frontend / Node.js test suites
node --test tests/chat-state.test.cjs
node --test tests/request-deadlines.test.cjs
node --test tests/watch-state.test.cjs
```
