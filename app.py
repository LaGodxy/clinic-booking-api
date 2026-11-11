import hmac
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
KEY = os.environ["BOOKING_API_KEY"].encode()
DATABASE = os.environ.get("BOOKING_DATABASE", "booking.sqlite3")
ROOT = Path(__file__).resolve().parent


@contextmanager
def transaction(write=False):
    db = sqlite3.connect(DATABASE, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    finally:
        db.close()


with transaction(True) as db:
    db.execute("""CREATE TABLE IF NOT EXISTS slots (
        id TEXT PRIMARY KEY, provider TEXT NOT NULL, start TEXT NOT NULL,
        end TEXT NOT NULL, starts_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS bookings (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        slot_id TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS confirmed_slot
        ON bookings(slot_id) WHERE status = 'confirmed'""")
    db.execute("CREATE INDEX IF NOT EXISTS bookings_email ON bookings(email, seq)")
    db.execute("""CREATE TABLE IF NOT EXISTS retries (
        key TEXT PRIMARY KEY, body TEXT NOT NULL, status INTEGER NOT NULL,
        response TEXT NOT NULL)""")
    db.execute("CREATE TABLE IF NOT EXISTS attempts (email TEXT NOT NULL, at REAL NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS attempts_email ON attempts(email, at)")


def error(message, status):
    return jsonify(error=message), status


@app.before_request
def authenticate():
    if request.path != "/" and not hmac.compare_digest(
        request.headers.get("X-API-Key", "").encode(), KEY
    ):
        return error("unauthorized", 401)


@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(413)
@app.errorhandler(500)
def http_error(exc):
    return error(exc.name.lower(), exc.code)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.timestamp()


def booking(row):
    return {key: row[key] for key in ("id", "slot_id", "name", "email", "status", "created_at")}


@app.get("/")
def page():
    return send_from_directory(ROOT, "index.html")


@app.post("/reset")
def reset():
    body = request.get_json(silent=True)
    slots = body.get("slots") if isinstance(body, dict) else None
    if not isinstance(slots, list):
        return error("slots must be an array", 400)
    rows = []
    seen = set()
    try:
        for slot in slots:
            values = [slot[key] for key in ("id", "provider", "start", "end")]
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError
            start, end = map(timestamp, values[2:])
            if end <= start or values[0] in seen:
                raise ValueError
            seen.add(values[0])
            rows.append((*values, start))
    except (KeyError, TypeError, ValueError, OverflowError):
        return error("invalid slot", 400)
    with transaction(True) as db:
        for table in ("bookings", "slots", "retries", "attempts"):
            db.execute(f"DELETE FROM {table}")
        db.executemany("INSERT INTO slots VALUES (?, ?, ?, ?, ?)", rows)
    return jsonify(ok=True)


@app.get("/slots")
def slots():
    provider = request.args.get("provider")
    with transaction() as db:
        rows = db.execute("""SELECT s.id, s.provider, s.start, s.end,
            NOT EXISTS (SELECT 1 FROM bookings b WHERE b.slot_id = s.id
            AND b.status = 'confirmed') AS available
            FROM slots s WHERE (? IS NULL OR s.provider = ?) ORDER BY s.starts_at, s.id""",
            (provider, provider))
        return jsonify(slots=[dict(row, available=bool(row["available"])) for row in rows])


def field(body, key):
    value = body.get(key)
    return value.strip() if isinstance(value, str) else ""


def create_booking(db, body, email, now):
    slot_id = field(body, "slot_id")
    name = field(body, "name")
    if not slot_id or not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return 400, {"error": "slot_id, name and valid email are required"}
    slot = db.execute("SELECT starts_at FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if slot is None:
        return 404, {"error": "slot not found"}
    if slot["starts_at"] <= now:
        return 400, {"error": "slot has already started"}
    result = dict(id=str(uuid4()), slot_id=slot_id, name=name, email=email,
                  status="confirmed", created_at=datetime.fromtimestamp(now, timezone.utc)
                  .isoformat().replace("+00:00", "Z"))
    inserted = db.execute("""INSERT INTO bookings (id, slot_id, name, email, status, created_at)
        VALUES (:id, :slot_id, :name, :email, :status, :created_at)
        ON CONFLICT(slot_id) WHERE status = 'confirmed' DO NOTHING""", result)
    if inserted.rowcount == 0:
        return 409, {"error": "slot already booked"}
    return 201, result


@app.post("/bookings")
def book():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("expected a JSON object", 400)
    email = field(body, "email").lower()
    key = request.headers.get("Idempotency-Key")
    signature = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with transaction(True) as db:
        now = time.time()
        if key:
            prior = db.execute("SELECT * FROM retries WHERE key = ?", (key,)).fetchone()
            if prior:
                if prior["body"] != signature:
                    return error("idempotency key reused with a different body", 422)
                return jsonify(json.loads(prior["response"])), prior["status"]
        if email:
            db.execute("DELETE FROM attempts WHERE at <= ?", (now - 60,))
            count, earliest = db.execute(
                "SELECT count(*), min(at) FROM attempts WHERE email = ?", (email,)).fetchone()
            if count >= 5:
                return jsonify(error="too many booking attempts"), 429, {
                    "Retry-After": str(max(1, math.ceil(earliest + 60 - now)))}
            db.execute("INSERT INTO attempts VALUES (?, ?)", (email, now))
        status, result = create_booking(db, body, email, now)
        if key:
            db.execute("INSERT INTO retries VALUES (?, ?, ?, ?)",
                       (key, signature, status, json.dumps(result)))
        return jsonify(result), status


@app.get("/bookings")
def history():
    email = request.args.get("email", "").strip().lower()
    limit = request.args.get("limit", "10")
    cursor = request.args.get("cursor", "0")
    if not email or not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= 50:
        return error("email and a limit from 1 to 50 are required", 400)
    if not re.fullmatch(r"[0-9]{1,19}", cursor) or int(cursor) > 2**63 - 1:
        return error("invalid cursor", 400)
    size = int(limit)
    with transaction() as db:
        rows = db.execute("""SELECT * FROM bookings WHERE email = ? AND seq > ?
            ORDER BY seq LIMIT ?""", (email, int(cursor), size + 1)).fetchall()
        return jsonify(bookings=[booking(row) for row in rows[:size]],
                       next_cursor=str(rows[size - 1]["seq"]) if len(rows) > size else None)


@app.route("/bookings/<booking_id>", methods=["GET", "DELETE"])
def booking_detail(booking_id):
    cancel = request.method == "DELETE"
    with transaction(cancel) as db:
        row = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None:
            return error("booking not found", 404)
        result = booking(row)
        if cancel:
            if row["status"] == "cancelled":
                return error("already cancelled", 409)
            db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
            result["status"] = "cancelled"
        return jsonify(result)
