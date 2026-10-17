# Deploying the Telugu Digital Library Hub

This app is a **FastAPI + Postgres web service** that serves its own frontend. It
runs as a single Python web service (a `render.yaml` blueprint and a `Dockerfile`
are included). It does **not** run on GitHub Pages, which only serves static files.

Target setup: **Render** for the app, **Supabase** for the database, custom domain
**library.auchithyam.com**.

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
  `https://library.auchithyam.com/auth/callback`
  (and, while testing, your `https://<app>.onrender.com/auth/callback`).
- **Vision (OCR):** see 2b below.
- **Gemini:** an API key from https://aistudio.google.com/apikey.

### 2b. Setting GOOGLE_CREDENTIALS_JSON (Vision / OCR)

1. In Google Cloud Console, pick the project and enable the API:
   **APIs & Services -> Library -> "Cloud Vision API" -> Enable** (billing must be on).
2. **IAM & Admin -> Service Accounts** -> create one (or reuse an existing one) ->
   **Keys -> Add key -> Create new key -> JSON**. A `.json` file downloads.
3. Turn that file into a single line so it can live in one environment variable:

   ```bash
   python -c "import json; print(json.dumps(json.load(open('credentials.json'))))"
   ```

   `json.dumps` keeps the private key's newlines as `\n` escapes, which is exactly
   what the app expects. Copy the entire output.
4. In Render -> your service -> **Environment -> Add Environment Variable**:
   - Key: `GOOGLE_CREDENTIALS_JSON`
   - Value: paste the whole single-line output from step 3.
5. **Delete `GOOGLE_APPLICATION_CREDENTIALS`** if it is set. It points at a local
   file that is never deployed, and it is what caused the original
   "credentials.json was not found" error.
6. Save (this redeploys). On boot the logs print
   `Startup config check -> ... GOOGLE_CREDENTIALS_JSON: True`.

Alternative: Render's **Secret Files** feature can mount the JSON at a path, in
which case set `GOOGLE_APPLICATION_CREDENTIALS` to that path instead. The env-var
route above is simpler and is what the app prefers.

## 3. Render wiring (step by step)

### 3a. Connect the repository

1. Render dashboard -> **New -> Blueprint** (to use `render.yaml`) or
   **New -> Web Service** (to configure by hand).
2. Connect GitHub and grant the Render GitHub App access to
   **`drrp/PDF_Fetch_Downloader_v2`** (it is a private repo, so it will not appear
   in the list until you grant access in the app's repository settings).
3. Pick the repo, branch **`main`**.

### 3b. Service settings

| Setting | Value |
| --- | --- |
| Runtime | Python 3 |
| Build command | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start command | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"` |
| Health check path | `/` |

The `--proxy-headers --forwarded-allow-ips="*"` part is required: it makes uvicorn
trust Render's HTTPS headers, so the app builds an `https://` Google OAuth redirect
URL. Without it, login fails on the custom domain.

### 3c. Environment variables

Add these in **Settings -> Environment**:

| Key | Value |
| --- | --- |
| `DATABASE_URL` | Supabase connection string (step 1) |
| `SESSION_SECRET_KEY` | a long random string (Render can generate it) |
| `GOOGLE_CLIENT_ID` | from step 2 |
| `GOOGLE_CLIENT_SECRET` | from step 2 |
| `GOOGLE_CREDENTIALS_JSON` | full Vision service-account JSON, one line |
| `GEMINI_API_KEY` | from step 2 |
| `GEMINI_MODEL` | `gemini-3.8-flash` |
| `ADMIN_EMAIL` | the email that gets the admin dashboard |
| `COOKIE_HTTPS_ONLY` | `true` |
| `PYTHON_VERSION` | `3.12.0` |

### 3d. Free tier vs paid (and the card prompt)

The blueprint ships on the **free** plan, which needs no payment method. Free
services sleep after ~15 minutes idle (the next visit cold-starts in ~30-60s) and
have no persistent disk, so downloaded PDFs are lost on restart.

To keep downloads across restarts, move to a paid instance and add a disk: set
`plan: starter` in `render.yaml` and uncomment the `disk:` block, or use
**Settings -> Disks -> Add** with mount path `/opt/render/project/src/downloads`.
Render asks for a card for any paid instance or disk.

## 4. Custom domain: library.auchithyam.com

1. Render service -> **Settings -> Custom Domains -> Add Custom Domain** and enter
   `library.auchithyam.com`.
2. Render shows the DNS records to create at your registrar. `library` is a
   subdomain, so it is normally a single **CNAME** to your service host
   (`<service>.onrender.com`). Add it at your DNS provider (Cloudflare, GoDaddy,
   your host's panel, etc.).
   - If you use Cloudflare, set the record to **DNS only** (grey cloud) until the
     Render certificate issues, then you may proxy it.
3. Wait for Render to show **Certificate issued** for the domain.
4. In Google Cloud Console, confirm the OAuth redirect URI is exactly
   `https://library.auchithyam.com/auth/callback` (same scheme, host, path).

## 5. Verify

- [ ] `https://library.auchithyam.com/` loads the login page.
- [ ] "Google తో లాగిన్ అవ్వండి" completes and returns you logged in (no
      `redirect_uri_mismatch`).
- [ ] A search returns results; a download saves a PDF.
- [ ] OCR on a book writes rows to `pdf_text_index` in Supabase.
- [ ] The AI summary appears (confirms `GEMINI_MODEL` and `GEMINI_API_KEY`).
- [ ] Logging in with `ADMIN_EMAIL` shows the admin panel.

## 6. Notes and gotchas

- **Downloads live on disk** (`downloads/`). The free plan wipes that disk on
  restart, so keep the persistent disk (paid) or move PDFs to Supabase Storage.
- **OCR and AI calls are heavy.** A free instance can time out on large books.
- **Model name:** the AI summary reads the model from `GEMINI_MODEL`
  (default `gemini-3.8-flash`). Change the env var if Google retires it.
- **Secrets stay in the host's env vars.** `.env` is git-ignored.
