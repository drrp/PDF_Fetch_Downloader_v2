import warnings
warnings.filterwarnings("ignore", message=".*httpx module is deprecated.*")
warnings.filterwarnings("ignore", module="authlib")

import os
import requests
import re
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import base64
import fitz  # PyMuPDF
import time
import pytz
from google.oauth2 import service_account
from google import genai
from google.genai import types  # 🌟 AI కాన్ఫిగరేషన్ (Temperature) కోసం ఇది అవసరం
from authlib.integrations.starlette_client import OAuth
from google.cloud import vision
from dotenv import load_dotenv
from fastapi import Form
from fastapi import FastAPI, Query, Request, Depends
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from urllib.parse import quote
from backend.svk import SVKScraper
from backend.archive import InternetArchiveScraper
from backend.manasu import ManasuFoundationScraper
from backend.ttd import TTDScraper
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(override=True)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
FRONTEND_INDEX = os.path.join(FRONTEND_DIR, "index.html")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

# Render Environment Variable నుండి Supabase కనెక్షన్ లింక్ తీసుకుంటుంది
DB_URL = os.getenv("DATABASE_URL")

# --- Config (override any of these via environment variables) ---
# Email that gets the admin dashboard.
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "rpsarma9247@gmail.com")
# Gemini model used for the AI summaries. Override if this name is retired.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Send the login cookie only over HTTPS. Set COOKIE_HTTPS_ONLY=false for plain-HTTP local dev.
COOKIE_HTTPS_ONLY = os.getenv("COOKIE_HTTPS_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")
# Base URL of the Chandam (ఛందస్సు) API. Accepts a full URL or Render's internal
# "host:port". Leave empty to disable chandassu analysis.
CHANDAM_API_URL = os.getenv("CHANDAM_API_URL", "").strip()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- Startup diagnostics: prints only whether each variable is present, never its value ---
print(
    "Startup config check ->",
    "DATABASE_URL:", bool(DB_URL),
    "| SESSION_SECRET_KEY:", bool(os.getenv("SESSION_SECRET_KEY")),
    "| GOOGLE_CLIENT_ID:", bool(os.getenv("GOOGLE_CLIENT_ID")),
    "| GOOGLE_CLIENT_SECRET:", bool(os.getenv("GOOGLE_CLIENT_SECRET")),
    "| GOOGLE_CREDENTIALS_JSON:", bool(os.getenv("GOOGLE_CREDENTIALS_JSON")),
    "| GEMINI_API_KEY:", bool(os.getenv("GEMINI_API_KEY")),
)

def build_vision_client():
    """Create a Google Cloud Vision client from GOOGLE_CREDENTIALS_JSON.

    Raises RuntimeError with a clear, actionable message when the credentials are
    missing or malformed, so the OCR page shows something useful instead of a raw
    library error about a credentials file.
    """
    creds_json = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
    if creds_json:
        try:
            creds_dict = json.loads(creds_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON is not valid JSON. Paste the full "
                "service-account JSON (from '{' to '}') as a single value. "
                f"Parser said: {e}"
            )
        try:
            credentials = service_account.Credentials.from_service_account_info(creds_dict)
        except Exception as e:
            raise RuntimeError(f"GOOGLE_CREDENTIALS_JSON is missing required fields: {e}")
        try:
            return vision.ImageAnnotatorClient(credentials=credentials)
        except Exception as e:
            raise RuntimeError(f"Google Vision rejected the service account: {e}")

    # No JSON provided: fall back to application-default credentials, but explain
    # clearly if that fails (it will on hosts like Render, where no file exists).
    try:
        return vision.ImageAnnotatorClient()
    except Exception as e:
        hint = ""
        gac = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
        if gac.startswith("{"):
            hint = (
                " It looks like GOOGLE_APPLICATION_CREDENTIALS contains the JSON "
                "itself - that variable must be a file PATH. Move the JSON into "
                "GOOGLE_CREDENTIALS_JSON and delete GOOGLE_APPLICATION_CREDENTIALS."
            )
        raise RuntimeError(
            "Google Vision credentials are not configured. Set the "
            "GOOGLE_CREDENTIALS_JSON environment variable to the full "
            "service-account JSON, and remove any GOOGLE_APPLICATION_CREDENTIALS "
            f"that points to a local file.{hint} Underlying error: {e}"
        )

def get_db_connection():
    if not DB_URL:
        raise Exception("DATABASE_URL environment variable is not set!")
    return psycopg2.connect(DB_URL)

def init_db():
    if not DB_URL:
        print("Warning: DATABASE_URL is not set.")
        return
        
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Users Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT,
                picture TEXT,
                last_login TEXT,
                is_active BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Activities Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activities (
                id SERIAL PRIMARY KEY,
                email TEXT,
                name TEXT,
                picture TEXT,
                action TEXT,
                details TEXT,
                time TEXT,
                status TEXT DEFAULT 'Success'
            )
        ''')
        
        # PDF Full-Text Search Index Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pdf_text_index (
                id SERIAL PRIMARY KEY,
                book_title TEXT,
                filename TEXT,
                page_number INTEGER,
                extracted_text TEXT,
                word_boxes TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error initializing DB: {e}")

init_db()

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "your-secret-key"),
    same_site="lax",
    https_only=COOKIE_HTTPS_ONLY
)

oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

def log_user_activity(request: Request, action_type: str, details: str, status: str = "Success"):
    user = request.session.get('user')
    if user:
        try:
            IST = pytz.timezone('Asia/Kolkata')
            current_time = datetime.now(IST).strftime("%d-%m-%Y %I:%M %p")
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO activities (email, name, picture, action, details, time, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (user.get("email"), user.get("name"), user.get("picture"), action_type, details, current_time, status))
            cursor.execute('UPDATE users SET is_active = TRUE WHERE email = %s', (user.get("email"),))
            conn.commit()
            conn.close()
        except Exception as e:
            print("DB Error logging activity:", e)

@app.get('/profile')
def user_profile_page(request: Request):
    user = request.session.get('user')
    if not user:
        return RedirectResponse(url='/login/google', status_code=303)
    
    profile_page = os.path.join(FRONTEND_DIR, "profile.html")
    return FileResponse(profile_page) if os.path.exists(profile_page) else {"error": "profile.html not found"}

@app.get('/user/activity-json')
def get_user_activity_json(request: Request):
    user = request.session.get('user')
    if not user:
        return {"error": "Unauthorized"}
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # యూజర్ యొక్క కార్యకలాపాలు (Activities) తెప్పించడం
        cursor.execute("SELECT action, details, time, status FROM activities WHERE email = %s ORDER BY id DESC", (user.get('email'),))
        activities = [dict(row) for row in cursor.fetchall()]
        
        # గణాంకాలు (Stats) లెక్కించడం
        searches_count = sum(1 for a in activities if a['action'] == 'Search')
        downloads_count = sum(1 for a in activities if a['action'] == 'Download' and a['status'] == 'Success')
        
        conn.close()
        
        return {
            "user": user,
            "stats": {
                "searches": searches_count,
                "downloads": downloads_count
            },
            "activities": activities
        }
    except Exception as e:
        return {"error": str(e)}

@app.get('/login/google')
async def login(request: Request):
    redirect_uri = request.url_for('auth')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get('/auth/callback')
async def auth(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo') or token.get('user_info')
        if not user_info:
            resp = await oauth.google.get('https://www.googleapis.com/oauth2/v3/userinfo', token=token)
            user_info = resp.json()
        
        if user_info and user_info.get('email'):
            user_dict = {
                "name": user_info.get("name"),
                "email": user_info.get("email"),
                "picture": user_info.get("picture")
            }
            request.session['user'] = user_dict
            
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            IST = pytz.timezone('Asia/Kolkata')
            login_time = datetime.now(IST).strftime("%d-%m-%Y %I:%M %p")
            
            cursor.execute('SELECT email FROM users WHERE email = %s', (user_dict.get('email'),))
            existing = cursor.fetchone()
            
            if existing:
                cursor.execute('''
                    UPDATE users SET name = %s, picture = %s, last_login = %s WHERE email = %s
                ''', (user_dict.get('name'), user_dict.get('picture'), login_time, user_dict.get('email')))
            else:
                cursor.execute('''
                    INSERT INTO users (email, name, picture, last_login, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                ''', (user_dict.get('email'), user_dict.get('name'), user_dict.get('picture'), login_time))
            
            conn.commit()
            conn.close()
                
            if user_dict.get('email') == ADMIN_EMAIL:
                return RedirectResponse(url='/admin', status_code=303)
                
    except Exception as e:
        print(f"OAuth Error: {e}")
        
    return RedirectResponse(url='/', status_code=303)

@app.get('/admin')
def admin_dashboard(request: Request):
    user = request.session.get('user')
    if not user or user.get('email') != ADMIN_EMAIL:
        return RedirectResponse(url='/')
    admin_page = os.path.join(FRONTEND_DIR, "admin.html")
    return FileResponse(admin_page) if os.path.exists(admin_page) else {"error": "admin.html not found"}

@app.get('/admin/users-json')
def get_admin_users_json(request: Request):
    user = request.session.get('user')
    if not user or user.get('email') != ADMIN_EMAIL:
        return {"error": "Unauthorized Access"}
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM users")
    users = [dict(row) for row in cursor.fetchall()]
    cursor.execute("SELECT * FROM activities ORDER BY id DESC")
    activities = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return {
        "total_users": len(users), 
        "users": users,
        "activities": activities,
        "stats": {
            "searches": sum(1 for a in activities if a['action'] == 'Search'),
            "downloads": sum(1 for a in activities if a['action'] == 'Download'),
            "total_actions": len(activities)
        }
    }

@app.get('/ocr-hub')
def ocr_hub_page(request: Request):
    user = request.session.get('user')
    if not user:
        return RedirectResponse(url='/login/google', status_code=303)
    ocr_page = os.path.join(FRONTEND_DIR, "ocr.html")
    return FileResponse(ocr_page) if os.path.exists(ocr_page) else {"error": "ocr.html not found"}

@app.get('/api/current-user')
def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return {"logged_in": False}
    return {
        "logged_in": True, "name": user.get("name"), "email": user.get("email"),
        "picture": user.get("picture"), "is_admin": (user.get("email") == ADMIN_EMAIL)
    }

@app.get('/logout')
def logout(request: Request):
    user = request.session.get('user')
    if user:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET is_active = FALSE WHERE email = %s', (user.get('email'),))
            conn.commit()
            conn.close()
        except: pass
    request.session.clear()
    return RedirectResponse(url='/')

@app.post("/reset-app")
def reset_app(request: Request):
    # The dashboard's Reset button reloads the page client-side; this endpoint
    # only acknowledges the request so the frontend fetch() does not error.
    return {"status": "ok"}

if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/downloads", StaticFiles(directory=DOWNLOAD_DIR), name="downloads")

@app.get("/")
def read_root(request: Request):
    user = request.session.get('user')
    if not user:
        login_page = os.path.join(FRONTEND_DIR, "login.html")
        return FileResponse(login_page) if os.path.exists(login_page) else RedirectResponse(url='/login/google')
    if os.path.exists(FRONTEND_INDEX):
        return FileResponse(FRONTEND_INDEX)
    return {"error": "index.html not found"}

svk_scraper = SVKScraper()
ia_scraper = InternetArchiveScraper()
manasu_scraper = ManasuFoundationScraper()
ttd_scraper = TTDScraper()

@app.get("/search")
def search(
    request: Request, query: str = Query(...),
    use_svk: bool = Query(True), use_ia: bool = Query(True),
    use_manasu: bool = Query(True), use_ttd: bool = Query(True),
    page: int = Query(1), limit: int = Query(10)
):
    log_user_activity(request, "Search", f"వెతికిన పదం: '{query}'")
    results = []
    if use_svk: results.extend(svk_scraper.search(query))
    if use_ia: results.extend(ia_scraper.search(query))
    if use_manasu: results.extend(manasu_scraper.search(query))
    if use_ttd: results.extend(ttd_scraper.search(query))
        
    total_items = len(results)
    start_idx = (page - 1) * limit
    paginated_results = results[start_idx:start_idx + limit]
    
    return {
        "results": paginated_results, "total": total_items,
        "page": page, "total_pages": (total_items + limit - 1) // limit if total_items > 0 else 1
    }

@app.get("/download-book")
def download_book(request: Request, book_page_url: str = Query(...), title: str = Query(...), source: str = Query("SVK")):
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()[:40] or "book"
    filename = f"{safe_title}_{uuid.uuid4().hex[:6]}.pdf"
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    
    try:
        if source == "Internet Archive": 
            ia_scraper.download(book_page_url, title, filepath)
        elif source == "Manasu Foundation": 
            manasu_scraper.consult_download(book_page_url, title, filepath) if hasattr(manasu_scraper, 'consult_download') else manasu_scraper.download(book_page_url, title, filepath)
        elif source == "TTD Ebooks": 
            ttd_scraper.download(book_page_url, title, filepath)
        else: 
            svk_scraper.download(book_page_url, title, filepath)
            
        log_user_activity(request, "Download", f"డౌన్‌లోడ్: '{title}' ({source})", "Success")
        return {
            "status": "success",
            "message": "పుస్తకం విజయవంతంగా డౌన్‌లోడ్ చేయబడింది!",
            "file_url": f"/downloads/{filename}",
            "filename": filename,
            "download_url": f"/download-file?filename={quote(filename)}",
        }
    except Exception as e:
        log_user_activity(request, "Download", f"విఫలం: '{title}' | ఎర్రర్: {str(e)}", "Failed")
        return {"status": "error", "message": str(e)}

@app.get("/download-file")
def download_file(request: Request, filename: str = Query(...)):
    """Serve a saved PDF as a file download (attachment) rather than inline.

    Browsers render large scanned PDFs very slowly in a tab; sending
    Content-Disposition: attachment saves the file straight to the user's disk.
    """
    user = request.session.get('user')
    if not user:
        return RedirectResponse(url='/login/google', status_code=303)
    # os.path.basename prevents path traversal (e.g. '../../etc/passwd')
    safe_name = os.path.basename(filename)
    filepath = os.path.join(DOWNLOAD_DIR, safe_name)
    if not os.path.exists(filepath):
        return {"error": "File not found"}
    return FileResponse(filepath, media_type='application/pdf', filename=safe_name)

@app.get("/list-downloaded-books")
def list_downloaded_books(request: Request):
    user = request.session.get('user')
    if not user: return {"error": "Unauthorized"}
    
    files = []
    if os.path.exists(DOWNLOAD_DIR):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            for f in os.listdir(DOWNLOAD_DIR):
                if f.lower().endswith('.pdf'):
                    cursor.execute("SELECT 1 FROM pdf_text_index WHERE filename = %s LIMIT 1", (f,))
                    is_done = cursor.fetchone() is not None
                    files.append({"filename": f, "ocr_done": is_done})
            conn.close()
        except Exception as e:
            print("Error listing files with DB:", e)
            for f in os.listdir(DOWNLOAD_DIR):
                if f.lower().endswith('.pdf'):
                    files.append({"filename": f, "ocr_done": False})
                    
    return {"files": files}

@app.post("/run-special-ocr")
def run_special_ocr(request: Request, filename: str = Query(...), start_page: int = Query(None), end_page: int = Query(None)):
    user = request.session.get('user')
    if not user: return {"error": "Unauthorized"}
    
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return {"status": "error", "message": "ఫైల్ కనుగొనబడలేదు."}
        
    doc = None
    try:
        # 🌟 Google Cloud Vision క్లయింట్ — క్రెడెన్షియల్స్ లేకపోతే స్పష్టమైన ఎర్రర్ ఇస్తుంది
        client = build_vision_client()

        doc = fitz.open(filepath)
        total_pages = len(doc)
        
        start_idx = 0
        end_idx = total_pages - 1
        
        if start_page is not None and start_page >= 1:
            start_idx = start_page - 1
        if end_page is not None and end_page <= total_pages:
            end_idx = end_page - 1
            
        if start_idx > end_idx or start_idx >= total_pages:
            return {"status": "error", "message": "దయచేసి సరైన పేజీ నంబర్లను ఎంచుకోండి."}
            
        conn = get_db_connection()
        cursor = conn.cursor()
        book_title = filename.rsplit('_', 1)[0]
        image_context = vision.ImageContext(language_hints=["te", "en"])

        for p_num in range(start_idx + 1, end_idx + 2):
            cursor.execute("DELETE FROM pdf_text_index WHERE filename = %s AND page_number = %s", (filename, p_num))

        for page_num in range(start_idx, end_idx + 1):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=200)
            image_bytes = pix.tobytes("png")            
            image = vision.Image(content=image_bytes)
            
            response = client.document_text_detection(image=image, image_context=image_context)
            if response.error.message: continue

            page_text = response.full_text_annotation.text if response.full_text_annotation else ""
            word_boxes = []
            
            if response.full_text_annotation:
                for page_obj in response.full_text_annotation.pages:
                    for block in page_obj.blocks:
                        for paragraph in block.paragraphs:
                            for word in paragraph.words:
                                word_str = "".join([symbol.text for symbol in word.symbols])
                                norm_vertices = word.bounding_box.normalized_vertices
                                if len(norm_vertices) >= 4:
                                    word_boxes.append({
                                        "word": word_str, "x0": norm_vertices[0].x, "y0": norm_vertices[0].y,
                                        "x2": norm_vertices[2].x, "y2": norm_vertices[2].y, "confidence": round(word.confidence, 2)
                                    })
            
            if page_text:
                cursor.execute('''
                    INSERT INTO pdf_text_index (book_title, filename, page_number, extracted_text, word_boxes)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (book_title, filename, page_num + 1, page_text, json.dumps(word_boxes, ensure_ascii=False)))
                
        conn.commit()
        conn.close()
        
        range_msg = f" (పేజీలు {start_idx + 1} నుండి {end_idx + 1} వరకు)" if (start_page or end_page) else " (పూర్తి పుస్తకం)"
        return {"status": "success", "message": f"'{book_title}' కు OCR పూర్తయింది! {range_msg}"}

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if doc is not None:
            try: doc.close()
            except: pass

@app.get("/get-ocr-text")
def get_ocr_text(filename: str, page_number: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT extracted_text FROM pdf_text_index WHERE filename = %s AND page_number = %s", (filename, page_number))
        result = cursor.fetchone()
        conn.close()
        if result: return {"status": "success", "text": result["extracted_text"]}
        return {"status": "error", "message": "డేటా కనుగొనబడలేదు."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
@app.get("/get-indexed-books")
def get_indexed_books(request: Request):
    user = request.session.get('user')
    if not user: return {"error": "Unauthorized"}
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT DISTINCT book_title FROM pdf_text_index")
        books = [row['book_title'] for row in cursor.fetchall()]
        conn.close()
        return {"status": "success", "books": books}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/update-ocr-text")
def update_ocr_text(filename: str = Form(...), page_number: int = Form(...), new_text: str = Form(...)):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE pdf_text_index SET extracted_text = %s WHERE filename = %s AND page_number = %s', (new_text, filename, page_number))
        conn.commit()
        conn.close()
        return {"status": "success", "message": "మార్పులు సేవ్ చేయబడ్డాయి!"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Books that use the strict సూత్రము / వృత్తి / ఉదాహరణలు grammar template.
# Every other book gets the general, template-free answer below.
GRAMMAR_BOOKS = ("బాలవ్యాకరణము", "ప్రౌఢవ్యాకరణము")

def _has_grammar_source(snippets_list):
    for s in snippets_list:
        title = s.get("book_title") or ""
        if any(g in title for g in GRAMMAR_BOOKS):
            return True
    return False


def _chandam_base_url():
    url = CHANDAM_API_URL
    if url and not url.startswith("http"):
        url = "http://" + url  # Render's internal host:port has no scheme
    return url.rstrip("/")


def _extract_verse(summary_html):
    """Pull the verse text out of the <div ... chandam="1"> block in the summary."""
    if not summary_html:
        return ""
    m = re.search(r'<div[^>]*chandam=["\']1["\'][^>]*>(.*?)</div>', summary_html, re.S | re.I)
    if not m:
        return ""
    inner = re.sub(r'<br\s*/?>', '\n', m.group(1), flags=re.I)
    inner = re.sub(r'<[^>]+>', '', inner)
    return inner.strip()


def analyze_chandassu(poem_text):
    """Ask the Chandam API to identify the metre (ఛందస్సు) of a verse."""
    base = _chandam_base_url()
    if not base or not (poem_text or "").strip():
        return None
    try:
        resp = requests.post(
            f"{base}/api/determine",
            json={
                "poemText": poem_text,
                "language": 0,       # 0 = Telugu
                "matchYati": True,
                "matchPrasa": True,
                "topMatches": 3,
                "renderFormat": 1,   # 1 = Html
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print("Chandam API HTTP", resp.status_code, (resp.text or "")[:200])
            return None
        matches = (resp.json() or {}).get("matches") or []
        if not matches:
            return None
        best = matches[0]
        rule = best.get("rule") or {}
        return {
            "name": rule.get("name") or rule.get("chandamName") or rule.get("identifier") or "",
            "identifier": rule.get("identifier") or "",
            "percentage": best.get("matchPercentage"),
            "is_matched": best.get("isMatched"),
            "beautified": best.get("beautified") or "",
            "html": best.get("html") or "",
            "errors": best.get("errors") or [],
        }
    except Exception as e:
        print("Chandam API error:", e)
        return None


GENERAL_PROMPT = """You are an expert assistant on Telugu literature and classical texts.
Answer the user's question: ('{query}')

Answer in Telugu, grounded ONLY in the OCR text given below. Never invent a verse,
meaning, or fact that is not supported by that text.

⚠️ STRICT RULES FOR OCR REPRODUCTION:
1. Do NOT autocorrect the OCR text. Reproduce characters and words exactly as given,
   even if they look like typos or have missing letters.
2. Do NOT add characters that are not in the text.
3. Never use LaTeX or math notation (no $\\text{{...}}$). Write only plain Telugu.

⚠️ OUTPUT FORMAT - simple and clean, with NO fixed template and NO coloured boxes:
- Begin with a short Telugu heading naming the text and passage.
- If the passage is a poem / verse (పద్యం), reproduce it EXACTLY as in the OCR text, inside
  this exact markup so the metre (ఛందస్సు) is analysed automatically:
    <div class="padyam" chandam="1">
    first line<br>
    second line<br>
    third line<br>
    fourth line
    </div>
  Put each line of the verse on its own line separated by <br>, with no verse number inside.
- Then give the meaning / explanation in clear Telugu paragraphs (అర్థం / భావం).
- Add a short "విశేషాలు" note only if the text supports it.
- Reproduce the COMPLETE passage available below. Never cut off mid-sentence.
- No preamble such as "Here is the answer". Start directly with the heading.

OCR TEXT:
{combined_text}"""


def generate_ai_summary(query, snippets_list):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: return "⚠️ API కీ లభించలేదు."
    if not snippets_list: return "సమాచారం లభించలేదు."

    try:
        client = genai.Client(api_key=api_key)
        combined_text = "\n".join([f"- గ్రంథం: {s['book_title']} (పేజీ {s['page_number']}): {s['snippet']}" for s in snippets_list])
        
        # 🌟 ఇక్కడ అదనపు Strict Rules ని ప్రాంప్ట్ లోకి చేర్చాము
        grammar_prompt = f"""You are an expert assistant on Telugu traditional
                grammar, grounded strictly in the ('{query}') from the text వ్యాకరణము.
        యూజర్ అడిగిన ('{query}') ఓసీఆర్ చేసిన డేటా ఆధారంగా మాత్రమే సమగ్రమైన సమాధానాన్ని తెలుగులో ఇవ్వండి.
        
        ⚠️ CRITICAL STRICT RULES FOR OCR REPRODUCTION:
        1. OCR డేటాలో అక్షర దోషాలు (typos) ఉన్నా, అక్షరాలు మిస్ అయినా సరే వాటిని ఎట్టి పరిస్థితుల్లోనూ సరిదిద్దవద్దు (DO NOT AUTOCORRECT).
        2. ఉదాహరణకు OCR లో "అ అ" అని ఉంటే, "అ ఆ" అని మీ సొంతంగా మార్చవద్దు. OCR లో ఏది ఉంటే అదే రాయాలి.
        3. మీకు ఇచ్చిన OCR Text లోని అక్షరాలను, పదాలను ఉన్నవి ఉన్నట్లుగా (Exact match / Verbatim) మాత్రమే వాడాలి. మీ సొంతంగా ఒక్క అక్షరాన్ని కూడా చేర్చకూడదు.
        
        ⚠️ అత్యంత ముఖ్యం: జవాబులో ఎట్టి పరిస్థితుల్లోనూ లటెక్ (LaTeX) కోడింగ్ లేదా గణిత చిహ్నాలు ($\text{{...}}$) వాడవద్దు. 
        సంధి రూపాలను లేదా పదాల కూర్పును ఎప్పుడూ సాధారణ తెలుగు అక్షరాలతో మాత్రమే రాయాలి.
        (ఉదాహరణకు: "నిర్జి + ఇంచు = నిర్జించు" అని మాత్రమే రాయాలి).
        ('{query}') అనే ప్రశ్నలో ముఖ్యమైన విషయం ఏ ఏ సూత్రాలు, వృత్తి, ఉదాహరణలు, వ్యాఖ్యాత వివరణల్లో ఉందో గ్రహించి, వాటిని వాడి సమాధానం ఇవ్వాలి.
        Never invent a సూత్రము, rule, or ఉదాహరణ. సూత్ర సంఖ్యను బట్టి పూర్తి సూత్రాన్ని గుర్తించి, ఉన్నది ఉన్నట్టు గానే ఇవ్వాలి.
        పూర్తి సూత్రం - OCR Data లో ఉన్నది ఉన్నట్టుగా ఇవ్వాలి. సామాన్య భాషలోకి మార్చకూడదు.
        
        ఓసీఆర్ డేటాలో ఏది పూర్తి సూత్రం; ఏవి వృత్తి, ఉదాహరణలు; ఏది వ్యాఖ్యానము; ఏవి విశేషాలు; తెలుసుకోడానికి ఉదాహరణలు:

        "టవర్ణకం బాడ్వాదుల కగు నగుచో నంత లోపంబగు. 7" (పూర్తి సూత్రం)
        "ఆడు... ఆట; ఊరు... ఊట; ఊఱడు... ఊఱట; ఏఁకరు... ఏంకట;
        ఓడు...ఓట; తివురు.... తివుట; తేరు... తేట; త్రిమ్మరు.... త్రిమ్మట; పండు....పంట;
        పాడు...పాట; మండు. మట; వండు.... వంట; వేసఱు....వేసట.
        అనుప్రయుక్తంబయిన యాట శబ్దంబు ప్రాయికంబుగాఁ గ్లీబసమంబగు. కోలాటము,
        పోరాటము, మండ్రాటము - ఇత్యాదులు." (వృత్తి, ఉదాహరణలు)
        -
        "వ్యా. ఆడు - మొదలగు ధాతువులకు ట వర్ణకమగును. అగుచోఁ దుది
        యక్షరమునకు లోపమగును. ఆడు+ట = ఆట. తత్సమ 39చే - ఆట+ము. 57చే
        ఆట. ఇట్లే తక్కినవి. అనుప్రయుక్తమగు ఆట శబ్దము తఱచుగాఁ గ్లీబసమమగును. కోల+ఆట.
        కోలాట. తత్సమ 39చే - కోలాటము. ఇట్లే పోరాడు+ట = పోరాటము.
        సంధి 4చే
        మండుట
        =
        మండ్రాటము." (వ్యాఖ్యానము)
        "విశే :- ప్రాయికంబుగా - అనుటచే, కోలాట అనియు నుండును." (విశేషాలు)
        -
        "టువర్ణకంబు పడ్వాదుల కగు నగుచో నవి యాద్యక్షర శేషంబులయి
        దీర్ఘంబు నొందు. 8" (పూర్తి సూత్రం)
        పడు... పాట, చెడు... చేటు, అడుచు... ఆటు, కఱచు... కాటు, పొడుచు...
        పోటు, వయిచు....వాటు, ఏయు.... ఏటు, ఓడు....ఓటు, వ్రేయు.... వ్రేటు. (వృత్తి, ఉదాహరణలు)
        =
        .
        "వ్యా. పడు
        పడు - మొదలగు ధాతువులకు 'టు' ప్రత్యయమగును. అగుచో వాని
        తొలి యక్షరము మిగిలి దీర్ఘమునందును. పడు+టు, అనుచో - డు వర్ణము లోపించి,
        శేషించిన మొదటి యక్షరమునకు దీర్ఘము : పా+టు = పాటు. తత్సమ 48చే - పాటు+వు.
        :
        పాటు. ఇట్లే చెడు+టు = చే+టు : చేటు. అడుచు+టు = ఆ+టు : ఆటు. మొ||" (వ్యాఖ్యానము)
        "పాఠపరిశీలన :- ప్రథమ ముద్రణమున, కఱుచు
        ప్రథమ ముద్రణమున, కఱుచు - అని కలదు. కఱచు అనియే
        57 చే
        -
        యుండవలెను." (పాఠపరిశీలన)
        "పవర్ణకంబు తిరియ్వాదుల కగు. 9" (పూర్తి సూత్రం)
        తిరియు....తిరిపము, కలియున కత్వంబునగు : కలపము, పొలయు....
        పొలపము, మురియు.... మురిపము : సొలయు....సొలపము,
        వలియు.... వలిపము." (వృత్తి, ఉదాహరణలు)
        "వ్యా. తిరియు - మొదలగు వానికి, ప వర్ణకమగును. తిరియు+ప - 2చే
        తిరి+ప. ఆచ్చిక 2చే - క్లీబస్మమగును గాన, తత్సమ 39చే - తిరిప+ము = తిరిపము,
        కలియు ధాతువునకు అత్వము సైతమగును. కలియు+ప. 2చే - కలి+ప
        “కలియునకత్వంబునగు" కల+ప. క్లీబస్మము గాన తత్సమ 39చే కలప +ము =
        పొలప+ము = పొలపము ఇత్యాది.
        కలపము. పొలయు+ప" 
        =
        308
        -(వ్యాఖ్యానము)
        
        యూజర్ ఇన్పుట్ గా ఇచ్చిన (''{query}'') అనే ప్రశ్నకు సంబంధించిన అన్ని సూత్రాలు ఒకదాని తరువాత ఒకటి ai summary లో ఉండాలి.
        
        ⚠️ అత్యంత ముఖ్యం: ప్రతిసారీ AI సొంతంగా లేఅవుట్ తయారు చేయకూడదు. కింది ఇచ్చిన ఖచ్చితమైన **స్టాండర్డ్ HTML టెంప్లేట్** రూపంలో మాత్రమే సమాధానాన్ని అవుట్‌పుట్ ఇవ్వాలి:

        <div style="background-color: #f8f9fa; border-left: 5px solid #0d6efd; padding: 15px; margin-bottom: 15px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <h4 style="color: #0d6efd; font-weight: bold; margin-bottom: 10px;">📜 సూత్రము</h4>
            <p style="font-size: 25px; margin-bottom: 5px;"><strong>పూర్తి సూత్రం:</strong> [ఇక్కడ సూత్రం రాయండి]</p>
            <p style="font-size: 20px; color: #6c757d; margin-bottom: 0;"><strong>గ్రంథ సూచన:</strong> [గ్రంథం, పరిచ్ఛేదం, సూత్ర సంఖ్య]</p>
        </div>

        <div style="background-color: #fff3cd; border-left: 5px solid #ffc107; padding: 15px; margin-bottom: 15px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <h4 style="color: #856404; font-weight: bold; margin-bottom: 5px;">💡 వృత్తి / వివరణ</h4>
            <p style="font-size: 20px; margin-bottom: 0;">[ఇక్కడ వృత్తి, వ్యాఖ్యాత వివరణ రాయండి]</p>
        </div>

        <div style="background-color: #d1e7dd; border-left: 5px solid #198754; padding: 15px; margin-bottom: 15px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <h4 style="color: #0f5132; font-weight: bold; margin-bottom: 10px;">🌟 ఉదాహరణలు</h4>
            <div style="background: #ffffff; padding: 10px; border-radius: 4px; border: 1px solid #badbcc;">
                [ఇక్కడ ఉదాహరణలను రంగుల బాక్సులు లేదా బోల్డ్ అక్షరాలతో ఇవ్వండి]
            </div>
        </div>
        
        <div style="background-color: #d1e7dd; border-left: 5px solid #198754; padding: 15px; margin-bottom: 15px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <h4 style="color: #0f5132; font-weight: bold; margin-bottom: 10px;">🌟 రూపసాధన</h4>
            <div style="background: #ffffff; padding: 10px; border-radius: 4px; border: 1px solid #badbcc;">
                [ఇక్కడ రూపసాధనను రంగుల బాక్సులు లేదా బోల్డ్ అక్షరాలతో ఇవ్వండి]
            </div>
        </div>

        <div style="background-color: #cfe2ff; border-left: 5px solid #0dcaf0; padding: 15px; margin-bottom: 15px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <h4 style="color: #055160; font-weight: bold; margin-bottom: 10px;">🔍 విశేషాలు, పాఠపరిశీలన</h4>
            <p style="font-size: 20px; margin-bottom: 5px;"><strong>విశే :-</strong> [విశేషాంశాలు]</p>
            <p style="font-size: 20px; margin-bottom: 0;"><strong>పాఠపరిశీలన :-</strong> [పాఠపరిశీలన వివరాలు]</p>
        </div>
        
        - No preamble, no closing summary. Start directly with the HTML template structure given above.

        {combined_text}"""

        # Grammar books (బాలవ్యాకరణము / ప్రౌఢవ్యాకరణము) keep the strict సూత్రము
        # template; every other book gets the general, template-free answer.
        if _has_grammar_source(snippets_list):
            prompt = grammar_prompt
        else:
            prompt = GENERAL_PROMPT.format(query=query, combined_text=combined_text)

        # 🌟 ఇక్కడ Temperature 0.0 కి సెట్ చేయబడింది.
        chat = client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                temperature=0.0,
                top_p=0.1,
                max_output_tokens=8192,
            )
        )
        response = chat.send_message(prompt)
        return response.text if response and response.text else "AI సేవలు అందుబాటులో లేవు."
    except Exception as e:
        print("Gemini AI Error:", str(e))
        return "సారాంశం రూపొందించడంలో సాంకేతిక లోపం ఏర్పడింది."

def get_telugu_regex(word):
    # ముందుగా యూజర్ ఇచ్చిన పదంలో అరసున్నా ఉంటే తీసేస్తాం
    clean_word = word.replace('ఁ', '')
    # స్పెషల్ క్యారెక్టర్స్ ( ?, *, . లాంటివి) ఉంటే ఎస్కేప్ చేస్తాం
    escaped_chars = [re.escape(c) for c in clean_word]
    # ప్రతి అక్షరం తర్వాత అరసున్నా (ఁ) ఆప్షనల్ గా (ఁ?) వచ్చేలా కలుపుతాం
    return 'ఁ?'.join(escaped_chars) + 'ఁ?'

@app.get("/search-pdf-highlight")
def search_pdf_highlight(request: Request, query: str = Query(...), books: str = Query(None)):
    user = request.session.get('user')
    if not user: return {"error": "Unauthorized"}
    
    cleaned_query = query.strip()
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    selected_books = [b.strip() for b in books.split(",")] if books else []
    book_filter = ""
    book_params = []
    if selected_books:
        placeholders = ",".join(["%s"] * len(selected_books))
        book_filter = f" AND book_title IN ({placeholders})"
        book_params = selected_books

    matches = []
    
    stop_words = ["లో", "కి", "కు", "ను", "ని", "యొక్క", "అంటే", "ఏమిటి", "ఇవ్వు", "గురించి", "అనే", "రూపాన్ని", "సాధించవచ్చునా", "చేయవచ్చునా"]
    words = [w for w in cleaned_query.split() if len(w) > 1 and w not in stop_words]
    if not words: words = [cleaned_query]

    # 🌟 1. Regex ప్యాట్రన్ తయారు చేయడం
    query_regex = get_telugu_regex(cleaned_query)

    # 🌟 2. ILIKE బదులు ~* (Regex) వాడటం
    cursor.execute(f'''
        SELECT book_title, filename, page_number, extracted_text, word_boxes 
        FROM pdf_text_index 
        WHERE (extracted_text ~* %s OR book_title ILIKE %s){book_filter}
        ORDER BY page_number ASC
    ''', tuple([query_regex, f'%{cleaned_query}%'] + book_params))
    matches = [dict(row) for row in cursor.fetchall()]

    # విడివిడి పదాలతో వెతకడం
    if not matches and words:
        conditions = " OR ".join(["extracted_text ~* %s" for _ in words])
        params = [get_telugu_regex(w) for w in words]
        
        cursor.execute(f'''
            SELECT book_title, filename, page_number, extracted_text, word_boxes 
            FROM pdf_text_index 
            WHERE ({conditions}){book_filter}
            ORDER BY page_number ASC
        ''', tuple(params + book_params))
        matches = [dict(row) for row in cursor.fetchall()]
        
    conn.close()
    
    results = []
    ai_items = []
    for m in matches:
        text = m['extracted_text'] or ""
        pos = -1
        matched_w = cleaned_query
        
        # 🌟 3. స్నిప్పెట్ కోసం పైథాన్ లో Regex వాడి వెతకడం 
        match = re.search(query_regex, text)
        if match:
            pos = match.start()
            matched_w = match.group() # ఇది 'పూఁబోఁడి' ని ఖచ్చితంగా క్యాచ్ చేస్తుంది
        else:
            for w in words:
                w_regex = get_telugu_regex(w)
                match_w = re.search(w_regex, text)
                if match_w:
                    pos = match_w.start()
                    matched_w = match_w.group()
                    break
                    
        if pos == -1: pos = 0
            
        snippet = text[max(0, pos - 400):min(len(text), pos + 1600)].replace('\n', ' ')
        # The AI reads the FULL page text (capped), not the display window, so its
        # answer is never cut off mid-passage.
        ai_items.append({
            "book_title": m['book_title'],
            "page_number": m['page_number'],
            "snippet": text.strip()[:6000],
        })
        filename = m.get('filename')
        highlighted_img_b64 = ""
        
        if not filename and m.get('book_title'):
            for f in os.listdir(DOWNLOAD_DIR):
                if m['book_title'][:10] in f and f.endswith('.pdf'):
                    filename = f
                    break
                    
        if filename and len(results) < 10:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.exists(filepath):
                doc = None
                try:
                    doc = fitz.open(filepath)
                    page = doc[m['page_number'] - 1]
                    pix = page.get_pixmap(dpi=100)
                    img_bytes = pix.tobytes("png")
                    highlighted_img_b64 = base64.b64encode(img_bytes).decode('utf-8')
                except: pass
                finally:
                    if doc is not None:
                        try: doc.close()
                        except: pass

        results.append({
            "book_title": m['book_title'],
            "filename": filename or "",
            "page_number": m['page_number'],
            "snippet": f"...{snippet}...",
            "word_boxes": json.loads(m['word_boxes']) if m.get('word_boxes') else [],
            "query": matched_w,  # ఇక్కడ అరసున్నాతో దొరికిన అసలు పదం వెళుతుంది
            "page_image": highlighted_img_b64
        })
        
    ai_summary = generate_ai_summary(cleaned_query, ai_items[:8]) if ai_items else "మీరు అడిగిన అంశానికి సంబంధించిన సమాచారం ఎంచుకున్న గ్రంథాల OCR డేటాలో లభించలేదు."

    # ఛందస్సు: for poetry (non-grammar) searches, ask the Chandam API for the metre.
    chandassu = None
    if ai_items and not _has_grammar_source(ai_items):
        verse_text = _extract_verse(ai_summary) or ai_items[0]["snippet"][:1500]
        chandassu = analyze_chandassu(verse_text)

    return {
        "query": cleaned_query,
        "ai_summary": ai_summary,
        "chandassu": chandassu,
        "results": results
    }
    
@app.get("/view-pdf")
def view_pdf(filename: str, page: int = 1):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type='application/pdf')
    return {"error": "File not found"}