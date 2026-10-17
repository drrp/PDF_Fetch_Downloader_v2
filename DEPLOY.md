# Deploying the Telugu Digital Library Hub

This app is a **FastAPI + Postgres web service** that serves its own frontend. It
runs as a single Python web service (a `render.yaml` blueprint and a `Dockerfile`
are included). It does **not** run on GitHub Pages, which only serves static files.

Target setup: **Render** for the app, **Supabase** for the database, custom domain
**library.auchithysm.com**.

> Spelling check: your logo assets point at `auchithyam.com` while the live host is
> `library.auchithysm.com`. Make sure the domain you register in Render and in
> Google matches the one you actually own.

---

## 1. Supabase connection string

1. Supabase dashboard -> **Project Settings -> Database**.
2. Copy the **Connection string -> URI** (use the *Session pooler*).
   It looks like `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
3. The app creates its tables (`users`, `activities`, `pdf_text_index`) on first
   start, so no manual SQL is needed.

## 2. Google credentials

- **OAuth (login):** Google Cloud Console -> APIs & Services -> Credentials ->
  OAuth 2.0 Client ID. Under **Authorized redirect URIs** add
  `https://library.auchithysm.com/auth/callback`
  (and, while testing, your `https://<app>.onrender.com/auth/callback`).
- **Vision (OCR):** a service account with the Cloud Vision API enabled; download
  its JSON key.
- **Gemini:** an API key from https://aistudio.google.com/apikey.

## 3. Deploy on Render

**Blueprint (recommended):** push `render.yaml`, then Render -> **New -> Blueprint**
-> pick this repo. Fill in the secret env vars when prompted.

**Manual web service:** Render -> **New -> Web Service**; runtime **Python 3**;
build `pip install -r requirements.txt`; start
`uvicorn backend.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`.

Environment variables:

| Key | Value |
| --- | --- |
| `DATABASE_URL` | Supabase connection string (step 1) |
| `SESSION_SECRET_KEY` | a long random string (Render can generate it) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from step 2 |
| `GOOGLE_CREDENTIALS_JSON` | full Vision service-account JSON |
| `GEMINI_API_KEY` | from step 2 |
| `GEMINI_MODEL` | `gemini-2.0-flash` |
| `ADMIN_EMAIL` | the email that gets the admin dashboard |
| `COOKIE_HTTPS_ONLY` | `true` |
| `PYTHON_VERSION` | `3.12.0` |

## 4. Custom domain (library.auchithysm.com)

1. Render service -> **Settings -> Custom Domains -> Add** `library.auchithysm.com`.
2. At your DNS provider add the CNAME/A records Render shows for that domain.
3. Wait for the certificate to issue, then confirm the Google OAuth redirect URI
   uses the same `https://library.auchithysm.com/auth/callback`.

The `--proxy-headers` flag in the start command is what makes the app generate
`https://` URLs behind Render's proxy — without it, OAuth login can fail on a
custom domain.

## 5. Notes and gotchas

- **Downloads live on disk** (`downloads/`). The free plan wipes that disk on
  restart, so keep the persistent disk in `render.yaml` (paid) or move PDFs to
  Supabase Storage later.
- **OCR and AI calls are heavy.** A free instance can time out on large books.
- **Model name:** the AI summary now reads the model from `GEMINI_MODEL`
  (default `gemini-2.0-flash`). Change the env var if Google retires it.
- **Secrets stay in the host's env vars.** `.env` is git-ignored.
