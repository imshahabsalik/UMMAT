from functools import wraps
from flask import session
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
import hmac
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import smtplib
import ssl
import threading
import time
import uuid
import warnings
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import formataddr
from io import BytesIO

from PIL import Image, ImageOps
from werkzeug.middleware.proxy_fix import ProxyFix

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_local_env():
    """Load ignored local .env settings without replacing host environment values."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_local_env()
DATA_FILE = os.path.join(BASE_DIR, "stores.json")
ENQUIRY_FILE = os.path.join(BASE_DIR, "enquiries.json")
DATABASE_FILE = os.environ.get("UMMAT_DATABASE_PATH", os.path.join(BASE_DIR, "ummat.sqlite3"))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")

MAX_PRODUCTS = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024            # 5 MB per product photo or store logo
MAX_IMAGE_DIMENSION = 1600                    # bound stored images for marketplace display
MAX_IMAGE_PIXELS = 40_000_000                 # reject decompression bombs before decoding
MAX_REQUEST_BYTES = 110 * 1024 * 1024        # up to 20 product photos plus a store logo

CATEGORIES = [
    "Hardware", "Garments", "Packaging", "Electronics", "Auto Parts",
    "Industrial Products", "Agriculture & Farming", "Building Materials & Construction",
    "Chemicals", "Food & Beverages", "Furniture & Home Furnishings",
    "Home Appliances", "Medical & Healthcare", "Office Supplies",
    "Tools & Machinery", "Beauty & Personal Care", "Textiles & Fabrics",
    "Plastics & Rubber", "Other",
]
CATEGORY_ICONS = {
    "Hardware": "⚙️", "Garments": "👕", "Packaging": "📦",
    "Electronics": "🔌", "Auto Parts": "🚗", "Industrial Products": "🧰",
    "Agriculture & Farming": "🌾", "Building Materials & Construction": "🏗️",
    "Chemicals": "🧪", "Food & Beverages": "🍎",
    "Furniture & Home Furnishings": "🪑", "Home Appliances": "🏠",
    "Medical & Healthcare": "🩺", "Office Supplies": "📎",
    "Tools & Machinery": "🛠️", "Beauty & Personal Care": "🧴",
    "Textiles & Fabrics": "🧵", "Plastics & Rubber": "🧱", "Other": "🏷️",
}
# unit (singular, used for "/ piece") -> plural (used for "MOQ 20 pieces")
UNITS = {"piece": "pieces", "kg": "kg", "tonne": "tonnes", "metre": "metres",
         "litre": "litres", "box": "boxes", "carton": "cartons", "bag": "bags",
         "set": "sets", "pair": "pairs", "dozen": "dozen", "roll": "rolls"}

app = Flask(__name__)
app.secret_key = os.environ.get("UMMAT_SECRET_KEY")

if not app.secret_key:
    if os.environ.get("UMMAT_ENV", "").lower() == "production":
        raise RuntimeError("Set UMMAT_SECRET_KEY before running in production.")
    # Local development only. Configure UMMAT_SECRET_KEY before deployment.
    app.secret_key = "local-dev-" + uuid.uuid4().hex

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("UMMAT_HTTPS") == "1"),
    PERMANENT_SESSION_LIFETIME=3600 * 8,
)
app.config["SESSION_COOKIE_NAME"] = "ummat_session"
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
if os.environ.get("UMMAT_TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
os.makedirs(UPLOAD_DIR, exist_ok=True)

_lock = threading.Lock()
_transaction = threading.local()


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def protect_post_requests():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        if request.path.startswith("/api/"):
            return jsonify(error="Your session token expired. Reload the page and try again."), 400
        return "Invalid or expired form token. Please reload the page and try again.", 400


# --------------------------------------------------------------------------
# Storage helpers
# --------------------------------------------------------------------------
def _load(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _connect():
    conn = sqlite3.connect(DATABASE_FILE, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _save_stores_conn(conn, stores):
    conn.execute("DELETE FROM products")
    conn.execute("DELETE FROM stores")
    product_ids = set()
    for store_position, store in enumerate(stores):
        store_slug = store.get("slug") or f"store-{uuid.uuid4().hex[:12]}"
        store["slug"] = store_slug
        products = store.get("products", [])
        base = {key: value for key, value in store.items() if key != "products"}
        conn.execute("INSERT INTO stores(slug, position, data) VALUES (?, ?, ?)",
                     (store_slug, store_position, json.dumps(base, ensure_ascii=False)))
        for product_position, product in enumerate(products):
            product_id = product.get("id")
            if not product_id or product_id in product_ids:
                product_id = uuid.uuid4().hex
                product["id"] = product_id
            product_ids.add(product_id)
            conn.execute("INSERT INTO products(id, store_slug, position, data) VALUES (?, ?, ?, ?)",
                         (product_id, store_slug, product_position,
                          json.dumps(product, ensure_ascii=False)))


def _save_enquiries_conn(conn, items):
    conn.execute("DELETE FROM enquiries")
    for position, item in enumerate(items):
        item_id = str(item.get("id") or uuid.uuid4().hex)
        item["id"] = item_id
        conn.execute("INSERT INTO enquiries(id, position, data) VALUES (?, ?, ?)",
                     (item_id, position, json.dumps(item, ensure_ascii=False)))


def init_database():
    """Create relational tables and import the legacy JSON files once, with backups."""
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE_FILE)), exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS stores (slug TEXT PRIMARY KEY, position INTEGER NOT NULL, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, store_slug TEXT NOT NULL REFERENCES stores(slug) ON DELETE CASCADE, position INTEGER NOT NULL, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS enquiries (id TEXT PRIMARY KEY, position INTEGER NOT NULL, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS account_tokens (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, purpose TEXT NOT NULL, expires_at INTEGER NOT NULL, consumed_at INTEGER)")
        conn.execute("CREATE INDEX IF NOT EXISTS account_tokens_email_purpose ON account_tokens(email, purpose)")
        conn.execute("CREATE TABLE IF NOT EXISTS rate_limits (bucket_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at INTEGER NOT NULL)")
        imported = conn.execute("SELECT 1 FROM schema_migrations WHERE name = 'legacy_json_import_v1'").fetchone()
        if not imported:
            stores = _load(DATA_FILE)
            enquiries = _load(ENQUIRY_FILE)
            for source in (DATA_FILE, ENQUIRY_FILE):
                if os.path.isfile(source):
                    backup_path = source + ".pre-sqlite.bak"
                    if not os.path.exists(backup_path):
                        shutil.copy2(source, backup_path)
            _save_stores_conn(conn, stores)
            _save_enquiries_conn(conn, enquiries)
            conn.execute("INSERT INTO schema_migrations(name) VALUES ('legacy_json_import_v1')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_transaction():
    """Serialize a JSON-shaped read/modify/write through SQLite across processes."""
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        _transaction.connection = conn
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            del _transaction.connection
            conn.close()


def load_stores():
    active = getattr(_transaction, "connection", None)
    if active:
        conn = active
        close_conn = False
    else:
        conn = _connect()
        close_conn = True
    try:
        stores = []
        for slug, data in conn.execute("SELECT slug, data FROM stores ORDER BY position"):
            store = json.loads(data)
            store["products"] = [json.loads(row[0]) for row in conn.execute(
                "SELECT data FROM products WHERE store_slug = ? ORDER BY position", (slug,))]
            stores.append(store)
        return stores
    finally:
        if close_conn:
            conn.close()


def save_stores(stores):
    active = getattr(_transaction, "connection", None)
    if active:
        _save_stores_conn(active, stores)
    else:
        with write_transaction():
            save_stores(stores)


def load_enquiries():
    active = getattr(_transaction, "connection", None)
    if active:
        return [json.loads(row[0]) for row in active.execute("SELECT data FROM enquiries ORDER BY position")]
    conn = _connect()
    try:
        return [json.loads(row[0]) for row in conn.execute("SELECT data FROM enquiries ORDER BY position")]
    finally:
        conn.close()


def save_enquiries(items):
    active = getattr(_transaction, "connection", None)
    if active:
        _save_enquiries_conn(active, items)
    else:
        with write_transaction():
            save_enquiries(items)


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(conn, email, purpose, lifetime_seconds):
    raw = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM account_tokens WHERE expires_at < ? OR consumed_at IS NOT NULL", (now - 86400,))
    conn.execute("UPDATE account_tokens SET consumed_at=? WHERE email=? AND purpose=? AND consumed_at IS NULL",
                 (now, email, purpose))
    conn.execute("INSERT INTO account_tokens(token_hash,email,purpose,expires_at,consumed_at) VALUES(?,?,?,?,NULL)",
                 (_hash_token(raw), email, purpose, now + lifetime_seconds))
    return raw


def _mail_configured():
    return bool(os.environ.get("UMMAT_SMTP_HOST") and os.environ.get("UMMAT_EMAIL_FROM"))


def _email_verification_required():
    return os.environ.get("UMMAT_REQUIRE_EMAIL_VERIFICATION", "0").strip() == "1"


def send_email(to_address, subject, body):
    """Send through configured SMTP, or log links in local development."""
    host = os.environ.get("UMMAT_SMTP_HOST")
    sender = os.environ.get("UMMAT_EMAIL_FROM")
    if not host or not sender:
        if os.environ.get("UMMAT_ENV", "development").lower() != "production":
            app.logger.warning("Development email to %s | %s\n%s", to_address, subject, body)
            return True
        app.logger.error("SMTP is not configured; email to %s could not be sent", to_address)
        return False
    message = EmailMessage()
    display_name = os.environ.get("UMMAT_EMAIL_NAME", "UMMAT").strip() or "UMMAT"
    message["From"] = formataddr((display_name, sender))
    message["To"], message["Subject"] = to_address, subject
    message.set_content(body)
    port = int(os.environ.get("UMMAT_SMTP_PORT", "465" if os.environ.get("UMMAT_SMTP_SSL") == "1" else "587"))
    timeout = 15
    try:
        if os.environ.get("UMMAT_SMTP_SSL") == "1" or port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as server:
                if os.environ.get("UMMAT_SMTP_USERNAME"):
                    server.login(os.environ["UMMAT_SMTP_USERNAME"], os.environ.get("UMMAT_SMTP_PASSWORD", ""))
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                if os.environ.get("UMMAT_SMTP_USERNAME"):
                    server.login(os.environ["UMMAT_SMTP_USERNAME"], os.environ.get("UMMAT_SMTP_PASSWORD", ""))
                server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        app.logger.exception("Email delivery failed for %s", to_address)
        return False


def _public_url(endpoint, **values):
    base = os.environ.get("UMMAT_PUBLIC_BASE_URL", request.url_root.rstrip("/"))
    return base.rstrip("/") + url_for(endpoint, **values)


def rate_limited(name, limit, window_seconds):
    """SQLite-backed fixed window limiter shared by all worker processes."""
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method in ("GET", "HEAD", "OPTIONS"):
                return view(*args, **kwargs)
            now = int(time.time())
            identity = request.remote_addr or "unknown"
            key = hashlib.sha256(f"{name}:{identity}".encode()).hexdigest()
            with write_transaction():
                conn = _transaction.connection
                row = conn.execute("SELECT attempts,reset_at FROM rate_limits WHERE bucket_key=?", (key,)).fetchone()
                if not row or row[1] <= now:
                    conn.execute("INSERT INTO rate_limits(bucket_key,attempts,reset_at) VALUES(?,?,?) ON CONFLICT(bucket_key) DO UPDATE SET attempts=excluded.attempts,reset_at=excluded.reset_at",
                                 (key, 1, now + window_seconds))
                    allowed = True
                elif row[0] >= limit:
                    allowed = False
                else:
                    conn.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE bucket_key=?", (key,))
                    allowed = True
            if not allowed:
                if request.path.startswith("/api/"):
                    return jsonify(error="Too many requests. Please try again later."), 429
                return "Too many attempts. Please try again later.", 429
            return view(*args, **kwargs)
        return wrapped
    return decorate


init_database()


def slugify(value):
    value = re.sub(r"[^a-zA-Z0-9\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value).strip("-")[:60]


def unique_slug(name, stores):
    base = slugify(name) or "store-" + uuid.uuid4().hex[:8]  # e.g. Hindi/Urdu names
    taken = {s.get("slug") for s in stores}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


# --------------------------------------------------------------------------
# Display helpers (also work for older records where price/moq are strings)
# --------------------------------------------------------------------------
def _inr(n):
    s = f"{float(n):.2f}".rstrip("0").rstrip(".")
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", whole[:-3])
        whole = head + "," + whole[-3:]
    return "₹" + whole + ("." + frac if frac else "")


@app.template_filter("price_text")
def price_text(p):
    v = p.get("price")
    if v in (None, ""):
        return "Contact for price"
    if isinstance(v, str):
        return v
    return f"{_inr(v)} / {p.get('unit', 'piece')}"


@app.template_filter("moq_text")
def moq_text(p):
    v = p.get("moq")
    if v in (None, ""):
        return "Contact for details"
    if isinstance(v, str):
        return v
    return f"{v} {UNITS.get(p.get('unit', 'piece'), 'pieces')}"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def parse_price(raw):
    raw = raw.strip().replace(",", "").lstrip("₹").replace("Rs.", "").strip()
    if not raw:
        return None, None                       # explicit: "Contact for price"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, "Price must be a number, e.g. 120 or 85.50 (or leave blank)."
    if not value.is_finite() or value <= 0 or value > Decimal("100000000"):
        return None, "Price must be greater than 0 and below ₹10 crore."
    value = round(float(value), 2)
    return (int(value) if value == int(value) else value), None


def parse_moq(raw):
    raw = raw.strip().replace(",", "")
    if not raw:
        return None, "Minimum order quantity is required."
    if not raw.isdigit() or not (1 <= int(raw) <= 10_000_000):
        return None, "Minimum order quantity must be a whole number of 1 or more."
    return int(raw), None


def normalise_phone(raw):
    """Return digits only (India default: 10 digits -> +91), or None if invalid."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    return digits if 11 <= len(digits) <= 15 else None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Magic numbers: we decide the type from the file's bytes, never its name.
def detect_image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def read_image(file_storage):
    """-> (bytes|None, ext|None, error|None). Empty upload is fine (optional)."""
    if not file_storage or not file_storage.filename:
        return None, None, None
    data = file_storage.stream.read(MAX_IMAGE_BYTES + 1)
    if not data:
        return None, None, None
    if len(data) > MAX_IMAGE_BYTES:
        return None, None, "Image is larger than 5 MB. Please choose a smaller one."
    try:
        detected = detect_image_type(data)
        if detected is None:
            return None, None, "Photo must be a valid JPG, PNG or WebP image."
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                fmt = (image.format or "").upper()
                ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(fmt)
                if ext != detected:
                    return None, None, "Photo must be a valid JPG, PNG or WebP image."
                if getattr(image, "n_frames", 1) != 1:
                    return None, None, "Photo must be a non-animated JPG, PNG or WebP image."
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    return None, None, "Image dimensions are too large. Please use an image under 40 megapixels."
                image.verify()
            with Image.open(BytesIO(data)) as image:
                oriented = ImageOps.exif_transpose(image)
                try:
                    oriented.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                                       Image.Resampling.LANCZOS, reducing_gap=3.0)
                    prepared = oriented
                    if fmt == "JPEG" and prepared.mode not in ("RGB", "L"):
                        prepared = oriented.convert("RGB")
                    elif fmt == "PNG":
                        has_transparency = "A" in prepared.getbands() or "transparency" in prepared.info
                        if has_transparency and prepared.mode not in ("RGBA", "LA"):
                            prepared = oriented.convert("RGBA")
                        elif not has_transparency and prepared.mode not in ("RGB", "L"):
                            prepared = oriented.convert("RGB")
                    elif fmt == "WEBP" and prepared.mode not in ("RGB", "RGBA"):
                        prepared = oriented.convert("RGBA" if "A" in oriented.getbands()
                                                    or "transparency" in oriented.info else "RGB")
                    try:
                        with BytesIO() as output:
                            if fmt == "JPEG":
                                prepared.save(output, format="JPEG", quality=85, optimize=False)
                            elif fmt == "PNG":
                                # Low compression level is much faster than optimize=True; alpha is retained above.
                                prepared.save(output, format="PNG", compress_level=3, optimize=False)
                            else:
                                prepared.save(output, format="WEBP", quality=85, method=4)
                            output.seek(0)
                            optimized = output.read()
                    finally:
                        if prepared is not oriented:
                            prepared.close()
                finally:
                    if oriented is not image:
                        oriented.close()
                if len(optimized) > MAX_IMAGE_BYTES:
                    return None, None, "Optimized image is larger than 5 MB. Please choose a smaller one."
                return optimized, ext, None
    except Exception as exc:
        app.logger.info("Rejected invalid image upload (%s).", type(exc).__name__)
        return None, None, "Photo must be a valid JPG, PNG or WebP image."


def validate_store_form(form, files, stores):
    """Return (clean_store | None, errors, row_errors, rows_for_redisplay)."""
    errors, row_errors = [], {}

    name = form.get("business_name", "").strip()
    personal_name = form.get("personal_name", "").strip()
    category = form.get("category", "").strip()
    location = form.get("location", "").strip()
    description = form.get("description", "").strip()
    phone_raw = form.get("phone", "").strip()

    if not name:
        errors.append("Business name is required.")
    elif len(name) > 120:
        errors.append("Business name must be 120 characters or fewer.")
    if category not in CATEGORIES:
        errors.append("Please choose a business category.")
    if not personal_name:
        errors.append("Personal name is required.")
    elif len(personal_name) > 100:
        errors.append("Personal name must be 100 characters or fewer.")
    if not location:
        errors.append("Business location is required.")
    elif len(location) > 120:
        errors.append("Location must be 120 characters or fewer.")
    if len(description) > 1000:
        errors.append("Business description must be 1000 characters or fewer.")
    phone = None
    if phone_raw:
        phone = normalise_phone(phone_raw)
        if not phone:
            errors.append("Phone / WhatsApp number looks invalid. Use digits only, e.g. 9876543210.")

    if name and location and any(
            s.get("business_name", "").strip().lower() == name.lower()
            and s.get("location", "").strip().lower() == location.lower()
            for s in stores):
        errors.append("A store with this business name and location already exists. "
                      "If it is a different business, add a distinguishing word to the name.")

    names = form.getlist("product_name")
    prices = form.getlist("product_price")
    units = form.getlist("product_unit")
    moqs = form.getlist("product_moq")
    descs = form.getlist("product_description")
    images = files.getlist("product_image")

    n = len(names)
    if not all(len(x) == n for x in (prices, units, moqs, descs, images)):
        errors.append("The product form was incomplete. Please reload the page and try again.")
        return None, errors, row_errors, []
    if n > MAX_PRODUCTS:
        errors.append(f"You can add up to {MAX_PRODUCTS} products at a time.")
        return None, errors, row_errors, []

    rows, products, staged = [], [], []
    for i in range(n):
        row = {"name": names[i].strip(), "price": prices[i].strip(),
               "unit": units[i] if units[i] in UNITS else "piece",
               "moq": moqs[i].strip(), "description": descs[i].strip()}
        rows.append(row)
        has_image = bool(images[i] and images[i].filename)
        if not (row["name"] or row["price"] or row["moq"] or row["description"] or has_image):
            continue  # untouched blank row: ignore

        errs = []
        if not row["name"]:
            errs.append("Product name is required.")
        elif len(row["name"]) > 160:
            errs.append("Product name must be 160 characters or fewer.")
        price, e = parse_price(row["price"])
        if e:
            errs.append(e)
        moq, e = parse_moq(row["moq"])
        if e:
            errs.append(e)
        if len(row["description"]) > 1000:
            errs.append("Description must be 1000 characters or fewer.")
        data, ext, e = read_image(images[i])
        if e:
            errs.append(e)
        if errs:
            row_errors[i] = errs
            continue
        products.append({"name": row["name"], "category": category, "price": price,
                         "moq": moq, "unit": row["unit"],
                         "description": row["description"], "image": None})
        staged.append((len(products) - 1, data, ext))

    if not products and not row_errors:
        errors.append("Add at least one product (name and minimum order quantity are required).")
    if errors or row_errors:
        return None, errors, row_errors, rows

    store = {"business_name": name, "personal_name": personal_name,
             "category": category, "location": location,
             "description": description, "phone": phone, "products": products,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return (store, staged), errors, row_errors, rows


def validate_product_form(form, files, category, existing=None):
    """Validate a single management-form product and retain its old photo if omitted."""
    name = form.get("product_name", "").strip()
    raw_price = form.get("product_price", "").strip()
    price, price_error = parse_price(raw_price)
    if not raw_price and isinstance((existing or {}).get("price"), str):
        # Keep legacy free-text prices such as "Contact for price" when edited.
        price = existing["price"]
    moq, moq_error = parse_moq(form.get("product_moq", ""))
    description = form.get("product_description", "").strip()
    unit = form.get("product_unit", "piece")
    errors = []
    if not name:
        errors.append("Product name is required.")
    elif len(name) > 160:
        errors.append("Product name must be 160 characters or fewer.")
    if price_error:
        errors.append(price_error)
    if moq_error:
        errors.append(moq_error)
    if len(description) > 1000:
        errors.append("Description must be 1000 characters or fewer.")
    if unit not in UNITS:
        errors.append("Please choose a valid price unit.")
        unit = "piece"
    image, ext, image_error = read_image(files.get("product_image"))
    if image_error:
        errors.append(image_error)
    product = {
        "id": (existing or {}).get("id") or uuid.uuid4().hex,
        "name": name, "category": (existing or {}).get("category", category), "price": price, "moq": moq,
        "unit": unit, "description": description,
        "image": (existing or {}).get("image"),
    }
    return (product, image, ext), errors


def write_images(staged, products):
    """Save validated images under generated names. Returns the paths written."""
    written = []
    try:
        for idx, data, ext in staged:
            if data is None:
                continue
            path, relative = save_uploaded_image(data, ext)
            written.append(path)
            products[idx]["image"] = relative
    except Exception:
        for path in written:
            os.remove(path)
        raise
    return written


def save_uploaded_image(data, ext):
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    try:
        with open(path, "wb") as image_file:
            image_file.write(data)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise
    return path, f"uploads/{filename}"


def delete_unused_image(image, stores):
    """Remove only app-generated upload paths that no remaining product uses."""
    if not image or any(s.get("logo") == image for s in stores) or any(
        p.get("image") == image for s in stores for p in s.get("products", [])
    ):
        return
    if not re.fullmatch(r"uploads/[0-9a-f]{32}\.(jpg|png|webp)", image):
        return
    path = os.path.abspath(os.path.join(UPLOAD_DIR, os.path.basename(image)))
    if os.path.commonpath([os.path.abspath(UPLOAD_DIR), path]) == os.path.abspath(UPLOAD_DIR) and os.path.isfile(path):
        os.remove(path)


# --------------------------------------------------------------------------
# Search / filtering
# --------------------------------------------------------------------------
def search_marketplace(stores, q="", category=""):
    tokens = q.lower().split()
    matched_stores, matched_products = [], []
    for s in stores:
        store_hay = " ".join([s.get("business_name", ""), s.get("category", ""),
                              s.get("location", "")]).lower()
        cat_ok = not category or s.get("category") == category
        if cat_ok and all(t in store_hay for t in tokens):
            matched_stores.append(s)
        for idx, p in enumerate(s.get("products", [])):
            hay = " ".join([p.get("name", ""), p.get("description", ""),
                            p.get("category", ""), store_hay]).lower()
            if cat_ok and all(t in hay for t in tokens):
                matched_products.append({**p, "index": idx,
                                         "store_name": s["business_name"],
                                         "store_slug": s["slug"],
                                     "store_logo": s.get("logo"),
                                         "location": s.get("location", "")})
    return matched_stores, matched_products


def generate_listing(business_name, category, location, raw_products):
    """Generate a basic product listing locally without an AI API."""

    business_name = business_name.strip()
    category = category.strip()
    location = location.strip()

    category_keywords = {
        "Hardware": [
            "pipe", "valve", "bolt", "nut", "screw",
            "tool", "fitting", "hardware", "plumbing"
        ],
        "Garments": [
            "shirt", "trouser", "fabric", "clothing",
            "jeans", "t-shirt", "garment", "cotton"
        ],
        "Packaging": [
            "carton", "box", "packaging", "pouch",
            "container", "wrapper"
        ],
        "Electronics": [
            "cable", "wire", "switch", "charger",
            "electronic", "led", "circuit"
        ],
        "Auto Parts": [
            "brake", "bearing", "engine", "vehicle",
            "automotive", "auto part"
        ],
        "Industrial": [
            "machine", "motor", "industrial",
            "pump", "compressor"
        ]
    }

    products = []

    lines = [
        line.strip()
        for line in raw_products.splitlines()
        if line.strip()
    ]

    for line in lines[:20]:
        parts = [part.strip() for part in line.split("|")]

        product_name = parts[0][:160]

        if not product_name:
            continue

        # Keep supplier-entered prices and MOQ unchanged.
        price = (
            parts[1][:80]
            if len(parts) > 1 and parts[1]
            else "Contact for price"
        )

        moq = (
            parts[2][:80]
            if len(parts) > 2 and parts[2]
            else "Contact for details"
        )

        # Use the supplier's category when provided.
        product_category = category or "General"

        if not category:
            name_lower = product_name.lower()

            for cat, keywords in category_keywords.items():
                if any(word in name_lower for word in keywords):
                    product_category = cat
                    break

        description = (
            f"{product_name} offered by {business_name}. "
            "Contact the supplier for product specifications, "
            "availability, bulk pricing and delivery details."
        )

        products.append({
            "name": product_name,
            "price": price,
            "moq": moq,
            "category": product_category,
            "description": description
        })

    if not products:
        raise ValueError("Enter at least one valid product name.")

    business_slug = slugify(business_name)

    if not business_slug:
        raise ValueError("Please enter a valid business name.")

    return {
        "business_name": business_name[:120],
        "slug": business_slug,
        "category": category or "General",
        "location": location or "Contact supplier",
        "description": (
            f"{business_name} is listed on UMMAT. "
            "Contact the supplier for product information "
            "and wholesale enquiries."
        ),
        "products": products
    }

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("supplier_email"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_owned_store():
    email = session.get("supplier_email")
    if not email:
        return None

    return next(
        (
            store for store in load_stores()
            if str(store.get("owner_email") or "").lower() == email.lower()
        ),
        None,
    )
# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    return {"CATEGORIES": CATEGORIES, "CATEGORY_ICONS": CATEGORY_ICONS,
            "UNITS": UNITS, "csrf_token": csrf_token}


@app.route("/")
def home():
    q = request.args.get("q", "").strip()[:100]
    category = request.args.get("category", "").strip()
    if category not in CATEGORIES:
        category = ""
    stores = list(reversed(load_stores()))            # newest first
    all_products = sum(len(s.get("products", [])) for s in stores)
    filtered = bool(q or category)
    shown_stores, products = search_marketplace(stores, q, category)
    return render_template("index.html", stores=shown_stores, products=products[:60],
                           product_total=len(products), q=q, category=category,
                           filtered=filtered, has_any_products=all_products > 0)


@app.route("/about")
def about():
    return render_template("about.html")


def _render_create(status=200, **ctx):
    ctx.setdefault("form", {})
    ctx.setdefault("errors", [])
    ctx.setdefault("row_errors", {})
    if not ctx.get("rows"):
        ctx["rows"] = [{"name": "", "price": "", "unit": "piece", "moq": "", "description": ""}]
    return render_template("create_store.html", max_products=MAX_PRODUCTS, **ctx), status


@app.route("/create-store", methods=["GET", "POST"])
@rate_limited("registration", 5, 3600)
def create_store():
    if request.method == "GET":
        return _render_create()

    verification_required = _email_verification_required()
    if (verification_required
            and os.environ.get("UMMAT_ENV", "development").lower() == "production"
            and not _mail_configured()):
        return _render_create(503, form=request.form,
                              errors=["Account email is temporarily unavailable. Please try again later."])

    account_email = request.form.get("account_email", "").strip().lower()
    account_password = request.form.get("account_password", "")
    account_password_confirm = request.form.get("account_password_confirm", "")
    if len(account_email) > 254 or not EMAIL_RE.fullmatch(account_email):
        return _render_create(400, form=request.form,
                              errors=["Enter a valid account email address."])
    if len(account_password) < 12 or len(account_password) > 128:
        return _render_create(400, form=request.form,
                              errors=["Password must be between 12 and 128 characters."])
    if account_password != account_password_confirm:
        return _render_create(400, form=request.form,
                              errors=["The passwords do not match. Retype the same password in both fields."])

    logo_data, logo_ext, logo_error = read_image(request.files.get("store_logo"))
    if logo_error:
        return _render_create(400, form=request.form, errors=[logo_error])

    with write_transaction():
        stores = load_stores()
        if any(str(s.get("owner_email") or "").lower() == account_email for s in stores):
            return _render_create(400, form=request.form,
                                  errors=["An account already exists for this email address. Please log in."])
        result, errors, row_errors, rows = validate_store_form(request.form, request.files, stores)
        if errors or row_errors:
            return _render_create(400, form=request.form, errors=errors,
                                  row_errors=row_errors, rows=rows or None)

        store, staged = result
        store["slug"] = unique_slug(store["business_name"], stores)
        store["owner_email"] = account_email
        store["password_hash"] = generate_password_hash(account_password)
        store["email_verified"] = not verification_required
        written = []
        try:
            written = write_images(staged, store["products"])
            if logo_data:
                logo_path, store["logo"] = save_uploaded_image(logo_data, logo_ext)
                written.append(logo_path)
            stores.append(store)
            save_stores(stores)
            token = (_issue_token(_transaction.connection, account_email, "verify", 24 * 3600)
                     if verification_required else None)
        except Exception:
            for path in written:                  # don't leave orphan images behind
                if os.path.exists(path):
                    os.remove(path)
            app.logger.exception("Could not save store")
            return _render_create(500, form=request.form,
                                  errors=["Something went wrong while saving your store. Please try again."],
                                  rows=rows)

    session.clear()
    if token:
        link = _public_url("verify_email", token=token)
        send_email(account_email, "Verify your UMMAT supplier account",
                   f"Confirm your email address to activate your account:\n\n{link}\n\nThis link expires in 24 hours.")
    return redirect(url_for("login", registered=1))


@app.route("/login", methods=["GET", "POST"])
@rate_limited("login", 10, 900)
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(email) > 254 or not EMAIL_RE.fullmatch(email) or len(password) > 128:
            return render_template("login.html", error="Invalid email or password."), 401
        store = next((s for s in load_stores()
                      if str(s.get("owner_email") or "").lower() == email), None)
        if (not store or not store.get("password_hash")
                or not check_password_hash(store["password_hash"], password)):
            return render_template("login.html", error="Invalid email or password."), 401
        if _email_verification_required() and not store.get("email_verified", True):
            return render_template("login.html", error="Please verify your email before logging in.",
                                   needs_verification=True), 403
        session.clear()
        session["supplier_email"] = email
        session.permanent = True
        return redirect(url_for("dashboard"))
    return render_template("login.html", error=None, needs_verification=False)


@app.route("/verify-email/<token>", methods=["GET", "POST"])
def verify_email(token):
    if request.method == "GET":
        return render_template("account_token.html", mode="verify", token=token, error=None)
    with write_transaction():
        row = _transaction.connection.execute(
            "SELECT email,expires_at,consumed_at FROM account_tokens WHERE token_hash=? AND purpose='verify'",
            (_hash_token(token),)).fetchone()
        now = int(time.time())
        if not row or row[1] <= now or row[2] is not None:
            return render_template("account_token.html", mode="verify", token=token,
                                   error="This link is invalid or expired. Request another verification email."), 400
        stores = load_stores()
        target = next((s for s in stores if str(s.get("owner_email", "")).lower() == row[0]), None)
        if not target:
            return render_template("account_token.html", mode="verify", token=token,
                                   error="This account could not be found."), 400
        target["email_verified"] = True
        save_stores(stores)
        _transaction.connection.execute("UPDATE account_tokens SET consumed_at=? WHERE token_hash=?", (now, _hash_token(token)))
    return redirect(url_for("login", verified=1))


@app.route("/resend-verification", methods=["GET", "POST"])
@rate_limited("resend-verification", 3, 3600)
def resend_verification():
    if not _email_verification_required():
        return redirect(url_for("login"))
    message = None
    if request.method == "POST":
        if os.environ.get("UMMAT_ENV", "development").lower() == "production" and not _mail_configured():
            return render_template("account_request.html", mode="verify",
                                   message="Email delivery is temporarily unavailable. Please contact the site administrator.")
        email = request.form.get("email", "").strip().lower()
        if EMAIL_RE.fullmatch(email) and len(email) <= 254:
            with write_transaction():
                store = next((s for s in load_stores() if str(s.get("owner_email", "")).lower() == email), None)
                if store and not store.get("email_verified", True):
                    token = _issue_token(_transaction.connection, email, "verify", 24 * 3600)
                else:
                    token = None
            if token:
                send_email(email, "Verify your UMMAT supplier account",
                           f"Confirm your email address:\n\n{_public_url('verify_email', token=token)}")
        message = "If that account needs verification, an email with instructions has been sent."
    return render_template("account_request.html", mode="verify", message=message)


@app.route("/forgot-password", methods=["GET", "POST"])
@rate_limited("forgot-password", 3, 3600)
def forgot_password():
    message = None
    if request.method == "POST":
        if os.environ.get("UMMAT_ENV", "development").lower() == "production" and not _mail_configured():
            return render_template("account_request.html", mode="reset",
                                   message="Email delivery is temporarily unavailable. Please contact the site administrator.")
        email = request.form.get("email", "").strip().lower()
        if EMAIL_RE.fullmatch(email) and len(email) <= 254:
            with write_transaction():
                store = next((s for s in load_stores() if str(s.get("owner_email", "")).lower() == email), None)
                token = _issue_token(_transaction.connection, email, "reset", 3600) if store and store.get("password_hash") else None
            if token:
                send_email(email, "Reset your UMMAT password",
                           f"Choose a new password within one hour:\n\n{_public_url('reset_password', token=token)}")
        message = "If an account exists for that email, password reset instructions have been sent."
    return render_template("account_request.html", mode="reset", message=message)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 12 or len(password) > 128:
            error = "Password must be between 12 and 128 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            with write_transaction():
                now = int(time.time())
                row = _transaction.connection.execute(
                    "SELECT email,expires_at,consumed_at FROM account_tokens WHERE token_hash=? AND purpose='reset'",
                    (_hash_token(token),)).fetchone()
                if not row or row[1] <= now or row[2] is not None:
                    error = "This link is invalid or expired. Request another password reset."
                else:
                    stores = load_stores()
                    target = next((s for s in stores if str(s.get("owner_email", "")).lower() == row[0]), None)
                    if target:
                        target["password_hash"] = generate_password_hash(password)
                        save_stores(stores)
                        _transaction.connection.execute("UPDATE account_tokens SET consumed_at=? WHERE token_hash=?", (now, _hash_token(token)))
                    else:
                        error = "This account could not be found."
            if error is None:
                return redirect(url_for("login", password_reset=1))
    return render_template("account_token.html", mode="reset", token=token, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    store = get_owned_store()
    if not store:
        session.clear()
        return redirect(url_for("login"))
    enquiries = [item for item in reversed(load_enquiries()) if item.get("store_slug") == store.get("slug")][:50]
    return render_template("dashboard.html", store=store, enquiries=enquiries)


@app.route("/manage-store", methods=["GET", "POST"])
@login_required
def manage_store():
    errors = []
    with write_transaction():
        stores = load_stores()
        store = next((s for s in stores if str(s.get("owner_email") or "").lower() ==
                      session.get("supplier_email", "").lower()), None)
        if not store:
            session.clear()
            return redirect(url_for("login"))
        if request.method == "POST":
            personal_name = request.form.get("personal_name", "").strip()
            name = request.form.get("business_name", "").strip()
            category = request.form.get("category", "").strip()
            location = request.form.get("location", "").strip()
            description = request.form.get("description", "").strip()
            phone_raw = request.form.get("phone", "").strip()
            phone = normalise_phone(phone_raw) if phone_raw else None
            logo_data, logo_ext, logo_error = read_image(request.files.get("store_logo"))
            if logo_error:
                errors.append(logo_error)
            if not name or len(name) > 120:
                errors.append("Business name is required and must be 120 characters or fewer.")
            if not personal_name:
                errors.append("Personal name is required.")
            elif len(personal_name) > 100:
                errors.append("Personal name must be 100 characters or fewer.")
            if category not in CATEGORIES:
                errors.append("Please choose a business category.")
            if not location or len(location) > 120:
                errors.append("Location is required and must be 120 characters or fewer.")
            if len(description) > 1000:
                errors.append("Business description must be 1000 characters or fewer.")
            if phone_raw and not phone:
                errors.append("Phone / WhatsApp number looks invalid. Use a valid phone number.")
            if name and location and any(
                other is not store
                and other.get("business_name", "").strip().lower() == name.lower()
                and other.get("location", "").strip().lower() == location.lower()
                for other in stores
            ):
                errors.append("A store with this business name and location already exists.")
            if not errors:
                # Keep the original slug so existing public links remain valid.
                old_logo = store.get("logo")
                new_logo_path = None
                try:
                    new_logo = old_logo
                    if logo_data:
                        new_logo_path, new_logo = save_uploaded_image(logo_data, logo_ext)
                    elif request.form.get("remove_logo") == "1":
                        new_logo = None
                    store.update(personal_name=personal_name, business_name=name, category=category, location=location,
                                 description=description, phone=phone, logo=new_logo)
                    save_stores(stores)
                except Exception:
                    if new_logo_path and os.path.exists(new_logo_path):
                        os.remove(new_logo_path)
                    app.logger.exception("Could not save store details")
                    return render_template("manage_store.html", store=store,
                                           errors=["Could not save store details. Please try again."]), 500
                if old_logo and old_logo != store.get("logo"):
                    delete_unused_image(old_logo, stores)
                return redirect(url_for("dashboard"))
        return render_template("manage_store.html", store=store, errors=errors)


@app.route("/manage-store/delete", methods=["POST"])
@login_required
def delete_store():
    email = session.get("supplier_email", "").lower()
    removed_assets = []
    removed_slug = None
    with write_transaction():
        stores = load_stores()
        store = next((s for s in stores if str(s.get("owner_email") or "").lower() == email), None)
        if not store:
            session.clear()
            return redirect(url_for("login"))
        removed_slug = store.get("slug")
        removed_assets.extend([store.get("logo")])
        removed_assets.extend(product.get("image") for product in store.get("products", []))
        remaining = [item for item in stores if item is not store]
        save_stores(remaining)
        enquiries = load_enquiries()
        save_enquiries([item for item in enquiries if item.get("store_slug") != removed_slug])
    for image in removed_assets:
        delete_unused_image(image, remaining)
    app.logger.info("Supplier store deleted: %s", removed_slug)
    session.clear()
    return redirect(url_for("home"))


@app.route("/manage-products/add", methods=["GET", "POST"])
@login_required
def add_product():
    with write_transaction():
        stores = load_stores()
        store = next((s for s in stores if str(s.get("owner_email") or "").lower() ==
                      session.get("supplier_email", "").lower()), None)
        if not store:
            session.clear()
            return redirect(url_for("login"))
        products = store.setdefault("products", [])
        if request.method == "POST":
            if len(products) >= MAX_PRODUCTS:
                return render_template("manage_product.html", product=request.form,
                                       store=store, errors=[f"You can have up to {MAX_PRODUCTS} products."]), 400
            (product, image, ext), errors = validate_product_form(request.form, request.files, store["category"])
            if not errors:
                staged = [(0, image, ext)] if image else []
                written = []
                try:
                    written = write_images(staged, [product])
                    products.append(product)
                    save_stores(stores)
                except Exception:
                    for path in written:
                        if os.path.exists(path):
                            os.remove(path)
                    app.logger.exception("Could not add product")
                    return render_template("manage_product.html", product=request.form,
                                           store=store, errors=["Could not save the product. Please try again."]), 500
                return redirect(url_for("dashboard"))
            return render_template("manage_product.html", product=request.form,
                                   store=store, errors=errors), 400
        return render_template("manage_product.html", product={}, store=store, errors=[])


def _product_position(products, product_id):
    # Stable IDs for newer records; indexes remain supported for legacy records.
    for i, product in enumerate(products):
        if product.get("id") == product_id:
            return i
    if product_id.isdigit() and int(product_id) < len(products):
        return int(product_id)
    return None


@app.route("/manage-products/<product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    with write_transaction():
        stores = load_stores()
        store = next((s for s in stores if str(s.get("owner_email") or "").lower() ==
                      session.get("supplier_email", "").lower()), None)
        if not store:
            session.clear()
            return redirect(url_for("login"))
        products = store.setdefault("products", [])
        pos = _product_position(products, product_id)
        if pos is None:
            abort(404)
        old_product = products[pos]
        if request.method == "POST":
            (product, image, ext), errors = validate_product_form(
                request.form, request.files, store["category"], existing=old_product)
            if errors:
                return render_template("manage_product.html", product=request.form,
                                       store=store, errors=errors, editing=True), 400
            old_image = old_product.get("image")
            written = []
            try:
                if image:
                    written = write_images([(0, image, ext)], [product])
                products[pos] = product
                # Give this legacy item and any other legacy items stable IDs on write.
                for item in products:
                    item.setdefault("id", uuid.uuid4().hex)
                save_stores(stores)
            except Exception:
                products[pos] = old_product
                for path in written:
                    if os.path.exists(path):
                        os.remove(path)
                app.logger.exception("Could not update product")
                return render_template("manage_product.html", product=request.form,
                                       store=store, errors=["Could not save the product. Please try again."], editing=True), 500
            if old_image and old_image != product.get("image"):
                delete_unused_image(old_image, stores)
            return redirect(url_for("dashboard"))
        return render_template("manage_product.html", product=old_product,
                               store=store, errors=[], editing=True)


@app.route("/manage-products/<product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id):
    with write_transaction():
        stores = load_stores()
        store = next((s for s in stores if str(s.get("owner_email") or "").lower() ==
                      session.get("supplier_email", "").lower()), None)
        if not store:
            session.clear()
            return redirect(url_for("login"))
        products = store.get("products", [])
        pos = _product_position(products, product_id)
        if pos is None:
            abort(404)
        old_image = products[pos].get("image")
        del products[pos]
        save_stores(stores)
        delete_unused_image(old_image, stores)
    return redirect(url_for("dashboard"))


@app.route("/store/<slug>")
def store(slug):
    supplier = next((s for s in load_stores() if s.get("slug") == slug), None)
    if not supplier:
        abort(404)
    return render_template("store.html", supplier=supplier,
                           created=request.args.get("created") == "1")


@app.route("/api/stores")
def api_stores():
    private_fields = {"owner_email", "password_hash", "personal_name"}
    return jsonify([{k: v for k, v in store.items() if k not in private_fields}
                    for store in load_stores()])


# ---- Buyer enquiries ------------------------------------------------------
@app.route("/api/enquiry", methods=["POST"])
@rate_limited("quote-enquiry", 15, 3600)
def api_enquiry():
    d = request.get_json(silent=True) or {}
    if d.get("website"):                          # honeypot field: bots fill it in
        return jsonify(ok=True)

    slug = str(d.get("store_slug", ""))
    supplier = next((s for s in load_stores() if s.get("slug") == slug), None)
    if not supplier:
        return jsonify(error="That store no longer exists."), 404

    name = str(d.get("buyer_name", "")).strip()
    contact = str(d.get("contact", "")).strip()
    quantity = str(d.get("quantity", "")).strip()
    message = str(d.get("message", "")).strip()
    errors = {}
    if not name or len(name) > 100:
        errors["buyer_name"] = "Please enter your name."
    if not (EMAIL_RE.match(contact) or normalise_phone(contact)) or len(contact) > 120:
        errors["contact"] = "Enter a valid phone number or email so the supplier can reply."
    if len(quantity) > 60:
        errors["quantity"] = "Quantity is too long."
    if len(message) > 1000:
        errors["message"] = "Message must be 1000 characters or fewer."
    if errors:
        return jsonify(error="Please fix the highlighted fields.", fields=errors), 400

    product_name = ""
    idx = d.get("product_index")
    products = supplier.get("products", [])
    if isinstance(idx, int) and 0 <= idx < len(products):
        product_name = products[idx].get("name", "")
    else:
        product_name = str(d.get("product_name", "")).strip()
        if len(product_name) > 160:
            return jsonify(error="Product name must be 160 characters or fewer.",
                           fields={"product_name": "Product name is too long."}), 400

    enquiry = {"id": uuid.uuid4().hex[:12],
               "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "store_slug": slug, "store_name": supplier["business_name"],
               "product": product_name, "product_name": product_name,
               "buyer_name": name, "contact": contact,
               "quantity": quantity, "message": message}
    with write_transaction():
        items = load_enquiries()
        items.append(enquiry)
        save_enquiries(items)
    recipients = []
    if supplier.get("owner_email") and supplier.get("email_verified", True):
        recipients.append(supplier["owner_email"])
    sales_email = os.environ.get("UMMAT_SALES_EMAIL", "").strip()
    if sales_email and EMAIL_RE.fullmatch(sales_email):
        recipients.append(sales_email)
    body = (f"Store: {supplier['business_name']}\n"
            f"Sales contact: {supplier.get('personal_name') or 'Not provided'}\n"
            f"Buyer: {name}\nContact: {contact}\n"
            f"Product: {product_name or 'General enquiry'}\n"
            f"Quantity: {quantity or 'Not specified'}\n\n"
            f"{message or '(No message)'}\n\nView the supplier inbox: {_public_url('dashboard')}")
    for recipient in dict.fromkeys(recipients):
        send_email(recipient, f"New buyer enquiry for {supplier['business_name']}", body)
    return jsonify(ok=True)


@app.route("/admin/enquiries")
def admin_enquiries():
    """Owner-only list. Disabled unless UMMAT_ADMIN_KEY is set in the environment."""
    key = os.environ.get("UMMAT_ADMIN_KEY", "")
    if not key or not hmac.compare_digest(request.args.get("key", ""), key):
        abort(404)
    return render_template("enquiries.html", items=list(reversed(load_enquiries())))


# ---- Optional rule-based generator (kept for compatibility; not required) --
@app.route("/api/generate-listing", methods=["POST"])
def api_generate_listing():
    payload = request.get_json(silent=True) or {}

    name = str(payload.get("business_name", "")).strip()
    category = str(payload.get("category", "")).strip()
    location = str(payload.get("location", "")).strip()
    raw_products = str(payload.get("products", "")).strip()

    if not name or not raw_products:
        return jsonify(error="Enter a business name and at least one product."), 400

    if len(name) > 120 or len(category) > 120 or len(location) > 120 or len(raw_products) > 12000:
        return jsonify(error="Some fields are too long. Shorten them and try again."), 400

    try:
        return jsonify(generate_listing(name, category, location, raw_products))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception:
        app.logger.exception("Local listing generation failed")
        return jsonify(error="Could not generate the listing. Please check your input."), 500


# ---- Errors ---------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_e):
    return _render_create(413, errors=[
        "The upload was too large. Each image must be 5 MB or smaller and the whole request under 110 MB."])


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
