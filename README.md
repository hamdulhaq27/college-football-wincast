# 🏈 College WinCast — College Football Win Probability & Value Betting Platform

**College WinCast** is a comprehensive interactive dashboard built with [Streamlit](https://streamlit.io) that fetches real-time college football data, computes calibrated win probabilities, predicts spreads and totals, analyzes value betting opportunities, and exports results to Excel or Google Sheets.

---

## Table of Contents

1. [Overall Architecture](#overall-architecture)
2. [How to Run](#how-to-run)
3. [File-by-File Explanation](#file-by-file-explanation)
4. [How to Deploy on Vercel](#how-to-deploy-on-vercel)
5. [Further Work](#further-work)

---

## Overall Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Streamlit Dashboard (main.py)            │
│  Sidebar: Season/Week params, Team filters, Weather        │
│  override, Google Sheets controls                           │
│  Main Panel: Predictions table, charts, Value Bets,        │
│  Calibration, Export buttons                                │
└──────────────────────┬──────────────────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
┌─────────────────┐ ┌─────────┐ ┌─────────────┐
│  fetch_cfb.py   │ │fetch_   │ │  compute_    │
│  CFBD /games    │ │weather  │ │  winprob.py  │
│  endpoints      │ │.py      │ │  LogReg      │
└────────┬────────┘ │Open-    │ └──────┬───────┘
         │          │Meteo +  │        │
         ▼          │Weather  │        ▼
┌─────────────────┐ │API      │ ┌─────────────┐
│ fetch_advanced_ │ │         │ │ compute_     │
│ stats.py        │ └─────────┘ │ spread.py    │
│ SP+, FPI, EPA   │             │ Linear Reg   │
└────────┬────────┘             └──────┬───────┘
         │                             │
         ▼                             ▼
┌─────────────────┐             ┌─────────────┐
│ odds_value_     │             │ model_       │
│ analysis.py     │             │ calibration  │
│ EV computation  │             │ .py          │
└────────┬────────┘             │ Backtesting  │
         │                      └─────────────┘
         ▼
┌─────────────────┐
│ export_data.py  │
│ Excel / Google  │
│ Sheets export   │
└─────────────────┘
```

**Data Flow:**

1. **User Configures Parameters** → Season, weeks, teams, weather override
2. **`fetch_games_cfbd()`** → Pulls game data from [CFBD API](https://collegefootballdata.com) (`/games` endpoint)
3. **`enrich_with_weather()`** → Resolves venue coordinates → fetches weather from Open-Meteo / WeatherAPI
4. **`fetch_advanced_team_metrics()`** → Pulls SP+ ratings, FPI ratings, EPA/play, Success Rate, Havoc Rate
5. **Massey Ratings (optional)** → Loads or builds Massey-like composite ratings from SP+ and FPI
6. **`add_winprob_column()`** → Logistic regression using calibrated coefficients → Win Probability %
7. **`compute_spread()`** → Linear regression using calibrated coefficients → Spread & Total predictions
8. **`evaluate_value_bets()`** → Merges odds from CFBD `/lines` → Computes Expected Value → Flags value bets
9. **Dashboard Display** → Interactive tables, Plotly scatter charts, styled dataframes
10. **Export** → Excel (.xlsx) or Google Sheets (OAuth / Service Account)

---

## How to Run

### Prerequisites

- **Python 3.10 or 3.11** (recommended)
- A **CFBD API key** (free tier): [Get one here](https://collegefootballdata.com/key)
- (Optional) A **WeatherAPI.com** key for premium weather data
- (Optional) Google OAuth Desktop client secrets for Google Sheets export

### Quick Start (Windows — Batch File)

Double-click **`FieldMetrics.bat`** — this script will:

1. Create a Python 3.11 virtual environment (if missing)
2. Install dependencies from `requirements.txt`
3. Launch the Streamlit app

### Option A: Run Without Virtual Environment (Simplest — No venv)

If you don't want to deal with virtual environments, you can install packages **globally** and run directly:

```powershell
# 1. Navigate to the project folder
cd C:\Users\PC\Downloads\college_football_project

# 2. Delete the broken venv folder (from the old location)
Remove-Item -Recurse -Force venv

# 3. Install dependencies directly (globally)
pip install -r requirements.txt

# 4. Run the app
streamlit run main.py
```

No activation steps needed. Just install and run.

### Option B: With Virtual Environment (Recommended for isolation)

Use a venv to keep dependencies separate from other Python projects:

```powershell
# 1. Navigate to the project folder
cd C:\Users\PC\Downloads\college_football_project

# 2. Delete any OLD virtual environment (if project was moved)
Remove-Item -Recurse -Force venv   # PowerShell

# 3. Create a FRESH virtual environment
python -m venv venv

# 4. Activate it (PowerShell)
.\venv\Scripts\Activate.ps1

# 5. Install dependencies
pip install -r requirements.txt

# 6. Run the app
streamlit run main.py
```

> **⚠️ "Fatal error in launcher" fix:**
> If you moved the project folder after creating a venv, you get:
> ```
> Fatal error in launcher: Unable to create process using '"OLD_PATH\venv\Scripts\python.exe" ...
> ```
> **Fix:** Delete venv (`Remove-Item -Recurse -Force venv`) and recreate it.

### Environment Variables (`.env`)

Create a `.env` file in the project root:

```ini
# Required: CFBD API key
CFBD_API_KEY=your_cfbd_api_key_here

# Optional: Secondary CFBD key for rotation
CFBD_API_KEY_2=your_second_key_here

# Optional: WeatherAPI.com key
WEATHERAPI_KEY=your_weatherapi_key_here

# Optional: Google Sheets - Service Account JSON path
GOOGLE_SERVICE_ACCOUNT_JSON=config/credentials.json

# Optional: Google Sheets - OAuth client secrets path
GOOGLE_OAUTH_CLIENT_SECRETS=config/client_secret.json

# Optional: Share exported sheets with this email
GOOGLE_SHARE_WITH=your.email@gmail.com

# Optional: Massey ratings CSV auto-download URL template
MASSEY_URL_TEMPLATE=https://example.com/massey_{season}.csv

# Optional: Calibration season window
CALIB_SEASON_START=2018
CALIB_SEASON_END=2024
```

---

## File-by-File Explanation

### Root Files

| File | Description |
|------|-------------|
| **`main.py`** | **Main Streamlit application** (~1322 lines). Contains the entire UI logic: sidebar parameters, main data pipeline (fetch → enrich → compute → display), calibration UI, Google OAuth connect/disconnect flow, and export buttons. Defines fallback inline calibration if `model_calibration.py` fails. |
| **`requirements.txt`** | All Python dependencies: Streamlit 1.37, pandas, numpy, scikit-learn, plotly, requests, gspread, google-auth, openpyxl, python-dotenv, etc. |
| **`.env`** | Environment variables (API keys, paths). **Do not commit** — contains secrets. |
| **`FieldMetrics.bat`** | Windows batch script for one-click setup: creates venv, installs deps, kills stale Streamlit processes, launches the app. |
| **`winCast_debug.log`** | Debug log file generated at runtime by the logging system. |

### Data Files (in `data/`)

| File | Description |
|------|-------------|
| **`model_params.json`** | **Calibrated model coefficients** — logistic regression (intercept, coefficient for SP/FPI/HomeAdv/Massey) and linear regression (spread/total coefficients). Generated by calibration. |
| **`weather_cache.json`** | Cached weather API responses (keyed by `lat,lon|YYYY-MM-DD`) to avoid redundant API calls. |
| **`venues_cache.json`** | Cached venue coordinate lookups (venue_id → lat/lon) from CFBD `/venues`. |
| **`college_results_*.xlsx`** | Exported prediction Excel files (timestamped). |
| **`predictions_*.xlsx`** | Prediction snapshots from the export button. |
| **`snapshot_*.xlsx`** | Multi-tab snapshot workbooks. |
| **`massey/`** | Directory containing per-season Massey rating CSV files (`massey_<season>.csv`). |

### Config Files (in `config/`)

| File | Description |
|------|-------------|
| **`client_secret.json`** | Google OAuth Desktop client credentials (JSON). Used for user-based Google Sheets authentication. |
| **`credentials.json`** | Google Service Account credentials for server-to-server Google Sheets access. |
| **`token.json`** | Cached OAuth 2.0 token generated after user grants consent via the browser OAuth flow. |

### Module Files (in `modules/`)

| Module | Description |
|--------|-------------|
| **`fetch_cfb.py`** | **Game data fetcher.** Calls CFBD `/games` endpoint, normalizes fields (home/away teams, scores, venue info), parses dates, saves raw JSON for debugging, infers week anchor dates from `/calendar`. Returns a DataFrame with one row per team per game (home + away rows). |
| **`fetch_weather.py`** | **Weather data enricher.** Resolves game location via venue ID → venue state/city → team city fallback → hardcoded `CITY_COORDS`. Fetches weather from Open-Meteo (archive for past, forecast for future) and/or WeatherAPI.com. Caches responses in `weather_cache.json`. Returns Condition, Temp (°F), Wind (mph), PrecipProb (%). |
| **`fetch_advanced_stats.py`** | **Advanced metrics fetcher.** Pulls SP+ ratings (`/ratings/sp`), FPI ratings (`/ratings/fpi`), and team-level advanced stats (`/stats/season` — EPA/play, Success Rate, Havoc Rate). Optionally loads local Massey CSV. Merges everything into a single DataFrame keyed by team. |
| **`compute_winprob.py`** | **Win probability calculator.** Loads calibrated logistic regression coefficients from `model_params.json`. Computes `Win_%` using a linear combination of SP_Diff, FPI_Diff, HomeAdv, and optional Massey diffs. Falls back to default coefficients if no calibration file exists. |
| **`compute_spread.py`** | **Spread & Total predictor.** Loads calibrated linear regression coefficients. Predicts point spread (`Spread_Pred`) using SP/FPI/Massey diffs + home field advantage. Predicts over/under total (`Total_Pred`) using a baseline + spread magnitude + weather adjustments (wind, temperature). |
| **`odds_value_analysis.py`** | **Value betting engine.** Fetches live betting odds from CFBD `/lines` endpoint with configurable provider priority (DraftKings, FanDuel, etc.). Merges with model predictions. Computes Expected Value (EV%) for moneyline bets. Flags value bets where EV > 5%, spread value > 3 points, or total value > 3 points. |
| **`model_calibration.py`** | **Backtesting & calibration.** Fetches historical games (2014–present) from CFBD, merges with SP+/FPI ratings, optionally merges Massey ratings. Trains logistic regression (win prob) and linear regression (spread) models. Saves coefficients to `model_params.json`. Supports Massey diffs when `CALIB_USE_MASSEY=1`. |
| **`export_data.py`** | **Export utilities.** Exports DataFrames to Excel (.xlsx) with timezone-safe datetime handling. Supports single-sheet, multi-sheet workbooks, Google Sheets export via Service Account (path, JSON env, or base64). |
| **`massey_builder.py`** | **Massey rating builder.** Constructs composite Massey-style ratings from CFBD SP+ and FPI z-scores. Builds per-season CSV files in `data/massey/`. Provides `enrich_with_massey_diffs()` to attach Massey diffs to prediction DataFrames. Used by `main.py` as an optional enhancement. |
| **`fetch_massey.py`** | **Massey CSV loader.** Loads per-season Massey ratings from CSV files (`data/massey/massey_<season>.csv`). Auto-downloads from URL if `MASSEY_URL_TEMPLATE` is set. Auto-maps column names flexibly. Returns Team, Massey_Total, Massey_Off, Massey_Def. |
| **`cfbd_dual.py`** | **Dual CFBD key rotation client.** Implements a robust HTTP client that rotates between multiple CFBD API keys. Tries each key per attempt, backs off exponentially on 401/429/5xx errors. Used optionally if multiple keys are configured. |
| **`venues.py`** | **Venue coordinate resolver.** Caches venue lat/lon from CFBD `/venues` endpoint into `venues_cache.json`. Handles 401 errors gracefully. Used by `fetch_weather.py` for precise game-location weather. |
| **`utils_massey_join.py`** | **Simple Massey join utility.** Lightweight helper to attach Massey diffs to any DataFrame with Team/Opponent columns. Uses `fetch_massey.py` and `model_calibration._canon_team` for team name canonicalization. |
| **`scheduler.py`** | **Placeholder/Skeleton** — currently empty (1 line). Intended for automated scheduled runs. |

### JSON Files (Raw Data)

| File | Description |
|------|-------------|
| **`games_raw_2023_week1.json`** | Raw CFBD `/games` response dump for debugging (autogenerated). |
| **`games_raw_2023_week2.json`** | Same — week 2. |
| **`games_raw_2023_week3.json`** | Same — week 3. |
| **`games_raw_2024_week1.json`** | Same — week 1 2024. |
| **`games_raw_2024_week2.json`** | Same — week 2 2024. |
| **`games_raw_2024_week3.json`** | Same — week 3 2024. |

---

## How to Deploy on Vercel

> ⚠️ **Note:** Vercel is primarily a frontend/serverless platform. Streamlit apps typically require a long-running Python process, so Vercel is **not the ideal host**. Better options: Streamlit Community Cloud, Railway, Render, or a VPS. However, there is a workaround using Vercel's Serverless Functions.

### Option 1: Streamlit Community Cloud (Recommended — Free)

1. Push the project to a **public GitHub repo**
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Click **"New app"** → Select your repo, branch, and `main.py`
4. Set secrets in the dashboard:
   - `CFBD_API_KEY` = `your_key`
   - `WEATHERAPI_KEY` = `your_key` (optional)
5. Deploy in under a minute

### Option 2: Deploy on Vercel (via Serverless Wrapper)

If you specifically need Vercel, here's how:

1. Install the Vercel CLI:
   ```bash
   npm i -g vercel
   ```

2. Create **`api/index.py`** (a Vercel Serverless Function wrapper):
   ```python
   from main import app
   # or use Streamlit's async serverless adapter
   ```

3. Create **`vercel.json`**:
   ```json
   {
     "builds": [
       {
         "src": "api/index.py",
         "use": "@vercel/python",
         "config": { "maxLambdaSize": "50mb" }
       }
     ],
     "routes": [{ "src": "/(.*)", "dest": "api/index.py" }]
   }
   ```

4. Create **`requirements_vercel.txt`** (lightweight — remove heavy GUI packages):
   ```
   streamlit==1.37.0
   pandas==2.2.2
   numpy==1.26.4
   scikit-learn==1.5.0
   requests==2.31.0
   plotly==5.24.0
   python-dotenv==1.0.1
   openpyxl==3.1.5
   ```

5. Set environment variables in Vercel Dashboard:
   - `CFBD_API_KEY`
   - Other keys as needed

6. Deploy:
   ```bash
   vercel --prod
   ```

### Option 3: Railway / Render (Better for Streamlit)

```bash
# Railway (railway.app)
railway login
railway init
railway up

# Render (render.com)
# Connect GitHub repo → Select "Web Service" → 
# Build Command: pip install -r requirements.txt
# Start Command: streamlit run main.py --server.port $PORT
```

---

## Further Work

### Short-term Improvements

- [ ] **Scheduler Module**: Implement `scheduler.py` for automated weekly runs (cron-based prediction generation)
- [ ] **Database Backend**: Replace JSON caches (weather, venues) with SQLite/PostgreSQL for persistence and querying
- [ ] **Multi-user Auth**: Add user accounts with session-based configuration storage
- [ ] **Team Logos**: Fetch and display team logos from CFBD `/teams` endpoint
- [ ] **Historical Predictions**: Store prediction history and show performance tracking (accuracy over time)
- [ ] **ML Model Zoo**: Compare logistic regression vs Random Forest vs XGBoost for win probability
- [ ] **Live Scores**: Add in-game live win probability tracking using CFBD's real-time endpoints
- [ ] **Betting Tracker**: Track placed bets, P&L, ROI with a built-in ledger

### Medium-term Features

- [ ] **Web Scraper**: Add alternative odds sources (web scrape for comparison)
- [ ] **Player-Level Stats**: Incorporate player impact metrics (injuries, transfers) via `cfbd` Python package
- [ ] **Recruiting Impact**: Merge recruiting class rankings (247Sports / Rivals) into prediction features
- [ ] **Dashboard Redesign**: Switch to a multi-page Streamlit layout with dedicated tabs (Predictions, Calibration, History, About)
- [ ] **Push Notifications**: Email/SMS alerts when a high-confidence value bet is detected
- [ ] **API Endpoints**: Expose prediction API via FastAPI alongside the Streamlit frontend
- [ ] **Mobile App**: Wrap predictions API in a React Native or Flutter mobile app

### Long-term Vision

- [ ] **Real-time Data Pipeline**: Use Kafka/Redis for streaming live game data
- [ ] **FBS/FCS + NFL Expansion**: Extend to all NCAA divisions and the NFL
- [ ] **Generative AI Insights**: Weekly matchup summaries generated by LLM integration
- [ ] **Public Shareable Reports**: Generate shareable snapshot URLs with key predictions
- [ ] **Social Features**: Community pick'em pools, leaderboards, confidence-based picking
- [ ] **Monetization**: Premium tier with exclusive features (advanced models, deeper history, API access)

---

## Model Calibration

The calibration module (`modules/model_calibration.py`) trains models on historical data:

- **Logistic Regression** (Win Probability): Features = SP_Diff, FPI_Diff, HomeAdv (+ optional Massey diffs)
- **Linear Regression** (Spread Prediction): Features = SP_Diff, FPI_Diff (+ optional Massey diffs)
- **Metrics Tracked**: Accuracy, Log Loss, RMSE, R²

Run calibration from the app sidebar → **🔧 Model Calibration** section → Click **🧮 Recalibrate Models**.

---

## License

This project is for **educational and personal use only**. Gambling involves financial risk. Always bet responsibly.

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Frontend | Streamlit 1.37 |
| Backend | Python 3.10+ |
| ML | scikit-learn (LogisticRegression, LinearRegression) |
| Data | pandas, numpy |
| Visualization | Plotly Express |
| Data Source | [CollegeFootballData.com](https://collegefootballdata.com) API |
| Weather | Open-Meteo API, WeatherAPI.com |
| Spreadsheets | Google Sheets API (gspread + google-auth) |
| Export | openpyxl (Excel), gspread (Google Sheets) |
| Caching | Streamlit's `@st.cache_data`, local JSON caches |
| Scheduling | `schedule` library (skeleton) |