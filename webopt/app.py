
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import imaplib
import email
from email.header import decode_header
import re
import html
import threading
import time
from imapclient import IMAPClient
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
import gc

load_dotenv()

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'mp4', 'webm', 'mov'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max limit

import hashlib
_raw = os.environ.get('DATABASE_URL', secrets.token_hex(32))
_secret = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.secret_key = hashlib.sha256(_secret.encode()).hexdigest()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

_login_attempts = {}
_login_lock = threading.Lock()

def is_rate_limited(ip):
    with _login_lock:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        _login_attempts[ip] = attempts
        if len(attempts) >= 10:
            return True
        attempts.append(now)
        _login_attempts[ip] = attempts
        return False

def _detect_database_url():
    url = os.environ.get('DATABASE_URL')
    if url and url.startswith('postgresql'):
        return url
    _pghost = os.environ.get('PGHOST')
    if _pghost:
        _pgport = os.environ.get('PGPORT', '5432')
        _pguser = os.environ.get('PGUSER', 'runner')
        _pgpass = os.environ.get('PGPASSWORD', '')
        _pgdb = os.environ.get('PGDATABASE', 'postgres')
        return f"postgresql://{_pguser}:{_pgpass}@{_pghost}:{_pgport}/{_pgdb}"
    print("⚠️ DATABASE_URL no encontrada.")
    print("⚠️ En VPS: configura DATABASE_URL en el archivo .env")
    print("⚠️ En Replit: crea una base de datos PostgreSQL desde Tools > Database")
    return None

DATABASE_URL = _detect_database_url()

_db_initialized = False

socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins="*"
)

def get_db_connection():
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        return conn
    except Exception as e:
        print(f"Error conectando a la DB: {e}")
        return None

def get_accounts():
    conn = get_db_connection()
    if not conn: return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT email, app_password FROM accounts")
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception:
        return []
    finally:
        try: conn.close()
        except Exception: pass

def init_db():
    global _db_initialized, DATABASE_URL
    print("🛠️ INICIALIZANDO BASE DE DATOS AUTOMÁTICA...")
    if not DATABASE_URL:
        DATABASE_URL = _detect_database_url()
    conn = get_db_connection()
    if not conn:
        print("❌ No se pudo conectar a la DB.")
        print("❌ En VPS: verifica DATABASE_URL en tu archivo .env")
        print("❌ En Replit: crea una base de datos PostgreSQL (Tools > Database)")
        return False
    
    try:
        conn.autocommit = True
        cur = conn.cursor()
        
        # 1. Asegurar esquema y permisos
        try:
            cur.execute("GRANT ALL ON SCHEMA public TO public")
        except: pass
        
        # 2. Creación automática de tablas (Unificado y Estandarizado)
        cur.execute('CREATE TABLE IF NOT EXISTS otps (id SERIAL PRIMARY KEY, sender TEXT, account TEXT, subject TEXT, code TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        cur.execute('CREATE TABLE IF NOT EXISTS accounts (id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, app_password TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        cur.execute('CREATE TABLE IF NOT EXISTS admin_users (id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL)')
        cur.execute('CREATE TABLE IF NOT EXISTS user_credentials (id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, expires_at TIMESTAMP)')
        cur.execute('CREATE TABLE IF NOT EXISTS settings (id SERIAL PRIMARY KEY, key TEXT UNIQUE, value TEXT)')
        
        # 3. Crear/actualizar administrador permanente
        ADMIN_USER = os.environ.get('ADMIN_USER', 'paudronixGt20p')
        ADMIN_PASS = os.environ.get('ADMIN_PASS', 'paudronixADM20a')
        cur.execute("DELETE FROM admin_users")
        cur.execute("INSERT INTO admin_users (username, password) VALUES (%s, %s)", (ADMIN_USER, ADMIN_PASS))
        
        # 4. Cargar cuentas por defecto si está vacío
        cur.execute("SELECT COUNT(*) FROM accounts")
        res = cur.fetchone()
        if res and res[0] == 0:
            default_accounts = [
                ("propaublite@gmail.com", "zczzcnpyhrzqbpgl"),
                ("paublutegt@gmail.com", "nvkvbiymuouxjmkf"),
                ("popupa083@gmail.com", "pcvyhpdrbrsyghok"),
                ("pakistepa254@gmail.com", "zzzhexfwvilikwwf")
            ]
            for e, p in default_accounts:
                cur.execute("INSERT INTO accounts (email, app_password) VALUES (%s, %s) ON CONFLICT DO NOTHING", (e, p))
        
        cur.close()
        conn.close()
        _db_initialized = True
        print("✅ SISTEMA AUTOMÁTICO LISTO")
        return True
    except Exception as e:
        if conn: conn.close()
        print(f"❌ Error crítico en DB: {e}")
        return False

def save_otp(sender, account, subject, code):
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (sender, account, subject, code) VALUES (%s, %s, %s, %s)", (sender, account, subject, code))
        conn.commit(); cur.close()
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass

def get_history():
    conn = get_db_connection()
    if not conn: return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT sender, account, subject, code, timestamp FROM otps WHERE timestamp > NOW() - INTERVAL '1 hour' ORDER BY timestamp DESC LIMIT 50")
        rows = cur.fetchall(); cur.close()
        for row in rows:
            if row['timestamp']:
                local_time = row['timestamp'] + timedelta(hours=6)
                row['time'] = local_time.strftime("%I:%M %p")
            if 'timestamp' in row:
                del row['timestamp']
        return rows
    except Exception:
        return []
    finally:
        try: conn.close()
        except Exception: pass

def decode_mime_words(s):
    if not s: return ""
    parts = decode_header(s)
    decoded = ""
    for part, encoding in parts:
        if isinstance(part, bytes):
            try: decoded += part.decode(encoding or "utf-8", errors="ignore")
            except: decoded += part.decode("utf-8", errors="ignore")
        else: decoded += part
    return decoded

def strip_html_tags(text: str) -> str:
    if not text: return ""
    text = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', ' ', text)
    text = re.sub(r'(?s)<.*?>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

NON_OTP_KEYWORDS = [
    'unsubscribe', 'newsletter',
    'invoice', 'factura', 'receipt', 'recibo',
    'your order', 'tu pedido', 'order confirmation', 'shipping',
    'promotion', 'offer expires', 'sale ends',
]

OTP_KEYWORDS = [
    'code', 'codigo', 'código', 'otp', 'pin', 'token',
    'verification', 'verificacion', 'verificación', 'verify', 'verificar',
    'confirm', 'confirmar', 'confirmación', 'confirmation',
    'security', 'seguridad', 'authenticate', 'autenticar',
    'one-time', 'one time', 'password', 'contraseña',
    'login', 'sign in', 'iniciar sesion', 'access', 'acceso',
    'two-factor', '2fa', 'mfa', 'multi-factor',
]

def is_otp_email(subject: str, body: str) -> bool:
    text = (subject + " " + body).lower()
    for kw in NON_OTP_KEYWORDS:
        if kw in text:
            return False
    for kw in OTP_KEYWORDS:
        if kw in text:
            return True
    if re.search(r'(?<!\d)\d{4,8}(?!\d)', text):
        return True
    return False

def extract_otp_code(text: str, subject: str = "", sender: str = ""):
    if not text: return None
    full_text = (text + " " + (subject or "")).replace("\n", " ")
    patterns = [
        r'(?:code|codigo|código|otp|pin|token|clave|key|verification|verificacion)[:\s\-–—=]*(\d{4,8})',
        r'(\d{4,8})\s*(?:is your|es tu|es su|es el)',
        r'(?:enter|ingresa|ingrese|usa|use|utiliza)[:\s]+(\d{4,8})',
        r'(?<!\d)(\d{4,8})(?!\d)',
    ]
    for p in patterns:
        match = re.search(p, full_text, re.IGNORECASE)
        if match:
            code = match.group(1)
            is_netflix = 'netflix' in sender.lower() or 'netflix' in (subject or "").lower()
            if is_netflix:
                return code[:4]
            return code
    return None

def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ['text/plain', 'text/html']:
                try: return strip_html_tags(part.get_payload(decode=True).decode(errors='ignore'))
                except: pass
    else:
        try: return strip_html_tags(msg.get_payload(decode=True).decode(errors='ignore'))
        except: pass
    return ""

_processed_lock = threading.Lock()

def _emit_otp(sender, email_addr, subject, otp, real_email_time, processed_otps):
    import email.utils as eu
    otp_id = f"{sender}_{email_addr}_{otp}"
    with _processed_lock:
        if otp_id in processed_otps:
            return
        processed_otps.add(otp_id)
    socketio.emit('new_otp', {'sender': sender, 'account': email_addr, 'subject': subject, 'code': otp, 'time': real_email_time})
    save_otp(sender, email_addr, subject, otp)

def _fetch_and_emit(client, email_addr, uids, processed_otps, ignore_age=False):
    import email as email_pkg
    import email.utils as eu
    if not uids:
        return
    try:
        batch = client.fetch(uids, ['RFC822'])
    except Exception as e:
        print(f"  [{email_addr}] Error fetch: {e}")
        return
    for uid in uids:
        try:
            if uid not in batch:
                continue
            raw = batch[uid].get(b'RFC822') or batch[uid].get('RFC822')
            if not raw:
                continue
            msg = email_pkg.message_from_bytes(raw)
            subject = decode_mime_words(msg.get("subject") or "")
            sender = decode_mime_words(msg.get("from") or "")
            email_date = msg.get("Date")
            if not email_date:
                continue
            try:
                parsed_date = eu.parsedate_to_datetime(email_date)
                now_aware = datetime.now(parsed_date.tzinfo)
                age_seconds = (now_aware - parsed_date).total_seconds()
            except Exception:
                age_seconds = 0
            if not ignore_age and age_seconds > 1800:
                continue
            real_email_time = datetime.now().strftime("%I:%M %p")
            body = get_email_body(msg)
            if not is_otp_email(subject, body):
                continue
            otp = extract_otp_code(body, subject, sender)
            if otp:
                print(f"  [{email_addr}] OTP de {sender[:35]}")
                _emit_otp(sender, email_addr, subject, otp, real_email_time, processed_otps)
            else:
                print(f"  [{email_addr}] Sin codigo en: {subject[:50]}")
        except Exception as e:
            print(f"  [{email_addr}] Error procesando email: {e}")

def _quick_fetch_new(client, email_addr, last_seen_uid, processed_otps):
    try:
        time.sleep(0.2)
        all_msgs = client.search(['ALL'])
        if all_msgs:
            new_uids = [u for u in all_msgs if u > last_seen_uid]
            if new_uids:
                _fetch_and_emit(client, email_addr, new_uids[-5:], processed_otps, ignore_age=True)
                return max(all_msgs)
        return last_seen_uid
    except Exception as e:
        print(f"  [{email_addr}] Error en busqueda: {e}")
        return last_seen_uid

def idle_account(account, processed_otps):
    email_addr, app_pass = account["email"], account["app_password"]
    last_seen_uid = 0
    consecutive_errors = 0
    while True:
        client = None
        try:
            print(f"  [{email_addr}] Conectando a Gmail IMAP...")
            client = IMAPClient("imap.gmail.com", ssl=True, timeout=30)
            client.login(email_addr, app_pass)
            client.select_folder("INBOX", readonly=True)
            all_msgs = client.search(['ALL'])
            if all_msgs:
                new_max = max(all_msgs)
                if last_seen_uid == 0:
                    last_seen_uid = new_max
                    print(f"  [{email_addr}] Conectado OK - UID base: {last_seen_uid}")
                else:
                    new_uids = [u for u in all_msgs if u > last_seen_uid]
                    if new_uids:
                        _fetch_and_emit(client, email_addr, new_uids[-5:], processed_otps, ignore_age=True)
                    last_seen_uid = new_max
                    print(f"  [{email_addr}] Reconectado OK - UID: {last_seen_uid}")
            else:
                print(f"  [{email_addr}] Conectado OK - inbox vacio")
            consecutive_errors = 0
            client.idle()
            idle_start = time.time()
            while True:
                responses = client.idle_check(timeout=8)
                if responses:
                    has_new = any(
                        (isinstance(r, tuple) and len(r) >= 2 and
                         (r[1] == b'EXISTS' or r[1] == 'EXISTS'))
                        for r in responses
                    )
                    if has_new:
                        client.idle_done()
                        print(f"  [{email_addr}] Email nuevo detectado!")
                        last_seen_uid = _quick_fetch_new(client, email_addr, last_seen_uid, processed_otps)
                        gc.collect()
                        client.idle()
                        idle_start = time.time()
                        continue
                if time.time() - idle_start > 60:
                    client.idle_done()
                    client.noop()
                    last_seen_uid = _quick_fetch_new(client, email_addr, last_seen_uid, processed_otps)
                    gc.collect()
                    client.idle()
                    idle_start = time.time()
        except Exception as e:
            consecutive_errors += 1
            err = str(e)
            print(f"  [{email_addr}] ERROR: {err}")
            if "AUTHENTICATIONFAILED" in err or "Invalid credentials" in err:
                print(f"  [{email_addr}] CONTRASENA INCORRECTA - verifica la app password de Gmail")
                time.sleep(120)
            elif "Too many simultaneous" in err or "Failure" in err:
                print(f"  [{email_addr}] Gmail limito conexiones - esperando 60s")
                time.sleep(60)
            else:
                wait = min(5 * consecutive_errors, 60)
                time.sleep(wait)
        finally:
            if client:
                try:
                    client.logout()
                except Exception:
                    pass
            gc.collect()

def check_emails():
    print("🛰️ MODO IDLE ACTIVO — OTPs EN TIEMPO REAL...")
    processed_otps = set()
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT sender, account, code FROM otps ORDER BY timestamp DESC LIMIT 100")
            for r, a, c in cur.fetchall():
                processed_otps.add(f"{r}_{a}_{c}")
            cur.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass

    active_threads = {}
    check_count = 0
    while True:
        try:
            accounts = get_accounts()
            current_emails = {acc["email"] for acc in accounts}
            for acc in accounts:
                email_addr = acc["email"]
                t = active_threads.get(email_addr)
                if t is None or not t.is_alive():
                    if t is not None:
                        print(f"  [{email_addr}] Hilo muerto - reconectando...")
                    t = threading.Thread(target=idle_account, args=(acc, processed_otps), daemon=True)
                    t.start()
                    active_threads[email_addr] = t
                    time.sleep(2)
            for dead in [e for e in list(active_threads) if e not in current_emails]:
                del active_threads[dead]
            check_count += 1
            if check_count % 40 == 0:
                alive = sum(1 for t in active_threads.values() if t.is_alive())
                print(f"  [MONITOR] {alive}/{len(active_threads)} cuentas activas")
            gc.collect()
        except Exception as e:
            print(f"  [MONITOR] Error: {e}")
        time.sleep(15)

@app.route('/')
def index():
    global _db_initialized
    if not _db_initialized:
        if init_db():
            threading.Thread(target=check_emails, daemon=True).start()
    if not _db_initialized:
        return '''<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OTP Pro — Configuración</title>
<style>body{font-family:sans-serif;background:#060a12;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:rgba(255,255,255,0.05);border:1px solid rgba(0,229,255,0.2);border-radius:20px;padding:40px;max-width:550px;text-align:center}
h1{color:#00e5ff;margin-bottom:20px}p{color:rgba(255,255,255,0.6);line-height:1.8;margin-bottom:15px}
h3{color:#f9d423;margin:20px 0 10px;font-size:0.9rem;letter-spacing:1px}
.step{background:rgba(0,229,255,0.08);border-radius:10px;padding:12px 15px;margin:8px 0;text-align:left;font-size:0.9rem}
.step b{color:#00e5ff}.btn{display:inline-block;margin-top:20px;padding:12px 30px;background:#00e5ff;color:#000;
border-radius:10px;text-decoration:none;font-weight:bold}</style></head><body><div class="box">
<h1>OTP PRO</h1><p>La base de datos no está configurada.</p>
<h3>EN VPS (Digital Ocean, etc.)</h3>
<div class="step"><b>1.</b> Crea el archivo <b>.env</b> en la carpeta del proyecto</div>
<div class="step"><b>2.</b> Agrega: <b>DATABASE_URL=postgresql://usuario:contraseña@localhost:5432/otp_db</b></div>
<div class="step"><b>3.</b> Reinicia la app: <b>sudo systemctl restart otppro</b></div>
<h3>EN REPLIT</h3>
<div class="step"><b>1.</b> Ve a <b>Tools</b> en el panel izquierdo</div>
<div class="step"><b>2.</b> Haz clic en <b>Database / PostgreSQL</b></div>
<div class="step"><b>3.</b> Crea la base de datos y reinicia</div>
<a href="/" class="btn">REINTENTAR</a></div></body></html>''', 200
    if not session.get('user_logged_in') and not session.get('logged_in'):
        return render_template('login_choice.html', background_url=get_background_url('login'))
    return render_template('index.html', background_url=get_background_url('panel'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr
        if is_rate_limited(ip):
            return render_template('login_choice.html', background_url=get_background_url('login'), error="Demasiados intentos. Espere 5 minutos.")
        u, p = request.form.get('username'), request.form.get('password')
        if not u or not p:
            return render_template('login_choice.html', background_url=get_background_url('login'), error="Complete todos los campos", tab="admin")
        ADMIN_USER_ENV = os.environ.get('ADMIN_USER', 'paudronixGt20p')
        ADMIN_PASS_ENV = os.environ.get('ADMIN_PASS', 'paudronixADM20a')
        if u == ADMIN_USER_ENV and p == ADMIN_PASS_ENV:
            session['logged_in'] = True
            session.permanent = False
            return redirect(url_for('admin'))
        return render_template('login_choice.html', background_url=get_background_url('login'), error="Datos inválidos", tab="admin")
    return redirect(url_for('index'))

@app.route('/user_login', methods=['GET', 'POST'])
def user_login():
    if request.method == 'POST':
        u, p = request.form.get('username'), request.form.get('password')
        if not u or not p:
            return render_template('login_choice.html', background_url=get_background_url('login'), error="Complete todos los campos", tab="user")
        conn = get_db_connection()
        if not conn: return render_template('login_choice.html', background_url=get_background_url('login'), error="Error de conexion. Intente de nuevo.", tab="user")
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM user_credentials WHERE username = %s AND password = %s", (u, p))
            user = cur.fetchone(); cur.close()
            if user:
                if user['expires_at'] and user['expires_at'] < datetime.now():
                    return render_template('login_choice.html', background_url=get_background_url('login'), error="Su cuenta ha expirado.", tab="user")
                session['user_logged_in'] = True
                return redirect('/')
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
        return render_template('login_choice.html', background_url=get_background_url('login'), error="Datos inválidos", tab="user")
    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('user_logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin')
def admin():
    if not session.get('logged_in'): return redirect(url_for('login'))
    conn = get_db_connection()
    if not conn: return "Error de base de datos"
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_credentials")
        users = cur.fetchall(); cur.close()
        return render_template('admin.html', accounts=get_accounts(), users=users, bg_login=get_background_url('login'), bg_panel=get_background_url('panel'))
    except Exception:
        return "Error al cargar datos"
    finally:
        try: conn.close()
        except Exception: pass

@app.route('/admin/add_user', methods=['POST'])
def add_user():
    if not session.get('logged_in'): return redirect(url_for('login'))
    u, p, days = request.form.get('username'), request.form.get('password'), request.form.get('days')
    if u and p:
        conn = get_db_connection()
        if not conn: return redirect(url_for('admin'))
        try:
            cur = conn.cursor()
            expires_at = datetime.now() + timedelta(days=int(days)) if days else datetime.now() + timedelta(days=30)
            cur.execute("INSERT INTO user_credentials (username, password, expires_at) VALUES (%s, %s, %s) ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password, expires_at = EXCLUDED.expires_at", (u, p, expires_at))
            conn.commit(); cur.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    return redirect(url_for('admin'))

@app.route('/admin/delete_user', methods=['POST'])
def delete_user():
    if not session.get('logged_in'): return redirect(url_for('login'))
    u = request.form.get('username')
    if u:
        conn = get_db_connection()
        if not conn: return redirect(url_for('admin'))
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_credentials WHERE username = %s", (u,))
            conn.commit(); cur.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    return redirect(url_for('admin'))

@app.route('/admin/delete', methods=['POST'])
def delete_account():
    if not session.get('logged_in'): return redirect(url_for('login'))
    e = request.form.get('email')
    if e:
        conn = get_db_connection()
        if not conn: return redirect(url_for('admin'))
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM accounts WHERE email = %s", (e,))
            conn.commit(); cur.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    return redirect(url_for('admin'))

def get_background_url(bg_type='login'):
    key = 'bg_login' if bg_type == 'login' else 'bg_panel'
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone(); cur.close()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        try: conn.close()
        except Exception: pass

@app.route('/admin/get_bg_url')
def get_bg_url_api():
    return {"login": get_background_url('login'), "panel": get_background_url('panel')}

@app.route('/admin/upload_bg', methods=['POST'])
def upload_bg():
    if not session.get('logged_in'): return redirect(url_for('login'))
    bg_type = request.form.get('bg_type', 'login')
    key = 'bg_login' if bg_type == 'login' else 'bg_panel'
    file = request.files.get('background')
    if file and file.filename and allowed_file(file.filename):
        old_bg = get_background_url(bg_type)
        if old_bg:
            old_path = os.path.join(UPLOAD_FOLDER, os.path.basename(old_bg))
            if os.path.exists(old_path):
                os.remove(old_path)
        filename = secure_filename(f"{bg_type}_{file.filename}")
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        bg_url = f"/static/uploads/{filename}"
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, bg_url))
                conn.commit(); cur.close()
            except Exception:
                pass
            finally:
                try: conn.close()
                except Exception: pass
    return redirect(url_for('admin'))

@app.route('/admin/delete_bg', methods=['POST'])
def delete_bg():
    if not session.get('logged_in'): return redirect(url_for('login'))
    bg_type = request.form.get('bg_type', 'login')
    key = 'bg_login' if bg_type == 'login' else 'bg_panel'
    old_bg = get_background_url(bg_type)
    if old_bg:
        old_path = os.path.join(UPLOAD_FOLDER, os.path.basename(old_bg))
        if os.path.exists(old_path):
            os.remove(old_path)
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM settings WHERE key = %s", (key,))
            conn.commit(); cur.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    return redirect(url_for('admin'))

@app.route('/admin/add', methods=['POST'])
def add_account():
    if not session.get('logged_in'): return redirect(url_for('login'))
    e, p = request.form.get('email'), request.form.get('app_password')
    if e and p:
        conn = get_db_connection()
        if not conn: return redirect(url_for('admin'))
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO accounts (email, app_password) VALUES (%s, %s) ON CONFLICT (email) DO UPDATE SET app_password = EXCLUDED.app_password", (e, p))
            conn.commit(); cur.close()
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    return redirect(url_for('admin'))

@socketio.on('ping_test')
def handle_ping(data): socketio.emit('pong_response', {'message': 'Conexión OK'})

@socketio.on('get_history')
def handle_history(): socketio.emit('history_data', get_history())

if __name__ == '__main__':
    if init_db():
        threading.Thread(target=check_emails, daemon=True).start()
    else:
        print("⚠️ App iniciada SIN base de datos. Configura PostgreSQL y reinicia.")
    socketio.run(app, host='0.0.0.0', port=5000, log_output=True, use_reloader=False, allow_unsafe_werkzeug=True)
