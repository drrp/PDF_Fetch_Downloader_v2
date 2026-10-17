# PDF Fetch Downloader — Telugu Digital Library Hub

A FastAPI + Supabase web app that searches four Telugu/Indian digital libraries at
once, downloads the books you pick, OCRs them with Google Cloud Vision, and answers
questions over the OCR text using Google Gemini.

> This is the cleaned-up **deployment** version of the project, prepared as a
> separate repository so the original stays untouched.

## What it does

- **Unified search** across Internet Archive, SVK Library (Telugu Academy),
  Manasu Foundation, and TTD Ebooks.
- **One-click download** of a found book as a PDF.
- **OCR pipeline** (Telugu + English) that indexes every page into Postgres for
  full-text search, with word-level bounding boxes.
- **AI answers** grounded strictly in the OCR'd text, returned in a fixed,
  coloured Telugu template.
- **Google login** with a user profile page and an admin dashboard.

## Stack

- **Backend:** FastAPI, served by Uvicorn (`backend/main.py`).
- **Database:** Supabase (PostgreSQL) via `psycopg2`, using `DATABASE_URL`.
- **Frontend:** static HTML/CSS/JS in `frontend/`, served by the same FastAPI app.
- **Services:** Google OAuth (authlib), Google Cloud Vision (OCR), Google Gemini (AI).

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the values
uvicorn backend.main:app --reload
```

Open http://127.0.0.1:8000 (set `COOKIE_HTTPS_ONLY=false` in `.env` for local HTTP).

## Configuration

All settings come from environment variables — see `.env.example`. The important
ones are `DATABASE_URL`, `SESSION_SECRET_KEY`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_CREDENTIALS_JSON`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
`ADMIN_EMAIL`, and `COOKIE_HTTPS_ONLY`.

## Deploying

See **DEPLOY.md**. The app runs as a single Python web service (a `render.yaml`
blueprint and a `Dockerfile` are included). It does **not** run on GitHub Pages.

## Project layout

```
backend/        FastAPI app + one scraper per library
frontend/       HTML/CSS/JS served by the backend
legacy/         Standalone desktop/CLI tools, not part of the web service
render.yaml     Render blueprint
Dockerfile      Portable container build
requirements.txt
```
