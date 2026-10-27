#!/usr/bin/env python3
"""RafGodwin Engineering - website + shop backend (Python standard library only).

Run:   ADMIN_PASSWORD="your-strong-password" python3 server.py
Open:  http://localhost:8000          (site + shop)
       http://localhost:8000/admin.html   (admin)

Env vars (all optional):
  ADMIN_PASSWORD  admin login password. If missing, a random one is generated and printed once.
  PORT            default 8000
  HOST            default 0.0.0.0
  DATA_DIR        where the database and uploaded photos live (default ./data) - back this folder up
  COOKIE_SECURE   set to 1 when the site is served over HTTPS
"""
import base64, hmac, json, mimetypes, os, re, secrets, sqlite3, sys, threading, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR") or os.path.join(ROOT, "data")
UPLOADS = os.path.join(DATA, "uploads")
DB_PATH = os.path.join(DATA, "shop.db")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
SECURE = os.environ.get("COOKIE_SECURE") == "1"
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"          # set to 1 when Netlify (or another proxy) sits in front
SITE_ORIGIN = (os.environ.get("SITE_ORIGIN") or "").rstrip("/")  # e.g. https://rafgodwin.netlify.app
os.makedirs(UPLOADS, exist_ok=True)

STATUSES = ["new", "confirmed", "out_for_delivery", "delivered", "cancelled"]
PAYMENTS = {"cod": "Pay on delivery / pickup", "momo": "Mobile Money (we send you the number)"}
PUBLIC_EXT = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".svg", ".mp4", ".webm", ".txt", ".xml", ".woff2"}
PUBLIC_DIRS = ("assets/",)

# ---------------------------------------------------------------- database
_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'General',
  price INTEGER NOT NULL,            -- pesewas (GH₵1 = 100)
  stock INTEGER,                     -- NULL = not tracked / always available
  description TEXT NOT NULL DEFAULT '',
  images TEXT NOT NULL DEFAULT '[]', -- JSON list of image URLs
  active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL, area TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  payment TEXT NOT NULL, items TEXT NOT NULL, total INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', admin_note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status, created_at);
""")

def rows(sql, args=()):
    with _lock:
        return [dict(r) for r in db.execute(sql, args).fetchall()]

def one(sql, args=()):
    with _lock:
        r = db.execute(sql, args).fetchone()
        return dict(r) if r else None

def prod_out(r, admin=False):
    p = {"id": r["id"], "name": r["name"], "category": r["category"], "price": r["price"],
         "description": r["description"], "images": json.loads(r["images"]),
         "in_stock": r["stock"] is None or r["stock"] > 0}
    if admin:
        p.update(stock=r["stock"], active=bool(r["active"]), created_at=r["created_at"])
    else:
        p["low"] = r["stock"] is not None and 0 < r["stock"] <= 3
    return p

def order_out(r):
    o = dict(r)
    o["items"] = json.loads(o["items"])
    o["payment_label"] = PAYMENTS.get(o["payment"], o["payment"])
    return o

# ---------------------------------------------------------------- admin password + sessions
def _admin_password():
    pw = os.environ.get("ADMIN_PASSWORD")
    if pw:
        return pw
    f = os.path.join(DATA, "admin_password.txt")
    if os.path.exists(f):
        return open(f).read().strip()
    pw = secrets.token_urlsafe(9)
    with open(f, "w") as fh:
        fh.write(pw)
    os.chmod(f, 0o600)
    print("\n  No ADMIN_PASSWORD set. Generated one and saved it to %s\n  Admin password: %s\n" % (f, pw))
    return pw

ADMIN_PW = _admin_password()
SESSIONS = {}          # token -> expiry
FAILS = {}             # ip -> (count, first_ts)
ORDER_HITS = {}        # ip -> [timestamps]
SESSION_TTL = 60 * 60 * 12

def new_session():
    t = secrets.token_urlsafe(32)
    SESSIONS[t] = time.time() + SESSION_TTL
    return t

def valid_session(t):
    e = SESSIONS.get(t or "")
    if e and e > time.time():
        return True
    SESSIONS.pop(t or "", None)
    return False

# ---------------------------------------------------------------- helpers
class ApiError(Exception):
    def __init__(self, msg, code=400):
        self.msg, self.code = msg, code

def clean(s, n):
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", str(s or "")).strip()[:n]

def money_to_pesewas(v):
    try:
        n = round(float(v) * 100)
    except (TypeError, ValueError):
        raise ApiError("Price must be a number")
    if n < 0 or n > 10**9:
        raise ApiError("Price is out of range")
    return n

def img_ext(b):
    if b[:3] == b"\xff\xd8\xff": return ".jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return ".png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return ".webp"
    return None

def parse_images(v):
    out = []
    for u in (v or [])[:6]:
        u = str(u)
        if re.fullmatch(r"/uploads/[A-Za-z0-9_.-]+", u) or re.fullmatch(r"assets/img/[A-Za-z0-9_.-]+", u):
            out.append(u)
    return out

def next_code(now):
    n = one("SELECT COUNT(*) c FROM orders")["c"] + 1
    return "RG-%s-%04d" % (time.strftime("%y%m%d", time.localtime(now)), n)

# ---------------------------------------------------------------- API handlers
def api_products_public(h, q):
    cat = q.get("category", [""])[0]
    sql = "SELECT * FROM products WHERE active=1"
    args = []
    if cat:
        sql += " AND category=?"; args.append(cat)
    sql += " ORDER BY id DESC"
    return {"products": [prod_out(r) for r in rows(sql, args)]}

def api_checkout(h, body):
    ip = h.ip()
    now = time.time()
    hits = [t for t in ORDER_HITS.get(ip, []) if now - t < 600]
    if len(hits) >= 8:
        raise ApiError("Too many orders from this connection. Please call us on 024 711 7982.", 429)
    name, phone = clean(body.get("name"), 80), clean(body.get("phone"), 30)
    address = clean(body.get("address"), 300)
    if len(name) < 2: raise ApiError("Please enter your name")
    if len(re.sub(r"\D", "", phone)) < 9: raise ApiError("Please enter a valid phone number")
    if len(address) < 5: raise ApiError("Please enter a delivery address")
    payment = body.get("payment") if body.get("payment") in PAYMENTS else "cod"
    raw = body.get("items")
    if not isinstance(raw, list) or not raw: raise ApiError("Your cart is empty")
    want = {}
    for it in raw[:40]:
        try:
            pid, qty = int(it.get("id")), int(it.get("qty"))
        except (TypeError, ValueError, AttributeError):
            raise ApiError("Invalid cart")
        if qty < 1 or qty > 99: raise ApiError("Invalid quantity")
        want[pid] = want.get(pid, 0) + qty
    with _lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            lines, total = [], 0
            for pid, qty in want.items():
                p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
                if not p: raise ApiError("An item in your cart is no longer available. Please refresh the shop.", 409)
                if p["stock"] is not None and p["stock"] < qty:
                    raise ApiError("Only %d left of %s" % (p["stock"], p["name"]), 409)
                imgs = json.loads(p["images"])
                lines.append({"id": p["id"], "name": p["name"], "price": p["price"], "qty": qty, "image": imgs[0] if imgs else ""})
                total += p["price"] * qty
                if p["stock"] is not None:
                    db.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
            code = next_code(now)
            db.execute("INSERT INTO orders(code,name,phone,email,address,area,notes,payment,items,total,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (code, name, phone, clean(body.get("email"), 120), address, clean(body.get("area"), 80),
                        clean(body.get("notes"), 500), payment, json.dumps(lines), total, "new", int(now), int(now)))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    hits.append(now); ORDER_HITS[ip] = hits
    return {"ok": True, "code": code, "total": total, "payment": payment}

def api_login(h, body):
    ip = h.ip()
    c, t0 = FAILS.get(ip, (0, time.time()))
    if time.time() - t0 > 900: c, t0 = 0, time.time()
    if c >= 8:
        raise ApiError("Too many attempts. Try again in 15 minutes.", 429)
    if hmac.compare_digest(str(body.get("password", "")).encode(), ADMIN_PW.encode()):
        FAILS.pop(ip, None)
        return {"ok": True}, new_session()
    FAILS[ip] = (c + 1, t0)
    raise ApiError("Wrong password", 401)

def admin_products(h, q):
    return {"products": [prod_out(r, True) for r in rows("SELECT * FROM products ORDER BY id DESC")]}

def save_product(body, pid=None):
    name = clean(body.get("name"), 120)
    if len(name) < 2: raise ApiError("Product name is required")
    stock = body.get("stock")
    stock = None if stock in (None, "") else int(stock)
    if stock is not None and stock < 0: raise ApiError("Stock cannot be negative")
    vals = (name, clean(body.get("category"), 60) or "General", money_to_pesewas(body.get("price")), stock,
            clean(body.get("description"), 4000), json.dumps(parse_images(body.get("images"))), 1 if body.get("active", True) else 0)
    with _lock:
        if pid:
            if not one("SELECT id FROM products WHERE id=?", (pid,)): raise ApiError("Not found", 404)
            db.execute("UPDATE products SET name=?,category=?,price=?,stock=?,description=?,images=?,active=? WHERE id=?", vals + (pid,))
        else:
            cur = db.execute("INSERT INTO products(name,category,price,stock,description,images,active,created_at) VALUES(?,?,?,?,?,?,?,?)", vals + (int(time.time()),))
            pid = cur.lastrowid
    return {"product": prod_out(one("SELECT * FROM products WHERE id=?", (pid,)), True)}

def admin_upload(h, body):
    m = re.fullmatch(r"data:image/[a-z+]+;base64,(.+)", str(body.get("data", "")), re.S)
    if not m: raise ApiError("Send an image")
    try:
        raw = base64.b64decode(m.group(1), validate=True)
    except Exception:
        raise ApiError("Bad image data")
    if len(raw) > 4 * 1024 * 1024: raise ApiError("Image is over 4 MB")
    ext = img_ext(raw)
    if not ext: raise ApiError("Use a JPG, PNG or WebP image")
    name = secrets.token_hex(8) + ext
    with open(os.path.join(UPLOADS, name), "wb") as f:
        f.write(raw)
    return {"url": "/uploads/" + name}

def admin_orders(h, q):
    st = q.get("status", [""])[0]
    sql, args = "SELECT * FROM orders", []
    if st in STATUSES:
        sql += " WHERE status=?"; args.append(st)
    sql += " ORDER BY id DESC LIMIT 500"
    return {"orders": [order_out(r) for r in rows(sql, args)]}

def admin_order_update(oid, body):
    o = one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o: raise ApiError("Not found", 404)
    st = body.get("status", o["status"])
    if st not in STATUSES: raise ApiError("Bad status")
    note = clean(body.get("admin_note", o["admin_note"]), 1000)
    with _lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            # put stock back once if an order is cancelled, take it again if un-cancelled
            if st == "cancelled" and o["status"] != "cancelled":
                for it in json.loads(o["items"]):
                    db.execute("UPDATE products SET stock=stock+? WHERE id=? AND stock IS NOT NULL", (it["qty"], it["id"]))
            elif o["status"] == "cancelled" and st != "cancelled":
                for it in json.loads(o["items"]):
                    db.execute("UPDATE products SET stock=MAX(0,stock-?) WHERE id=? AND stock IS NOT NULL", (it["qty"], it["id"]))
            db.execute("UPDATE orders SET status=?, admin_note=?, updated_at=? WHERE id=?", (st, note, int(time.time()), oid))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK"); raise
    return {"order": order_out(one("SELECT * FROM orders WHERE id=?", (oid,)))}

def admin_stats(h, q):
    c = {s: one("SELECT COUNT(*) c FROM orders WHERE status=?", (s,))["c"] for s in STATUSES}
    month = int(time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d")))
    rev = one("SELECT COALESCE(SUM(total),0) t FROM orders WHERE status IN ('confirmed','out_for_delivery','delivered') AND created_at>=?", (month,))["t"]
    low = rows("SELECT id,name,stock FROM products WHERE active=1 AND stock IS NOT NULL AND stock<=3 ORDER BY stock")
    return {"counts": c, "revenue_month": rev, "low_stock": low,
            "products": one("SELECT COUNT(*) c FROM products")["c"], "newest_order": (one("SELECT MAX(id) m FROM orders") or {}).get("m") or 0}

# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "RG/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % a))

    # --- plumbing
    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name: return v
        return None

    def is_admin(self):
        return valid_session(self.cookie("rg_admin"))

    def send_json(self, obj, code=200, cookie=None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            flags = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if SECURE else "")
            self.send_header("Set-Cookie", "rg_admin=%s; Max-Age=%d; %s" % (cookie, SESSION_TTL if cookie else 0, flags))
        self.security_headers()
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(data)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8 * 1024 * 1024: raise ApiError("Request too large", 413)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError("Bad JSON")

    def ip(self):
        if TRUST_PROXY:
            return (self.headers.get("X-Nf-Client-Connection-Ip") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0])
        return self.client_address[0]

    def same_origin(self):
        o = self.headers.get("Origin")
        if not o: return True
        if SITE_ORIGIN and o.rstrip("/") == SITE_ORIGIN: return True
        return urlparse(o).netloc == self.headers.get("Host")

    # --- routing
    def do_GET(self): self.route()
    def do_HEAD(self): self.route()
    def do_POST(self): self.route()
    def do_PUT(self): self.route()
    def do_DELETE(self): self.route()

    def route(self):
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                return self.api(path, q)
            if self.command not in ("GET", "HEAD"):
                raise ApiError("Method not allowed", 405)
            return self.static(path)
        except ApiError as e:
            self.send_json({"error": e.msg}, e.code)
        except Exception as e:
            sys.stderr.write("ERROR %r\n" % (e,))
            self.send_json({"error": "Server error"}, 500)

    def api(self, path, q):
        m = self.command
        if m != "GET" and not self.same_origin():
            raise ApiError("Bad origin", 403)
        if path == "/api/products" and m == "GET":
            return self.send_json(api_products_public(self, q))
        if path == "/api/checkout" and m == "POST":
            return self.send_json(api_checkout(self, self.read_json()))
        if path == "/api/admin/login" and m == "POST":
            res, tok = api_login(self, self.read_json())
            return self.send_json(res, cookie=tok)
        if path == "/api/admin/logout" and m == "POST":
            SESSIONS.pop(self.cookie("rg_admin") or "", None)
            return self.send_json({"ok": True}, cookie="")
        if not path.startswith("/api/admin/"):
            raise ApiError("Not found", 404)
        if not self.is_admin():
            raise ApiError("Please log in", 401)
        if path == "/api/admin/me": return self.send_json({"ok": True})
        if path == "/api/admin/stats" and m == "GET": return self.send_json(admin_stats(self, q))
        if path == "/api/admin/products" and m == "GET": return self.send_json(admin_products(self, q))
        if path == "/api/admin/products" and m == "POST": return self.send_json(save_product(self.read_json()), 201)
        if path == "/api/admin/upload" and m == "POST": return self.send_json(admin_upload(self, self.read_json()))
        if path == "/api/admin/orders" and m == "GET": return self.send_json(admin_orders(self, q))
        mm = re.fullmatch(r"/api/admin/products/(\d+)", path)
        if mm:
            pid = int(mm.group(1))
            if m == "PUT": return self.send_json(save_product(self.read_json(), pid))
            if m == "DELETE":
                with _lock: db.execute("DELETE FROM products WHERE id=?", (pid,))
                return self.send_json({"ok": True})
        mm = re.fullmatch(r"/api/admin/orders/(\d+)", path)
        if mm and m == "PUT": return self.send_json(admin_order_update(int(mm.group(1)), self.read_json()))
        raise ApiError("Not found", 404)

    def static(self, path):
        if path in ("", "/"): path = "/index.html"
        rel = path.lstrip("/")
        if rel.startswith("uploads/"):
            base, rel2 = UPLOADS, rel[len("uploads/"):]
            cache = "public, max-age=31536000, immutable"
        else:
            ext = os.path.splitext(rel)[1].lower()
            top = "/" not in rel and ext == ".html"
            if not (top or rel.startswith(PUBLIC_DIRS)) or ext not in PUBLIC_EXT:
                raise ApiError("Not found", 404)
            base, rel2 = ROOT, rel
            cache = "no-cache" if ext in (".html", ".js", ".css") else "public, max-age=86400"
        full = os.path.realpath(os.path.join(base, rel2))
        if not full.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(full):
            raise ApiError("Not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache)
        if rel == "admin.html":
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.security_headers()
        self.end_headers()
        if self.command != "HEAD":
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)

# ---------------------------------------------------------------- demo data
def seed_demo():
    if one("SELECT COUNT(*) c FROM products")["c"]: return
    demo = [
        ("Biometric Smart Door Lock", "Smart Locks", 129000, 8, "Fingerprint, PIN, card and app unlock with access history.", ["assets/img/smart-lock.jpg"]),
        ("Solar PTZ 4G Camera", "CCTV Cameras", 85000, 12, "Wireless solar-powered camera with night vision and phone viewing.", ["assets/img/gallery-08.jpg"]),
        ("Dual-Lens Solar Camera", "CCTV Cameras", 95000, 2, "Dual-lens wireless camera, no cabling needed.", ["assets/img/gallery-12.jpg"]),
        ("Electric Fence Energiser", "Electric Fencing", 240000, None, "Alarm-linked energiser for wall-top fences.", ["assets/img/gallery-09.jpg"]),
    ]
    for n, c, p, s, d, im in demo:
        db.execute("INSERT INTO products(name,category,price,stock,description,images,active,created_at) VALUES(?,?,?,?,?,?,1,?)", (n, c, p, s, d, json.dumps(im), int(time.time())))
    print("Demo products added (delete them in Admin before going live).")

if __name__ == "__main__":
    if "--demo" in sys.argv: seed_demo()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print("RafGodwin site running on http://%s:%d  (admin: /admin.html)" % ("localhost" if HOST == "0.0.0.0" else HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
