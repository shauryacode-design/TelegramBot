"""Coaching-institute enquiry bot: Telegram + FastAPI + Groq + SQLite.

Flow: Telegram -> POST /webhook -> (background) Groq -> reply via Telegram,
and any name/class/phone the user gives is saved as a lead in SQLite.
"""
import asyncio
import csv
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs full URLs at INFO level, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("enquiry-bot")

# ----------------------------------------------------------------------------
# Configuration (all from environment / .env)
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # letters, digits, _ and - only
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. your ngrok https URL
ADMIN_KEY = os.getenv("ADMIN_KEY", "")  # protects /leads pages
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")  # optional: owner gets a Telegram alert per lead
INSTITUTE_NAME = os.getenv("INSTITUTE_NAME", "Brightpath Academy")
INSTITUTE_FILE = Path(os.getenv("INSTITUTE_FILE", str(BASE_DIR / "institute_info.txt")))
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "bot.db"))
HISTORY_LIMIT = 8  # how many past messages the bot remembers per chat

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

FALLBACK_REPLY = (
    "Sorry, I'm having a small technical problem right now. "
    "Please try again in a minute, or call our office and we'll help you."
)
WELCOME = (
    f"Hello! Welcome to {INSTITUTE_NAME}. 😊\n"
    "You can ask me about courses, fees, batch timings, demo classes or admission. "
    "Hindi, English ya Hinglish, jaise aap chahein!"
)

# Shared HTTP client, created at startup.
http: httpx.AsyncClient | None = None


def load_institute_info() -> str:
    return INSTITUTE_FILE.read_text(encoding="utf-8")


def build_system_prompt() -> str:
    return f"""You are the friendly enquiry assistant of {INSTITUTE_NAME} on Telegram.
You talk to students and parents. Use ONLY the institute information below.

RULES
- If the answer is not in the information, say you are not sure and offer a call back from the office. Never invent fees, dates, discounts or results.
- Never promise marks, ranks or selection.
- Reply in the same language and style the user writes in (English, Hindi, or Hinglish). Keep replies short (under 80 words), warm, in plain text with no markdown.
- - Do not ask for contact details in the first two or three replies. Answer helpfully first. Ask for the student's name, class and phone number only when the user shows interest (asks about fees, admission, demo class, or says they want to join), and ask for ONE missing item at a time.
- You cannot book anything. If the user wants a demo class, collect their name, class and phone number, then say the office team will call to confirm the slot. Never say a class is booked or confirmed, and never name a specific date.
- Fill the JSON fields "name", "student_class" and "phone" ONLY with details the user has actually stated in this conversation (including earlier messages). Otherwise use null. Never guess.
- "reply" is the message to send to the user.

INSTITUTE INFORMATION
{load_institute_info()}
"""


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {"type": "STRING"},
        "name": {"type": "STRING", "nullable": True},
        "student_class": {"type": "STRING", "nullable": True},
        "phone": {"type": "STRING", "nullable": True},
    },
    "required": ["reply"],
}

# ----------------------------------------------------------------------------
# Database (SQLite, standard library only)
# ----------------------------------------------------------------------------


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS leads (
                chat_id INTEGER PRIMARY KEY,
                name TEXT,
                student_class TEXT,
                phone TEXT,
                tg_username TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def mark_update_seen(update_id: int) -> bool:
    """Return True if this update is new, False if we already handled it (Telegram retry)."""
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO processed_updates (update_id) VALUES (?)", (update_id,))
        return cur.rowcount == 1


def unmark_update(update_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM processed_updates WHERE update_id=?", (update_id,))


def get_history(chat_id: int) -> list[tuple[str, str]]:
    with db() as c:
        rows = c.execute(
            "SELECT role, text FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, HISTORY_LIMIT),
        ).fetchall()
    history = [(r["role"], r["text"]) for r in reversed(rows)]
    while history and history[0][0] != "user":  # Gemini history should start with a user turn
        history.pop(0)
    return history


def save_exchange(chat_id: int, user_text: str, bot_text: str) -> None:
    with db() as c:
        c.executemany(
            "INSERT INTO messages (chat_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            [(chat_id, "user", user_text, now()), (chat_id, "model", bot_text, now())],
        )


def clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if value.lower() in ("", "null", "none", "unknown", "n/a") else value


def normalize_phone(raw) -> str | None:
    """Return a valid 10-digit Indian mobile number or None."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def upsert_lead(chat_id: int, name, student_class, phone, tg_username: str | None):
    """Merge new details into the lead. Returns (lead_dict_or_None, became_complete)."""
    new = {
        "name": clean_text(name),
        "student_class": clean_text(student_class),
        "phone": normalize_phone(phone),
    }
    with db() as c:
        row = c.execute("SELECT * FROM leads WHERE chat_id=?", (chat_id,)).fetchone()
        old = dict(row) if row else {}
        merged = {k: new[k] or old.get(k) for k in new}
        if not any(merged.values()):
            return None, False
        was_complete = all(old.get(k) for k in new)
        is_complete = all(merged.values())
        c.execute(
            """
            INSERT INTO leads (chat_id, name, student_class, phone, tg_username, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                name=excluded.name,
                student_class=excluded.student_class,
                phone=excluded.phone,
                tg_username=COALESCE(excluded.tg_username, leads.tg_username),
                updated_at=excluded.updated_at
            """,
            (chat_id, merged["name"], merged["student_class"], merged["phone"], tg_username, now(), now()),
        )
    merged["tg_username"] = tg_username or old.get("tg_username")
    return merged, (is_complete and not was_complete)


def all_leads() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM leads ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# Telegram + Gemini helpers
# ----------------------------------------------------------------------------


async def send_telegram(chat_id, text: str) -> None:
    if http is None:
        raise RuntimeError("HTTP client is not initialized")
    r = await http.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]})
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed with HTTP {r.status_code}")


async def ask_gemini(history: list[tuple[str, str]], user_text: str) -> dict:
    """Call Groq (OpenAI-compatible) with retry on rate limits."""
    messages = [{"role": "system", "content": build_system_prompt()}]
    for role, text in history:
        messages.append({"role": role if role == "user" else "assistant", "content": text})
    messages.append({"role": "user", "content": user_text})

    body = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(3):
        r = await http.post(GROQ_URL, json=body, headers=headers)
        if r.status_code in (429, 500, 503):
            wait = 2 * (attempt + 1)
            log.warning("Groq returned %s, retrying in %ss", r.status_code, wait)
            await asyncio.sleep(wait)
            continue
        break

    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")

    data = r.json()
    text = data["choices"][0]["message"]["content"]
    return json.loads(text)


# ----------------------------------------------------------------------------
# Core logic: handle one Telegram update (runs in the background)
# ----------------------------------------------------------------------------


async def handle_update(update: dict) -> bool:
    try:
        msg = update.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            return True  # ignore edits, group chats, etc.

        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()

        if not text:
            await send_telegram(chat_id, "Please send your question as a text message. 🙂")
            return True
        if text.startswith("/start"):
            await send_telegram(chat_id, WELCOME)
            return True

        try:
            result = await ask_gemini(get_history(chat_id), text)
        except Exception:
            log.exception("Gemini call failed")
            await send_telegram(chat_id, FALLBACK_REPLY)
            return True

        reply = clean_text(result.get("reply")) or FALLBACK_REPLY
        save_exchange(chat_id, text, reply)

        username = msg.get("from", {}).get("username")
        lead, just_completed = upsert_lead(
            chat_id, result.get("name"), result.get("student_class"), result.get("phone"), username
        )

        await send_telegram(chat_id, reply)

        if just_completed and OWNER_CHAT_ID:
            alert = (
                "New enquiry!\n"
                f"Name: {lead['name']}\nClass: {lead['student_class']}\n"
                f"Phone: {lead['phone']}\nTelegram: @{lead['tg_username'] or 'n/a'}"
            )
            try:
                await send_telegram(OWNER_CHAT_ID, alert)
            except Exception:
                log.exception("Could not send owner lead alert")
        return True
    except Exception:
        log.exception("Unhandled error while processing update")
        return False


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and GROQ_API_KEY in your .env file")
    if PUBLIC_URL and not WEBHOOK_SECRET:
        raise RuntimeError("Set WEBHOOK_SECRET when PUBLIC_URL is configured")
    if not INSTITUTE_FILE.is_file():
        raise RuntimeError(f"Institute information file not found: {INSTITUTE_FILE}")
    init_db()
    http = httpx.AsyncClient(timeout=12)
    try:
        if PUBLIC_URL:
            payload = {"url": f"{PUBLIC_URL}/webhook", "allowed_updates": ["message"]}
            if WEBHOOK_SECRET:
                payload["secret_token"] = WEBHOOK_SECRET
            r = await http.post(f"{TG_API}/setWebhook", json=payload)
            result = r.json()
            if r.status_code != 200 or not result.get("ok"):
                raise RuntimeError(f"Telegram setWebhook failed with HTTP {r.status_code}")
            log.info("Telegram webhook registered")
        else:
            log.warning("PUBLIC_URL not set: webhook not registered with Telegram")
        yield
    finally:
        await http.aclose()
        http = None


app = FastAPI(title="Enquiry Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        update = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    update_id = update.get("update_id")
    if isinstance(update_id, int) and not mark_update_seen(update_id):
        return {"ok": True}  # duplicate delivery, already handled

    if not await handle_update(update):
        if isinstance(update_id, int):
            unmark_update(update_id)
        raise HTTPException(status_code=503, detail="Update processing failed")
    return {"ok": True}


def require_admin(key: str | None) -> None:
    if not ADMIN_KEY or not key or not hmac.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/leads", response_class=HTMLResponse)
async def leads_page(key: str | None = None):
    require_admin(key)
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(l.get(col) or ''))}</td>"
            for col in ("name", "student_class", "phone", "tg_username", "updated_at")
        )
        + "</tr>"
        for l in all_leads()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(INSTITUTE_NAME)} - Leads</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;max-width:900px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f4f4f4}}
</style></head><body>
<h2>{html.escape(INSTITUTE_NAME)} - Enquiries</h2>
<p><a href="/leads.csv?key={html.escape(key)}">Download CSV</a></p>
<table><tr><th>Name</th><th>Class</th><th>Phone</th><th>Telegram</th><th>Last updated (UTC)</th></tr>
{rows or '<tr><td colspan="5">No leads yet</td></tr>'}
</table></body></html>"""


def spreadsheet_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if re.match(r"^[\t\r\n ]*[=+\-@]", text) else text


@app.get("/leads.csv", response_class=PlainTextResponse)
async def leads_csv(key: str | None = None):
    require_admin(key)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "student_class", "phone", "tg_username", "created_at", "updated_at"])
    for l in all_leads():
        values = [l["name"], l["student_class"], l["phone"], l["tg_username"], l["created_at"], l["updated_at"]]
        writer.writerow([spreadsheet_safe(value) for value in values])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )
