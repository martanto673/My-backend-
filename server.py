#!/usr/bin/env python3
"""
MediLinka Live Backend — Pure Python, no external dependencies
SQLite database, full REST API, CORS enabled
Runs on http://localhost:3001
"""

import json
import sqlite3
import hashlib
import hmac
import uuid
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from urllib.parse import urlparse, parse_qs
import base64
import re
import os

DB_PATH = "/tmp/medilinka.db"  # Changed for Render deployment
JWT_SECRET = 'medilinka_dev_jwt_secret_change_in_production'
PLATFORM_FEE = 0.05

# ─── Database ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT,
        phone TEXT,
        country_code TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS pharmacies (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        address TEXT,
        country_code TEXT NOT NULL,
        license_number TEXT UNIQUE NOT NULL,
        is_verified INTEGER DEFAULT 1,
        is_open INTEGER DEFAULT 1,
        contact_phone TEXT,
        contact_email TEXT
    );

    CREATE TABLE IF NOT EXISTS medicines (
        id TEXT PRIMARY KEY,
        pharmacy_id TEXT REFERENCES pharmacies(id),
        country_code TEXT NOT NULL,
        name TEXT NOT NULL,
        strength TEXT NOT NULL,
        description TEXT,
        category TEXT NOT NULL,
        requires_rx INTEGER DEFAULT 0,
        price REAL NOT NULL,
        currency TEXT NOT NULL,
        stock_qty INTEGER DEFAULT 0,
        reserved_qty INTEGER DEFAULT 0,
        icon TEXT,
        is_active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        user_id TEXT REFERENCES users(id),
        pharmacy_id TEXT REFERENCES pharmacies(id),
        country_code TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        delivery_method TEXT NOT NULL,
        delivery_name TEXT,
        delivery_phone TEXT,
        delivery_address TEXT,
        delivery_city TEXT,
        subtotal REAL NOT NULL,
        delivery_fee REAL DEFAULT 0,
        total REAL NOT NULL,
        currency TEXT NOT NULL,
        idempotency_key TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL,
        paid_at TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS order_items (
        id TEXT PRIMARY KEY,
        order_id TEXT REFERENCES orders(id),
        medicine_id TEXT REFERENCES medicines(id),
        name TEXT NOT NULL,
        strength TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        line_total REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stock_reservations (
        id TEXT PRIMARY KEY,
        order_id TEXT REFERENCES orders(id),
        medicine_id TEXT REFERENCES medicines(id),
        quantity INTEGER NOT NULL,
        is_active INTEGER DEFAULT 1,
        reserved_at TEXT DEFAULT (datetime('now')),
        released_at TEXT,
        released_reason TEXT
    );

    CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        order_id TEXT REFERENCES orders(id),
        user_id TEXT REFERENCES users(id),
        country_code TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT DEFAULT 'processing',
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        provider_reference TEXT,
        idempotency_key TEXT UNIQUE NOT NULL,
        initiated_at TEXT DEFAULT (datetime('now')),
        completed_at TEXT,
        expires_at TEXT NOT NULL,
        failure_reason TEXT
    );

    CREATE TABLE IF NOT EXISTS ledger_transactions (
        id TEXT PRIMARY KEY,
        order_id TEXT REFERENCES orders(id),
        payment_id TEXT REFERENCES payments(id),
        type TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        gross_amount REAL NOT NULL,
        platform_fee REAL NOT NULL,
        pharmacy_amount REAL NOT NULL,
        currency TEXT NOT NULL,
        country_code TEXT NOT NULL,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS audit_logs (
        id TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        actor_id TEXT,
        actor_type TEXT DEFAULT 'user',
        entity_type TEXT,
        entity_id TEXT,
        country_code TEXT,
        details TEXT,
        ip_address TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS webhook_events (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        event_id TEXT NOT NULL,
        status TEXT DEFAULT 'processed',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(provider, event_id)
    );
    """)

    # Seed pharmacies
    pharmacies = [
        ('ph-ug-001','HealthPlus Pharmacy','Kampala, Nakasero','Plot 14, Nakasero Road','UG','NDA-UG-2019-0041',1,1,'+256772000001','ops@healthplus.ug'),
        ('ph-ug-002','MediCare Uganda','Kampala, Entebbe Rd','Plot 8, Entebbe Road','UG','NDA-UG-2020-0072',1,1,'+256772000002','ops@medicare.ug'),
        ('ph-ug-003','City Pharmacy','Jinja, Main Street','12 Main Street, Jinja','UG','NDA-UG-2018-0033',1,0,'+256772000003','info@citypharm.ug'),
        ('ph-ug-004','WellCare Pharmacy','Mbarara, High Street','5 High Street, Mbarara','UG','NDA-UG-2021-0088',1,1,'+256772000004','ops@wellcare.ug'),
        ('ph-ae-001','Aster Pharmacy','Dubai, Deira','Al Rigga Street, Deira','AE','MOH-AE-2019-1041',1,1,'+971501000001','dubai@asterpharmacy.ae'),
        ('ph-ae-002','Life Pharmacy','Dubai, Downtown','Dubai Mall, Financial Centre Rd','AE','MOH-AE-2018-0882',1,1,'+971501000002','downtown@lifepharmacy.ae'),
        ('ph-ae-003','Boots UAE','Abu Dhabi, Corniche','Corniche Road West, Abu Dhabi','AE','MOH-AE-2020-1133',1,1,'+971501000003','abudhabi@boots.ae'),
        ('ph-ae-004','Bin Sina Pharmacy','Sharjah, Al Majaz','Al Majaz 2, Buhaira Corniche Rd','AE','MOH-AE-2017-0770',1,0,'+971501000004','sharjah@binsina.ae'),
    ]
    c.executemany("""INSERT OR IGNORE INTO pharmacies
        (id,name,city,address,country_code,license_number,is_verified,is_open,contact_phone,contact_email)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", pharmacies)

    # Seed medicines — fixed IDs so INSERT OR IGNORE is idempotent
    medicines = [
        # Uganda
        ('med-ug-001','ph-ug-001','UG','Amoxicillin','500mg','Broad-spectrum penicillin antibiotic','antibiotic',1,12500,'UGX',120,0,'💊',1),
        ('med-ug-002','ph-ug-002','UG','Paracetamol','500mg','Analgesic and antipyretic tablet','pain',0,3500,'UGX',500,0,'🩹',1),
        ('med-ug-003','ph-ug-001','UG','Vitamin C','1000mg','Immune support & antioxidant','vitamin',0,18000,'UGX',200,0,'🟡',1),
        ('med-ug-004','ph-ug-004','UG','Cetirizine','10mg','Non-sedating antihistamine','allergy',0,8500,'UGX',4,0,'🌿',1),
        ('med-ug-005','ph-ug-002','UG','Omeprazole','20mg','Proton pump inhibitor for acid reflux','digestive',1,15000,'UGX',80,0,'🫁',1),
        ('med-ug-006','ph-ug-001','UG','Metronidazole','400mg','Antibiotic and antiprotozoal agent','antibiotic',1,9500,'UGX',150,0,'💉',1),
        ('med-ug-007','ph-ug-003','UG','Hydrocortisone','1% Cream','Mild corticosteroid for skin inflammation','skin',0,14000,'UGX',3,0,'🧴',1),
        ('med-ug-008','ph-ug-004','UG','Zinc Supplement','20mg','Essential mineral for immune health','vitamin',0,11000,'UGX',90,0,'⚗️',1),
        ('med-ug-009','ph-ug-002','UG','Ibuprofen','400mg','NSAID for pain, fever & inflammation','pain',0,6000,'UGX',0,0,'💊',1),
        ('med-ug-010','ph-ug-001','UG','Antacid Suspension','200ml','Fast-acting relief for heartburn','digestive',0,7500,'UGX',60,0,'🧪',1),
        # UAE
        ('med-ae-001','ph-ae-001','AE','Augmentin','625mg','Amoxicillin-clavulanate combination antibiotic','antibiotic',1,42,'AED',100,0,'💊',1),
        ('med-ae-002','ph-ae-002','AE','Brufen','400mg','Ibuprofen for pain and inflammation','pain',0,18,'AED',350,0,'🩹',1),
        ('med-ae-003','ph-ae-001','AE','Vitamin D3','5000 IU','Bone density & immune health support','vitamin',0,55,'AED',180,0,'☀️',1),
        ('med-ae-004','ph-ae-003','AE','Telfast','180mg','Non-drowsy fexofenadine antihistamine','allergy',0,35,'AED',3,0,'🌿',1),
        ('med-ae-005','ph-ae-002','AE','Nexium','40mg','Esomeprazole proton pump inhibitor','digestive',1,68,'AED',60,0,'🫁',1),
        ('med-ae-006','ph-ae-001','AE','Flagyl','500mg','Metronidazole broad-spectrum antibiotic','antibiotic',1,28,'AED',110,0,'💉',1),
        ('med-ae-007','ph-ae-004','AE','Betnovate','0.1% Cream','Betamethasone corticosteroid cream','skin',1,38,'AED',2,0,'🧴',1),
        ('med-ae-008','ph-ae-003','AE','Omega-3','1000mg','Fish oil for cardiovascular health','vitamin',0,85,'AED',95,0,'🐟',1),
        ('med-ae-009','ph-ae-002','AE','Voltaren','1% Emulgel','Topical diclofenac for joint & muscle pain','pain',0,48,'AED',70,0,'🧴',1),
        ('med-ae-010','ph-ae-001','AE','Gaviscon','200ml','Alginate antacid for acid reflux','digestive',0,32,'AED',0,0,'🧪',1),
    ]
    c.executemany("""INSERT OR IGNORE INTO medicines
        (id,pharmacy_id,country_code,name,strength,description,category,requires_rx,price,currency,stock_qty,reserved_qty,icon,is_active)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", medicines)

    conn.commit()
    conn.close()
    print("[DB] Initialised and seeded")

# ─── Auth helpers ─────────────────────────────────────────────────────────────
def hash_password(pw):
    return hashlib.sha256((pw + 'medilinka_salt').encode()).hexdigest()

def make_token(user_id, country_code):
    payload = {
        'sub': user_id,
        'country': country_code,
        'exp': int(time.time()) + 86400,  # 24h
        'iat': int(time.time()),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    header_b64  = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip('=')
    sig_input   = f"{header_b64}.{payload_b64}"
    sig         = hmac.new(JWT_SECRET.encode(), sig_input.encode(), hashlib.sha256).hexdigest()
    return f"{header_b64}.{payload_b64}.{sig}"

def verify_token(token):
    try:
        parts = token.split('.')
        if len(parts) != 3: return None
        header_b64, payload_b64, sig = parts
        sig_input    = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(JWT_SECRET.encode(), sig_input.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig): return None
        padding  = '=' * (4 - len(payload_b64) % 4)
        payload  = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        if payload.get('exp', 0) < time.time(): return None
        return payload
    except:
        return None

# ─── Response helpers ─────────────────────────────────────────────────────────
def ok(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', len(body))
    cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)

def err(handler, msg, status=400, code='ERROR'):
    body = json.dumps({'error': msg, 'code': code}).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', len(body))
    cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)

def cors_headers(handler):
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
    handler.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Country-Code, X-Idempotency-Key')

def read_json(handler):
    length = int(handler.headers.get('Content-Length', 0))
    if length == 0: return {}
    return json.loads(handler.rfile.read(length))

def get_user(handler):
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '): return None
    payload = verify_token(auth[7:])
    if not payload: return None
    return payload

def stock_status(m):
    avail = m['stock_qty'] - m['reserved_qty']
    if avail <= 0: return 'out_of_stock'
    if avail <= 5: return 'low_stock'
    return 'in_stock'

def row_to_dict(row):
    if row is None: return None
    return dict(row)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def expiry_iso(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()

def audit(action, actor_id=None, actor_type='user', entity_type=None, entity_id=None,
          country_code=None, details=None, ip=None):
    try:
        conn = get_db()
        conn.execute("""INSERT INTO audit_logs
            (id,action,actor_id,actor_type,entity_type,entity_id,country_code,details,ip_address)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), action, actor_id, actor_type, entity_type, entity_id,
             country_code, json.dumps(details or {}), ip))
        conn.commit()
        conn.close()
    except: pass

# ─── Payment simulation ────────────────────────────────────────────────────────
# In real system: webhooks come from provider. Here we simulate confirmation
# after a realistic delay (5s for mobile money, 3s for card) so the frontend
# polling actually finds a real status change.
def simulate_payment_confirm(payment_id, order_id, delay_seconds):
    def _confirm():
        time.sleep(delay_seconds)
        conn = get_db()
        try:
            # Check payment still processing
            pay = row_to_dict(conn.execute(
                "SELECT * FROM payments WHERE id=? AND status='processing'", (payment_id,)).fetchone())
            if not pay:
                conn.close(); return

            now = now_iso()

            # Mark payment success
            conn.execute("""UPDATE payments SET status='success',
                provider_reference=?, completed_at=? WHERE id=?""",
                (f"SIM-{payment_id[:8].upper()}", now, payment_id))

            # Mark order paid
            conn.execute("""UPDATE orders SET status='paid', paid_at=?
                WHERE id=? AND status='pending'""", (now, order_id))

            # Deduct stock (release reservation, reduce stock_qty)
            reservations = conn.execute(
                "SELECT * FROM stock_reservations WHERE order_id=? AND is_active=1",
                (order_id,)).fetchall()
            for r in reservations:
                conn.execute("""UPDATE medicines SET
                    stock_qty = MAX(0, stock_qty - ?),
                    reserved_qty = MAX(0, reserved_qty - ?)
                    WHERE id=?""", (r['quantity'], r['quantity'], r['medicine_id']))
                conn.execute("""UPDATE stock_reservations SET
                    is_active=0, released_at=?, released_reason='payment_success'
                    WHERE id=?""", (now, r['id']))

            # Ledger entry
            gross     = pay['amount']
            fee       = round(gross * PLATFORM_FEE, 2)
            pharm_amt = gross - fee
            conn.execute("""INSERT INTO ledger_transactions
                (id,order_id,payment_id,type,status,gross_amount,platform_fee,pharmacy_amount,currency,country_code)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), order_id, payment_id, 'payment', 'pending',
                 gross, fee, pharm_amt, pay['currency'], pay['country_code']))

            conn.commit()
            print(f"[PAYMENT] Confirmed: {payment_id[:8]} | Order: {order_id[:8]} | {pay['currency']} {gross}")
        except Exception as e:
            print(f"[PAYMENT] Confirm error: {e}")
        finally:
            conn.close()

    t = threading.Thread(target=_confirm, daemon=True)
    t.start()

# ─── Background expiry job ────────────────────────────────────────────────────
def run_expiry_job():
    while True:
        time.sleep(60)
        try:
            conn = get_db()
            now = now_iso()
            expired = conn.execute(
                "SELECT id FROM orders WHERE status='pending' AND expires_at<?", (now,)).fetchall()
            for row in expired:
                oid = row['id']
                # Release reservations
                reservations = conn.execute(
                    "SELECT * FROM stock_reservations WHERE order_id=? AND is_active=1", (oid,)).fetchall()
                for r in reservations:
                    conn.execute("UPDATE medicines SET reserved_qty=MAX(0,reserved_qty-?) WHERE id=?",
                                 (r['quantity'], r['medicine_id']))
                    conn.execute("UPDATE stock_reservations SET is_active=0, released_at=?, released_reason='expired' WHERE id=?",
                                 (now, r['id']))
                conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
                conn.execute("UPDATE payments SET status='expired', failure_reason='Order expired' WHERE order_id=? AND status='processing'", (oid,))
                print(f"[JOB] Expired order: {oid[:8]}")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[JOB] Expiry error: {e}")

# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, fmt, *args):
        print(f"[HTTP] {self.address_string()} {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        cors_headers(self)
        self.end_headers()

    def do_GET(self):  self.route()
    def do_POST(self): self.route()
    def do_DELETE(self): self.route()
    def do_PATCH(self): self.route()

    def route(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        qs     = parse_qs(parsed.query)
        method = self.command
        ip     = self.client_address[0]

        try:
            # ── Serve frontend app ───────────────────────────────────
            if path in ('', '/') and method == 'GET':
                return self.serve_file(os.path.join(os.path.dirname(__file__), 'frontend.html'), 'text/html')

            # ── Serve admin dashboard ────────────────────────────────
            if path in ('/admin', '/admin/') and method == 'GET':
                return self.serve_admin()

            # ── Health ──────────────────────────────────────────────
            if path == '/health' and method == 'GET':
                return ok(self, {'status':'ok','time':now_iso(),'version':'1.0.0-live'})

            # ── Auth: Register ──────────────────────────────────────
            if path == '/api/auth/register' and method == 'POST':
                return self.auth_register(ip)

            # ── Auth: Login ─────────────────────────────────────────
            if path == '/api/auth/login' and method == 'POST':
                return self.auth_login(ip)

            # ── Auth: Refresh ───────────────────────────────────────
            if path == '/api/auth/refresh' and method == 'POST':
                return ok(self, {'token': 'refresh-not-implemented', 'expiresAt': expiry_iso(60*24)})

            # ── Auth: Logout ────────────────────────────────────────
            if path == '/api/auth/logout' and method == 'POST':
                return ok(self, {'message':'Logged out successfully'})

            # ── Users: Me ───────────────────────────────────────────
            if path == '/api/users/me' and method == 'GET':
                return self.users_me()

            if path == '/api/users/me' and method == 'PATCH':
                return self.users_me_patch()

            # ── Catalog: Medicines ──────────────────────────────────
            if path == '/api/catalog/medicines' and method == 'GET':
                return self.catalog_medicines(qs)

            # ── Catalog: Pharmacies ─────────────────────────────────
            if path == '/api/catalog/pharmacies' and method == 'GET':
                return self.catalog_pharmacies(qs)

            # ── Catalog: Payment methods ────────────────────────────
            if path == '/api/catalog/payment-methods' and method == 'GET':
                return self.catalog_payment_methods(qs)

            # ── Catalog: Countries ──────────────────────────────────
            if path == '/api/catalog/countries' and method == 'GET':
                return ok(self, {'countries':[
                    {'code':'UG','name':'Uganda','currency':'UGX','flag':'🇺🇬'},
                    {'code':'AE','name':'UAE','currency':'AED','flag':'🇦🇪'},
                ]})

            # ── Orders: Create ──────────────────────────────────────
            if path == '/api/orders' and method == 'POST':
                return self.orders_create(ip)

            # ── Orders: List ────────────────────────────────────────
            if path == '/api/orders' and method == 'GET':
                return self.orders_list()

            # ── Orders: Detail ──────────────────────────────────────
            m = re.match(r'^/api/orders/([0-9a-f\-]+)$', path)
            if m:
                if method == 'GET':    return self.orders_detail(m.group(1))
                if method == 'DELETE': return self.orders_cancel(m.group(1), ip)

            # ── Payments: Initiate ──────────────────────────────────
            if path == '/api/payments/initiate' and method == 'POST':
                return self.payments_initiate(ip)

            # ── Payments: Status ────────────────────────────────────
            m = re.match(r'^/api/payments/([0-9a-f\-]+)/status$', path)
            if m:
                return self.payments_status(m.group(1))

            # ── Payments: Refund ────────────────────────────────────
            m = re.match(r'^/api/payments/([0-9a-f\-]+)/refund$', path)
            if m and method == 'POST':
                return self.payments_refund(m.group(1), ip)

            err(self, f'Route not found: {method} {path}', 404, 'NOT_FOUND')
        except Exception as e:
            import traceback
            traceback.print_exc()
            err(self, str(e), 500, 'INTERNAL_ERROR')

    # ── Auth ───────────────────────────────────────────────────────────────────
    def auth_register(self, ip):
        body = read_json(self)
        email    = (body.get('email') or '').strip().lower()
        password = body.get('password') or ''
        name     = (body.get('full_name') or '').strip()
        country  = (body.get('country_code') or '').upper()

        if not email or '@' not in email: return err(self,'Valid email required',422)
        if len(password) < 8: return err(self,'Password must be at least 8 characters',422)
        if not name:          return err(self,'Full name required',422)
        if country not in ('UG','AE'): return err(self,'Invalid country code',422)

        uid = str(uuid.uuid4())
        conn = get_db()
        try:
            conn.execute("""INSERT INTO users (id,email,password_hash,full_name,country_code)
                VALUES (?,?,?,?,?)""",
                (uid, email, hash_password(password), name, country))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return err(self,'Email already registered',409,'EMAIL_EXISTS')
        conn.close()
        audit('user_registered', uid, country_code=country, ip=ip)
        return ok(self, {'message':'Account created successfully','userId':uid}, 201)

    def auth_login(self, ip):
        body  = read_json(self)
        email = (body.get('email') or '').strip().lower()
        pw    = body.get('password') or ''
        conn  = get_db()
        user  = row_to_dict(conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)).fetchone())
        conn.close()
        if not user or user['password_hash'] != hash_password(pw):
            audit('user_login_failed', details={'email': email}, ip=ip)
            return err(self,'Invalid email or password',401,'AUTH_INVALID')
        token = make_token(user['id'], user['country_code'])
        audit('user_login', user['id'], country_code=user['country_code'], ip=ip)
        return ok(self, {
            'token': token,
            'refreshToken': token,
            'expiresAt': expiry_iso(60*24),
            'user': {
                'id': user['id'],
                'email': user['email'],
                'full_name': user['full_name'],
                'phone': user['phone'],
                'country_code': user['country_code'],
            }
        })

    # ── Users ──────────────────────────────────────────────────────────────────
    def users_me(self):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401,'AUTH_REQUIRED')
        conn = get_db()
        user = row_to_dict(conn.execute(
            "SELECT id,email,full_name,phone,country_code,created_at FROM users WHERE id=?",
            (payload['sub'],)).fetchone())
        conn.close()
        if not user: return err(self,'User not found',404)
        return ok(self, {'user': user})

    def users_me_patch(self):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401)
        body = read_json(self)
        updates = {}
        if 'full_name' in body: updates['full_name'] = body['full_name']
        if 'phone' in body:     updates['phone']     = body['phone']
        if not updates: return err(self,'Nothing to update',400)
        conn = get_db()
        for col, val in updates.items():
            conn.execute(f"UPDATE users SET {col}=? WHERE id=?", (val, payload['sub']))
        conn.commit()
        user = row_to_dict(conn.execute(
            "SELECT id,email,full_name,phone,country_code FROM users WHERE id=?",
            (payload['sub'],)).fetchone())
        conn.close()
        return ok(self, {'user': user})

    # ── Catalog ────────────────────────────────────────────────────────────────
    def catalog_medicines(self, qs):
        country  = (qs.get('country_code',[''])[0] or '').upper()
        category = qs.get('category',[''])[0]
        search   = qs.get('search',[''])[0].lower()
        if country not in ('UG','AE'): return err(self,'Invalid country_code',422)

        conn = get_db()
        rows = conn.execute("""
            SELECT m.*, p.id as pharm_id, p.name as pharm_name, p.city as pharm_city,
                   p.is_verified, p.is_open
            FROM medicines m
            JOIN pharmacies p ON p.id = m.pharmacy_id
            WHERE m.country_code=? AND m.is_active=1
            ORDER BY m.name""", (country,)).fetchall()
        conn.close()

        medicines = []
        for r in rows:
            m = dict(r)
            avail = m['stock_qty'] - m['reserved_qty']
            status = 'out_of_stock' if avail <= 0 else ('low_stock' if avail <= 5 else 'in_stock')
            if category and category != 'all' and m['category'] != category: continue
            if search and search not in m['name'].lower() and search not in (m['description'] or '').lower() and search not in m['strength'].lower(): continue
            medicines.append({
                'id': m['id'], 'name': m['name'], 'strength': m['strength'],
                'description': m['description'], 'category': m['category'],
                'requires_rx': bool(m['requires_rx']), 'price': m['price'],
                'currency': m['currency'], 'available_qty': max(0, avail),
                'stock_status': status, 'icon': m['icon'],
                'pharmacy': {
                    'id': m['pharm_id'], 'name': m['pharm_name'],
                    'city': m['pharm_city'], 'is_verified': bool(m['is_verified']),
                    'is_open': bool(m['is_open']),
                }
            })
        return ok(self, {'medicines': medicines, 'country_code': country})

    def catalog_pharmacies(self, qs):
        country = (qs.get('country_code',[''])[0] or '').upper()
        if country not in ('UG','AE'): return err(self,'Invalid country_code',422)
        conn = get_db()
        rows = conn.execute("""SELECT id,name,city,address,is_verified,is_open,contact_phone,contact_email
            FROM pharmacies WHERE country_code=? ORDER BY name""", (country,)).fetchall()
        conn.close()
        return ok(self, {'pharmacies': [dict(r) for r in rows], 'country_code': country})

    def catalog_payment_methods(self, qs):
        country = (qs.get('country_code',[''])[0] or '').upper()
        if country not in ('UG','AE'): return err(self,'Invalid country_code',422)
        methods = {
            'UG': [
                {'id':'mtn',    'name':'MTN Mobile Money','desc':'USSD push to your MTN line','logo':'MTN','type':'mobile'},
                {'id':'airtel', 'name':'Airtel Money',    'desc':'USSD push to your Airtel line','logo':'Airtel','type':'mobile'},
            ],
            'AE': [
                {'id':'zina','name':'Zina Card Payment','desc':'Secure card checkout via Zina gateway','logo':'Zina','type':'card'},
            ],
        }
        delivery = {
            'UG': {
                'delivery': {'fee':5000,'eta':'45 – 90 min','currency':'UGX'},
                'pickup':   {'fee':0,   'eta':'Ready in 20 min','currency':'UGX'},
            },
            'AE': {
                'delivery': {'fee':15,'eta':'60 – 120 min','currency':'AED'},
                'pickup':   {'fee':0, 'eta':'Ready in 30 min','currency':'AED'},
            },
        }
        return ok(self, {'country_code':country,'payment_methods':methods[country],'delivery':delivery[country]})

    # ── Orders ─────────────────────────────────────────────────────────────────
    def orders_create(self, ip):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401,'AUTH_REQUIRED')
        body = read_json(self)

        country      = (body.get('country_code') or '').upper()
        pharmacy_id  = body.get('pharmacy_id') or ''
        del_method   = body.get('delivery_method') or ''
        del_name     = (body.get('delivery_name') or '').strip()
        del_phone    = (body.get('delivery_phone') or '').strip()
        del_city     = (body.get('delivery_city') or '').strip()
        del_address  = (body.get('delivery_address') or '').strip() or None
        items        = body.get('items') or []
        idem_key     = body.get('idempotency_key') or ''

        if country not in ('UG','AE'): return err(self,'Invalid country_code',422)
        if del_method not in ('delivery','pickup'): return err(self,'Invalid delivery_method',422)
        if not del_name:  return err(self,'delivery_name required',422)
        if not del_phone: return err(self,'delivery_phone required',422)
        if not del_city:  return err(self,'delivery_city required',422)
        if del_method=='delivery' and not del_address: return err(self,'delivery_address required for delivery',422)
        if not items: return err(self,'items required',422)
        if not idem_key: return err(self,'idempotency_key required',422)

        conn = get_db()

        # Idempotency
        existing = row_to_dict(conn.execute(
            "SELECT id,status FROM orders WHERE idempotency_key=?", (idem_key,)).fetchone())
        if existing:
            conn.close()
            return ok(self, {'order_id':existing['id'],'status':existing['status'],'idempotent':True})

        # Validate pharmacy
        pharm = row_to_dict(conn.execute(
            "SELECT * FROM pharmacies WHERE id=? AND country_code=? AND is_verified=1",
            (pharmacy_id, country)).fetchone())
        if not pharm:
            conn.close()
            return err(self,'Pharmacy not found or not in your country',404,'PHARMACY_NOT_FOUND')

        # Validate items and calculate total
        med_ids = [i.get('medicine_id') for i in items if i.get('medicine_id')]
        if not med_ids:
            conn.close()
            return err(self,'No valid medicine IDs',422)

        placeholders = ','.join('?' * len(med_ids))
        medicines = {m['id']: dict(m) for m in conn.execute(
            f"SELECT * FROM medicines WHERE id IN ({placeholders}) AND country_code=? AND is_active=1",
            med_ids + [country]).fetchall()}

        order_items = []
        subtotal = 0
        for item in items:
            mid = item.get('medicine_id')
            qty = int(item.get('quantity', 0))
            if not mid or qty <= 0: continue
            med = medicines.get(mid)
            if not med:
                conn.close()
                return err(self, f'Medicine {mid} not found', 404)
            if med['pharmacy_id'] != pharmacy_id:
                conn.close()
                return err(self, f'{med["name"]} is not from the selected pharmacy', 400, 'PHARMACY_MISMATCH')
            avail = med['stock_qty'] - med['reserved_qty']
            if avail < qty:
                conn.close()
                return err(self, f'Insufficient stock for {med["name"]}. Available: {avail}', 409, 'INSUFFICIENT_STOCK')
            line_total = round(med['price'] * qty, 2)
            subtotal  += line_total
            order_items.append((str(uuid.uuid4()), None, mid, med['name'], med['strength'], qty, med['price'], line_total))

        fee_map = {'UG':{'delivery':5000,'pickup':0},'AE':{'delivery':15,'pickup':0}}
        delivery_fee = fee_map[country][del_method]
        total    = round(subtotal + delivery_fee, 2)
        currency = 'UGX' if country == 'UG' else 'AED'
        order_id = str(uuid.uuid4())
        expires  = expiry_iso(30)

        try:
            conn.execute("""INSERT INTO orders
                (id,user_id,pharmacy_id,country_code,status,delivery_method,
                delivery_name,delivery_phone,delivery_address,delivery_city,
                subtotal,delivery_fee,total,currency,idempotency_key,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, payload['sub'], pharmacy_id, country, 'pending', del_method,
                 del_name, del_phone, del_address, del_city,
                 subtotal, delivery_fee, total, currency, idem_key, expires))

            for (iid, _, mid, name, strength, qty, unit_price, line_total) in order_items:
                conn.execute("""INSERT INTO order_items (id,order_id,medicine_id,name,strength,quantity,unit_price,line_total)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (iid, order_id, mid, name, strength, qty, unit_price, line_total))
                # Reserve stock
                conn.execute("UPDATE medicines SET reserved_qty=reserved_qty+? WHERE id=?", (qty, mid))
                conn.execute("""INSERT INTO stock_reservations (id,order_id,medicine_id,quantity)
                    VALUES (?,?,?,?)""", (str(uuid.uuid4()), order_id, mid, qty))

            conn.commit()
        except Exception as e:
            conn.close()
            raise e

        conn.close()
        audit('order_created', payload['sub'], entity_type='order', entity_id=order_id,
              country_code=country, details={'total':total,'currency':currency}, ip=ip)
        print(f"[ORDER] Created: {order_id[:8]} | {currency} {total} | {del_method}")
        return ok(self, {
            'order_id': order_id, 'status':'pending',
            'total': total, 'currency': currency, 'expires_at': expires
        }, 201)

    def orders_list(self):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401,'AUTH_REQUIRED')
        conn = get_db()
        orders = conn.execute("""
            SELECT o.*, p.name as pharm_name, p.city as pharm_city,
                   pay.id as pay_id, pay.provider, pay.status as pay_status
            FROM orders o
            LEFT JOIN pharmacies p ON p.id=o.pharmacy_id
            LEFT JOIN payments pay ON pay.order_id=o.id
            WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 50""",
            (payload['sub'],)).fetchall()

        result = []
        for o in orders:
            od = dict(o)
            items = conn.execute(
                "SELECT * FROM order_items WHERE order_id=?", (od['id'],)).fetchall()
            result.append({
                'id': od['id'], 'status': od['status'],
                'total': od['total'], 'currency': od['currency'],
                'delivery_method': od['delivery_method'],
                'delivery_city': od['delivery_city'],
                'created_at': od['created_at'], 'paid_at': od['paid_at'],
                'expires_at': od['expires_at'],
                'pharmacy': {'id': od['pharmacy_id'], 'name': od['pharm_name'], 'city': od['pharm_city']},
                'items': [dict(i) for i in items],
                'payment': {'provider': od['provider'], 'status': od['pay_status']} if od['pay_id'] else None,
            })
        conn.close()
        return ok(self, {'orders': result})

    def orders_detail(self, order_id):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401)
        conn = get_db()
        o = row_to_dict(conn.execute(
            "SELECT o.*, p.name as pharm_name FROM orders o JOIN pharmacies p ON p.id=o.pharmacy_id WHERE o.id=? AND o.user_id=?",
            (order_id, payload['sub'])).fetchone())
        if not o: conn.close(); return err(self,'Order not found',404)
        items = [dict(r) for r in conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()]
        pays  = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchall()]
        conn.close()
        o['items'] = items
        o['payments'] = pays
        return ok(self, {'order': o})

    def orders_cancel(self, order_id, ip):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401)
        conn = get_db()
        o = row_to_dict(conn.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, payload['sub'])).fetchone())
        if not o: conn.close(); return err(self,'Order not found',404)
        if o['status'] != 'pending':
            conn.close()
            return err(self, f'Cannot cancel order in status: {o["status"]}', 400, 'CANCEL_FORBIDDEN')

        # Release stock
        reservations = conn.execute(
            "SELECT * FROM stock_reservations WHERE order_id=? AND is_active=1", (order_id,)).fetchall()
        for r in reservations:
            conn.execute("UPDATE medicines SET reserved_qty=MAX(0,reserved_qty-?) WHERE id=?",
                         (r['quantity'], r['medicine_id']))
            conn.execute("UPDATE stock_reservations SET is_active=0, released_at=?, released_reason='user_cancelled' WHERE id=?",
                         (now_iso(), r['id']))

        conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
        conn.commit(); conn.close()
        audit('order_status_changed', payload['sub'], entity_type='order', entity_id=order_id,
              details={'from':'pending','to':'cancelled'}, ip=ip)
        return ok(self, {'message':'Order cancelled successfully'})

    # ── Payments ───────────────────────────────────────────────────────────────
    def payments_initiate(self, ip):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401)
        body = read_json(self)

        order_id = body.get('order_id') or ''
        provider = (body.get('provider') or '').lower()
        idem_key = body.get('idempotency_key') or ''

        if not order_id: return err(self,'order_id required',422)
        if not provider: return err(self,'provider required',422)
        if not idem_key: return err(self,'idempotency_key required',422)

        conn = get_db()

        # Idempotency
        existing = row_to_dict(conn.execute(
            "SELECT id,status FROM payments WHERE idempotency_key=?", (idem_key,)).fetchone())
        if existing:
            conn.close()
            return ok(self, {'payment_id':existing['id'],'status':existing['status'],'idempotent':True})

        # Validate order
        o = row_to_dict(conn.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?",
            (order_id, payload['sub'])).fetchone())
        if not o:
            conn.close(); return err(self,'Order not found',404)
        if o['status'] != 'pending':
            conn.close(); return err(self,f'Order not in pending status (current: {o["status"]})',400,'ORDER_NOT_PENDING')

        # Country-provider enforcement
        allowed = {'UG':['mtn','airtel'],'AE':['zina']}
        if provider not in allowed.get(o['country_code'],[]):
            conn.close()
            return err(self,f"Provider '{provider}' not allowed for {o['country_code']}",400,'PROVIDER_NOT_ALLOWED')

        payment_id = str(uuid.uuid4())
        expires    = expiry_iso(10)  # 10 min payment window

        conn.execute("""INSERT INTO payments
            (id,order_id,user_id,country_code,provider,status,amount,currency,idempotency_key,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (payment_id, order_id, payload['sub'], o['country_code'],
             provider, 'processing', o['total'], o['currency'], idem_key, expires))
        conn.commit(); conn.close()

        audit('payment_initiated', payload['sub'], entity_type='payment', entity_id=payment_id,
              country_code=o['country_code'],
              details={'provider':provider,'amount':o['total'],'currency':o['currency']}, ip=ip)

        # Simulate payment confirmation (real system: wait for webhook)
        delay = 5 if provider in ('mtn','airtel') else 3
        simulate_payment_confirm(payment_id, order_id, delay)

        print(f"[PAYMENT] Initiated: {payment_id[:8]} | {provider} | {o['currency']} {o['total']} | confirms in {delay}s")

        return ok(self, {
            'payment_id': payment_id,
            'status': 'processing',
            'provider': provider,
            'awaiting_webhook': provider in ('mtn','airtel'),
        }, 201)

    def payments_status(self, payment_id):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401)
        conn = get_db()
        pay = row_to_dict(conn.execute(
            "SELECT * FROM payments WHERE id=? AND user_id=?",
            (payment_id, payload['sub'])).fetchone())
        conn.close()
        if not pay: return err(self,'Payment not found',404)
        return ok(self, {
            'payment_id': pay['id'], 'status': pay['status'],
            'provider': pay['provider'], 'amount': pay['amount'],
            'currency': pay['currency'], 'order_id': pay['order_id'],
            'provider_reference': pay['provider_reference'],
            'completed_at': pay['completed_at'],
            'failure_reason': pay['failure_reason'],
        })

    def payments_refund(self, payment_id, ip):
        payload = get_user(self)
        if not payload: return err(self,'Unauthorized',401)
        body = read_json(self)
        reason = (body.get('reason') or 'User requested refund').strip()

        conn = get_db()
        pay = row_to_dict(conn.execute(
            "SELECT * FROM payments WHERE id=? AND user_id=?",
            (payment_id, payload['sub'])).fetchone())
        if not pay: conn.close(); return err(self,'Payment not found',404)
        if pay['status'] != 'success':
            conn.close(); return err(self,'Only successful payments can be refunded',400,'REFUND_NOT_ELIGIBLE')

        now = now_iso()
        conn.execute("UPDATE payments SET status='refunded' WHERE id=?", (payment_id,))
        conn.execute("""UPDATE orders SET status='refunded', paid_at=NULL
            WHERE id=?""", (pay['order_id'],))

        # Ledger entry for refund
        gross = pay['amount']
        fee   = round(gross * PLATFORM_FEE, 2)
        conn.execute("""INSERT INTO ledger_transactions
            (id,order_id,payment_id,type,status,gross_amount,platform_fee,pharmacy_amount,currency,country_code,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), pay['order_id'], payment_id, 'refund', 'pending',
             -gross, -fee, -(gross-fee), pay['currency'], pay['country_code'], reason))

        conn.commit(); conn.close()
        audit('payment_refunded', payload['sub'], entity_type='payment', entity_id=payment_id,
              country_code=pay['country_code'], details={'reason':reason,'amount':pay['amount']}, ip=ip)
        print(f"[REFUND] Payment: {payment_id[:8]} | {pay['currency']} {pay['amount']}")
        return ok(self, {'message':'Refund initiated successfully', 'refund_reference':f"REF-{payment_id[:8].upper()}"})


    # ── File serving ──────────────────────────────────────────────────────────
    def serve_file(self, filepath, mime):
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', len(data))
            cors_headers(self)
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            err(self, 'File not found', 404)

    def serve_admin(self):
        conn = get_db()
        stats = {
            'users':     conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            'orders':    conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            'payments':  conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
            'medicines': conn.execute("SELECT COUNT(*) FROM medicines").fetchone()[0],
        }
        orders  = [dict(r) for r in conn.execute(
            "SELECT o.id, o.status, o.total, o.currency, o.created_at, o.paid_at, p.name as pharm "
            "FROM orders o LEFT JOIN pharmacies p ON p.id=o.pharmacy_id "
            "ORDER BY o.created_at DESC LIMIT 20").fetchall()]
        pays = [dict(r) for r in conn.execute(
            "SELECT id, provider, status, amount, currency, initiated_at, completed_at "
            "FROM payments ORDER BY initiated_at DESC LIMIT 20").fetchall()]
        meds = [dict(r) for r in conn.execute(
            "SELECT id, name, strength, currency, price, stock_qty, reserved_qty, country_code "
            "FROM medicines ORDER BY country_code, name").fetchall()]
        users = [dict(r) for r in conn.execute(
            "SELECT id, email, full_name, country_code, created_at FROM users ORDER BY created_at DESC LIMIT 20").fetchall()]
        ledger = [dict(r) for r in conn.execute(
            "SELECT type, status, gross_amount, platform_fee, pharmacy_amount, currency, created_at "
            "FROM ledger_transactions ORDER BY created_at DESC LIMIT 20").fetchall()]
        conn.close()

        def rows(data, cols):
            if not data:
                return '<tr><td colspan="' + str(len(cols)) + '" style="text-align:center;color:#888;padding:20px">No data yet</td></tr>'
            html = ''
            for row in data:
                html += '<tr>' + ''.join(f'<td>{row.get(c,"")}</td>' for c in cols) + '</tr>'
            return html

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MediLinka — Live Admin Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}}
.top{{background:linear-gradient(135deg,#1e40af,#0d9488);padding:24px 32px;display:flex;align-items:center;gap:16px}}
.top h1{{font-size:22px;font-weight:700;color:white}}
.top .sub{{font-size:13px;color:rgba(255,255,255,.7);margin-top:2px}}
.badge{{background:rgba(255,255,255,.2);color:white;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}}
.live{{display:inline-flex;align-items:center;gap:5px;color:#4ade80;font-size:12px;font-weight:600}}
.live::before{{content:'';width:8px;height:8px;border-radius:50%;background:#4ade80;animation:pulse 1.5s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:24px 32px}}
.stat{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px}}
.stat .n{{font-size:32px;font-weight:700;color:#60a5fa}}
.stat .l{{font-size:13px;color:#94a3b8;margin-top:4px}}
.content{{padding:0 32px 32px}}
.section{{margin-bottom:28px}}
.section h2{{font-size:14px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #1e293b}}
.tbl{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden}}
.tbl th{{background:#0f172a;padding:10px 14px;text-align:left;font-size:12px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.05em}}
.tbl td{{padding:10px 14px;font-size:13px;border-bottom:1px solid #0f172a;color:#cbd5e1}}
.tbl tr:last-child td{{border-bottom:none}}
.tbl tr:hover td{{background:#263348}}
.pill{{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}}
.pill-pending{{background:#1e3a5f;color:#60a5fa}}
.pill-paid{{background:#14532d;color:#4ade80}}
.pill-processing{{background:#312e81;color:#a78bfa}}
.pill-success{{background:#14532d;color:#4ade80}}
.pill-failed{{background:#450a0a;color:#f87171}}
.pill-refunded{{background:#431407;color:#fb923c}}
.pill-cancelled{{background:#1c1c1c;color:#9ca3af}}
.pill-ug{{background:#1e3a5f;color:#93c5fd}}
.pill-ae{{background:#1a2e1a;color:#86efac}}
.refresh{{float:right;background:#1e40af;color:white;border:none;padding:7px 16px;border-radius:7px;font-size:12px;cursor:pointer;font-weight:600}}
.refresh:hover{{background:#1d4ed8}}
.url-bar{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 16px;margin:0 32px 20px;display:flex;align-items:center;gap:12px;font-size:13px}}
.url-bar a{{color:#60a5fa;text-decoration:none;font-weight:500}}
.url-bar a:hover{{text-decoration:underline}}
.sep{{color:#475569}}
</style>
</head>
<body>
<div class="top">
  <div>
    <h1>🏥 MediLinka Admin</h1>
    <div class="sub">Live Backend Dashboard</div>
  </div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:12px">
    <span class="live">LIVE</span>
    <span class="badge">v1.0.0</span>
    <button class="refresh" onclick="location.reload()">↻ Refresh</button>
  </div>
</div>

<div class="url-bar">
  <strong>API Base:</strong>
  <a href="/health">/health</a>
  <span class="sep">·</span>
  <a href="/api/catalog/medicines?country_code=UG">/api/catalog/medicines?country_code=UG</a>
  <span class="sep">·</span>
  <a href="/api/catalog/medicines?country_code=AE">/api/catalog/medicines?country_code=AE</a>
  <span class="sep">·</span>
  <a href="/api/catalog/pharmacies?country_code=UG">/api/catalog/pharmacies?country_code=UG</a>
  <span class="sep">·</span>
  <a href="/api/catalog/payment-methods?country_code=UG">/api/catalog/payment-methods?country_code=UG</a>
  <span class="sep">·</span>
  <a href="/" target="_blank">Open App →</a>
</div>

<div class="stats">
  <div class="stat"><div class="n">{stats['users']}</div><div class="l">Registered Users</div></div>
  <div class="stat"><div class="n">{stats['orders']}</div><div class="l">Total Orders</div></div>
  <div class="stat"><div class="n">{stats['payments']}</div><div class="l">Payments</div></div>
  <div class="stat"><div class="n">{stats['medicines']}</div><div class="l">Medicines (total)</div></div>
</div>

<div class="content">

<div class="section">
  <h2>Recent Orders</h2>
  <table class="tbl">
    <thead><tr><th>Order ID</th><th>Status</th><th>Pharmacy</th><th>Total</th><th>Created</th><th>Paid At</th></tr></thead>
    <tbody>{rows(orders, ['id','status','pharm','total','created_at','paid_at'])}</tbody>
  </table>
</div>

<div class="section">
  <h2>Recent Payments</h2>
  <table class="tbl">
    <thead><tr><th>Payment ID</th><th>Provider</th><th>Status</th><th>Amount</th><th>Initiated</th><th>Completed</th></tr></thead>
    <tbody>{rows(pays, ['id','provider','status','amount','initiated_at','completed_at'])}</tbody>
  </table>
</div>

<div class="section">
  <h2>Ledger (Recent Transactions)</h2>
  <table class="tbl">
    <thead><tr><th>Type</th><th>Status</th><th>Gross</th><th>Platform Fee</th><th>Pharmacy</th><th>Currency</th><th>Created</th></tr></thead>
    <tbody>{rows(ledger, ['type','status','gross_amount','platform_fee','pharmacy_amount','currency','created_at'])}</tbody>
  </table>
</div>

<div class="section">
  <h2>Users</h2>
  <table class="tbl">
    <thead><tr><th>ID</th><th>Email</th><th>Name</th><th>Country</th><th>Registered</th></tr></thead>
    <tbody>{rows(users, ['id','email','full_name','country_code','created_at'])}</tbody>
  </table>
</div>

<div class="section">
  <h2>Medicine Stock ({len(meds)} items)</h2>
  <table class="tbl">
    <thead><tr><th>ID</th><th>Name</th><th>Strength</th><th>Country</th><th>Price</th><th>Stock</th><th>Reserved</th><th>Available</th></tr></thead>
    <tbody>
    {''.join(f"""<tr>
      <td style='font-size:11px;color:#64748b'>{m['id']}</td>
      <td><strong>{m['name']}</strong></td>
      <td style='color:#94a3b8'>{m['strength']}</td>
      <td><span class='pill pill-{m["country_code"].lower()}'>{m['country_code']}</span></td>
      <td>{m['currency']} {m['price']}</td>
      <td>{m['stock_qty']}</td>
      <td style='color:{"#f87171" if m["reserved_qty"]>0 else "#64748b"}'>{m['reserved_qty']}</td>
      <td style='color:{"#f87171" if m["stock_qty"]-m["reserved_qty"]<=0 else "#4ade80" if m["stock_qty"]-m["reserved_qty"]>5 else "#fbbf24"}'>{max(0,m["stock_qty"]-m["reserved_qty"])}</td>
    </tr>""" for m in meds)}
    </tbody>
  </table>
</div>

</div>
<script>
// Add status pill classes to status cells
document.querySelectorAll('td').forEach(td => {{
  const v = td.textContent.trim().toLowerCase();
  if (['pending','paid','processing','success','failed','refunded','cancelled'].includes(v)) {{
    td.innerHTML = `<span class="pill pill-${{v}}">${{td.textContent}}</span>`;
  }}
}});
// Auto-refresh every 10 seconds
setTimeout(() => location.reload(), 10000);
</script>
</body>
</html>"""
        data = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
        cors_headers(self)
        self.end_headers()
        self.wfile.write(data)

# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    t = threading.Thread(target=run_expiry_job, daemon=True)
    t.start()
    print("[SERVER] MediLinka Live Backend")
    print("[SERVER] http://localhost:3001")
    print("[SERVER] Health: http://localhost:3001/health")
    print("[SERVER] Payment simulation: confirms 3-5s after initiation")
    print()
import os
PORT = int(os.environ.get("PORT", 3001))
server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
