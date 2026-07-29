import os
import re
import time
import uuid
import logging
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("taha-downloader")

# ── Config ───────────────────────────────────────────────────────────────────
BALE_TOKEN = os.environ["BALE_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_WORKFLOW_FILE = os.environ.get("GITHUB_WORKFLOW_FILE", "yt-bale.yml")
GITHUB_REF = os.environ.get("GITHUB_REF", "main")
ALLOWED_USERS = {u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()}

CALLBACK_SECRET = os.environ.get("CALLBACK_SECRET", "change-me-too")  # protects /callback/<secret>
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")                    # protects /setup ; empty = unprotected
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")            # your own chat id, for failure alerts

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "15"))         # jobs/user/day, 0 = unlimited
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "5"))          # re-download entries kept per user

BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

app = Flask(__name__)

# All in-memory — cleared on a Render restart. Fine for a free-tier deploy;
# any GitHub Actions run already in flight is unaffected by a bot restart.
SESSIONS = {}   # chat_id -> in-progress button flow state
JOBS = {}       # job_id  -> {chat_id, url, platform, format, quality, subs, created_at}
ACTIVE = {}     # chat_id -> job_id (one active job per user)
HISTORY = {}    # chat_id -> [ {url, platform, format, quality, subs, label}, ... ]
DAILY = {}      # chat_id -> {"date": "YYYY-MM-DD", "count": N}

PLATFORM_PATTERNS = [
    ("youtube", re.compile(r"(youtube\.com|youtu\.be)", re.I)),
    ("tiktok", re.compile(r"tiktok\.com", re.I)),
    ("instagram", re.compile(r"instagram\.com", re.I)),
    ("twitter", re.compile(r"(twitter\.com|x\.com)", re.I)),
    ("reddit", re.compile(r"reddit\.com", re.I)),
]
URL_RE = re.compile(r"https?://\S+")

QUALITIES = ["8K", "4K", "1080p", "720p", "480p", "360p", "240p"]


# ── Bale API Helpers ─────────────────────────────────────────────────────────
def api_call(method, payload):
    try:
        r = requests.post(f"{BALE_API}/{method}", json=payload, timeout=20)
        if not r.ok:
            log.warning("Bale API %s failed: %s", method, r.text)
        return r.json() if r.content else {}
    except Exception as e:
        log.warning("Bale API %s error: %s", method, e)
        return {}


def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api_call("sendMessage", payload)


def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api_call("editMessageText", payload)


def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return api_call("answerCallbackQuery", payload)


def btn(text, data):
    return {"text": text, "callback_data": data}


def alert_admin(text):
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, f"🛠 *Admin alert*\n{text}")


# ── GitHub Actions Trigger ───────────────────────────────────────────────────
def trigger_workflow(inputs):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"ref": GITHUB_REF, "inputs": inputs}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    return r.status_code == 204, r.text


# ── Rate limiting ────────────────────────────────────────────────────────────
def check_daily_limit(chat_id):
    if DAILY_LIMIT <= 0:
        return True
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = DAILY.get(chat_id)
    if not rec or rec["date"] != today:
        rec = {"date": today, "count": 0}
    if rec["count"] >= DAILY_LIMIT:
        DAILY[chat_id] = rec
        return False
    rec["count"] += 1
    DAILY[chat_id] = rec
    return True


# ── Job lifecycle ────────────────────────────────────────────────────────────
def start_job(chat_id, s):
    job_id = uuid.uuid4().hex[:12]
    inputs = {
        "YT_URL": s["url"],
        "PLATFORM": s["platform"],
        "CHAT_ID": chat_id,
        "YT_QUALITY": s.get("quality") or "1080p",
        "YT_FORMAT": s["format"],
        "GET_SUBS": s.get("subs", "true"),
        "JOB_ID": job_id,
    }
    ok, info = trigger_workflow(inputs)
    if not ok:
        log.error("Workflow trigger failed: %s", info)
        return False, None

    JOBS[job_id] = {
        "chat_id": chat_id,
        "created_at": time.time(),
        **{k: s.get(k) for k in ("url", "platform", "format", "quality", "subs")},
    }
    ACTIVE[chat_id] = job_id
    return True, job_id


def record_history(chat_id, s):
    entry = {
        "url": s["url"], "platform": s["platform"], "format": s["format"],
        "quality": s.get("quality"), "subs": s.get("subs", "true"),
        "label": f"{s['platform']} · {s['format']}" + (f" · {s['quality']}" if s.get("quality") else ""),
    }
    lst = HISTORY.setdefault(chat_id, [])
    lst.insert(0, entry)
    del lst[MAX_HISTORY:]


def finish_job(job_id, status, run_url):
    job = JOBS.pop(job_id, None)
    if not job:
        log.warning("Callback for unknown job_id=%s", job_id)
        return
    chat_id = job["chat_id"]
    if ACTIVE.get(chat_id) == job_id:
        ACTIVE.pop(chat_id, None)

    elapsed = int(time.time() - job["created_at"])
    if status == "success":
        send_message(chat_id, f"✅ *تمام شد!* ({elapsed} ثانیه طول کشید)\nفایل‌ها بالا ⬆️ ارسال شدن.")
        record_history(chat_id, job)
    else:
        send_message(chat_id, f"❌ *عملیات ناموفق بود* (بعد از {elapsed} ثانیه)\n[مشاهده گزارش خطا]({run_url})")
        alert_admin(f"Job `{job_id}` failed for chat `{chat_id}`.\nURL: {job.get('url')}\n[Run]({run_url})")


# ── Farsi Conversation Flow ──────────────────────────────────────────────────
def detect_platform(url):
    for name, pattern in PLATFORM_PATTERNS:
        if pattern.search(url):
            return name
    return None


def start_session(chat_id, url):
    platform = detect_platform(url)
    SESSIONS[chat_id] = {"url": url, "platform": platform}
    if platform:
        ask_format(chat_id)
    else:
        ask_platform(chat_id)


def ask_platform(chat_id):
    kb = [
        [btn("یوتیوب", "plat:youtube"), btn("تیک‌تاک", "plat:tiktok")],
        [btn("اینستاگرام", "plat:instagram"), btn("تویتر / X", "plat:twitter")],
        [btn("ردیت", "plat:reddit"), btn("سایر پلتفرم‌ها", "plat:other")],
    ]
    send_message(chat_id, "پلتفرم این لینک رو نتونستم تشخیص بدم 🤔\nلطفاً پلتفرم رو انتخاب کن:", kb)


def ask_format(chat_id, edit=None):
    kb = [[btn("🎬 ویدیو (MP4)", "fmt:mp4"), btn("🎧 فقط صوت (MP3)", "fmt:mp3")]]
    text = "چه فرمتی دوست داری دانلود بشه؟"
    if edit:
        edit_message(chat_id, edit, text, kb)
    else:
        send_message(chat_id, text, kb)


def ask_quality(chat_id, message_id):
    rows = [[btn(QUALITIES[i], f"q:{QUALITIES[i]}"), btn(QUALITIES[i + 1], f"q:{QUALITIES[i + 1]}")]
             for i in range(0, len(QUALITIES) - 1, 2)]
    if len(QUALITIES) % 2:
        rows.append([btn(QUALITIES[-1], f"q:{QUALITIES[-1]}")])
    edit_message(chat_id, message_id, "کیفیت مورد نظرت رو انتخاب کن:", rows)


def ask_subs(chat_id, message_id):
    kb = [[btn("✅ بله", "subs:true"), btn("🚫 خیر", "subs:false")]]
    edit_message(chat_id, message_id, "زیرنویس (فارسی / انگلیسی) چسبانده شود؟", kb)


def ask_confirm(chat_id, message_id):
    s = SESSIONS[chat_id]
    subs_status = "بله" if s.get("subs") == "true" else "خیر"
    lines = [
        "⚙️ *مشخصات سفارش دانلود:*",
        f"🔗 پلتفرم: `{s['platform'].upper()}`",
        f"📦 فرمت: `{s['format'].upper()}`",
    ]
    if s["format"] == "mp4":
        lines.append(f"🎚 کیفیت: `{s['quality']}`")
        lines.append(f"🔤 زیرنویس: `{subs_status}`")
    text = "\n".join(lines)
    kb = [[btn("🚀 شروع دانلود", "confirm:go"), btn("❌ انصراف", "confirm:cancel")]]
    edit_message(chat_id, message_id, text, kb)


def launch(chat_id, s, message_id):
    if chat_id in ACTIVE:
        edit_message(chat_id, message_id, "⏳ شما در حال حاضر یک عملیات فعال دارید. با /status وضعیتش رو ببین.")
        return
    if not check_daily_limit(chat_id):
        edit_message(chat_id, message_id, f"🚫 سقف روزانه ({DAILY_LIMIT} عملیات) پر شده. فردا دوباره امتحان کن.")
        return

    ok, job_id = start_job(chat_id, s)
    if ok:
        edit_message(chat_id, message_id, "🚀 **عملیات شروع شد!** فایل‌ها به زودی پس از پردازش برات ارسال میشن.\nبا /status می‌تونی وضعیت رو ببینی.")
    else:
        edit_message(chat_id, message_id, "❌ خطایی در شروع دانلود رخ داد. لطفاً دوباره تلاش کنید.")


def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    if ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        send_message(chat_id, "🚫 شما مجاز به استفاده از این ربات نیستید.")
        return

    if text in ("/start", "/help"):
        send_message(chat_id, "👋 **به ربات طاها دانلودر خوش آمدید!**\n\nلینک ویدیوی مورد نظرت (یوتیوب، اینستاگرام، تیک‌تاک، توییتر، ردیت و...) رو برام بفرست تا با کیفیت دلخواه برات دانلود کنم.\n\nدستورات: /status ، /history")
        return

    if text == "/status":
        job_id = ACTIVE.get(chat_id)
        if not job_id:
            send_message(chat_id, "در حال حاضر عملیات فعالی نداری. یک لینک بفرست تا شروع کنیم.")
        else:
            job = JOBS.get(job_id, {})
            elapsed = int(time.time() - job.get("created_at", time.time()))
            send_message(chat_id, f"⏳ عملیات در حال اجراست ({elapsed} ثانیه گذشته): `{job.get('url', '')}`")
        return

    if text == "/history":
        entries = HISTORY.get(chat_id, [])
        if not entries:
            send_message(chat_id, "هنوز هیچ دانلود تکمیل‌شده‌ای نداری.")
            return
        kb = [[btn(f"🔁 {e['label']}", f"hist:{i}")] for i, e in enumerate(entries)]
        send_message(chat_id, "کدوم رو دوباره اجرا کنم؟", kb)
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "❌ این یک لینک معتبر نیست. لطفاً یک لینک ویدیو بفرست.")
        return

    start_session(chat_id, match.group(0))


def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    answer_callback(cq["id"])

    if ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        return

    kind, _, value = data.partition(":")

    if kind == "hist":
        entries = HISTORY.get(chat_id, [])
        idx = int(value) if value.isdigit() else -1
        if idx < 0 or idx >= len(entries):
            edit_message(chat_id, message_id, "⚠️ این مورد دیگه در دسترس نیست.")
            return
        launch(chat_id, dict(entries[idx]), message_id)
        return

    s = SESSIONS.get(chat_id)
    if not s and not data.startswith("plat:"):
        edit_message(chat_id, message_id, "⚠️ این نشست منقضی شده است. لطفاً لینک را دوباره ارسال کنید.")
        return

    if kind == "plat":
        SESSIONS.setdefault(chat_id, {})["platform"] = value
        ask_format(chat_id, edit=message_id)

    elif kind == "fmt":
        s["format"] = value
        if value == "mp3":
            s["quality"] = None
            s["subs"] = "false"
            ask_confirm(chat_id, message_id)
        else:
            ask_quality(chat_id, message_id)

    elif kind == "q":
        s["quality"] = value
        ask_subs(chat_id, message_id)

    elif kind == "subs":
        s["subs"] = value
        ask_confirm(chat_id, message_id)

    elif kind == "confirm":
        if value == "cancel":
            SESSIONS.pop(chat_id, None)
            edit_message(chat_id, message_id, "❌ عملیات دانلود لغو شد.")
            return
        launch(chat_id, s, message_id)
        SESSIONS.pop(chat_id, None)


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception:
        log.exception("Error handling update")
    return jsonify({"ok": True})


@app.route(f"/callback/{CALLBACK_SECRET}", methods=["POST"])
def job_callback():
    """Called by the GitHub Actions workflow's final step when a job finishes."""
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id", "")
    status = data.get("status", "unknown")
    run_url = data.get("run_url", "")
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    try:
        finish_job(job_id, status, run_url)
    except Exception:
        log.exception("Error finishing job")
    return jsonify({"ok": True})


@app.route("/")
def health():
    return "Taha Downloader is running!", 200


@app.route("/setup")
def setup_webhook():
    """Hit once after deploying to register the webhook.
    Protected by ADMIN_KEY if set: /setup?key=your-admin-key"""
    if ADMIN_KEY and request.args.get("key") != ADMIN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    base_url = os.environ.get("RENDER_EXTERNAL_URL") or request.url_root.rstrip("/")
    target = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    r = requests.post(f"{BALE_API}/setWebhook", json={"url": target}, timeout=20)
    return jsonify({"set_url": target, "bale_response": r.json() if r.content else {}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
