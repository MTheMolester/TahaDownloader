import os
import re
import time
import logging
import requests
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

BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

app = Flask(__name__)

SESSIONS = {}

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


def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    if ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        send_message(chat_id, "🚫 شما مجاز به استفاده از این ربات نیستید.")
        return

    if text in ("/start", "/help"):
        send_message(chat_id, "👋 **به ربات طاها دانلودر خوش آمدید!**\n\nلینک ویدیوی مورد نظرت (یوتیوب، اینستاگرام، تیک‌تاک، توییتر، ردیت و...) رو برام بفرست تا با کیفیت دلخواه برات دانلود کنم.")
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

    s = SESSIONS.get(chat_id)
    if not s and not data.startswith("plat:"):
        edit_message(chat_id, message_id, "⚠️ این نشست منقضی شده است. لطفاً لینک را دوباره ارسال کنید.")
        return

    kind, _, value = data.partition(":")

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

        inputs = {
            "YT_URL": s["url"],
            "PLATFORM": s["platform"],
            "CHAT_ID": chat_id,
            "YT_QUALITY": s.get("quality") or "1080p",
            "YT_FORMAT": s["format"],
            "GET_SUBS": s.get("subs", "true"),
        }
        ok, info = trigger_workflow(inputs)
        SESSIONS.pop(chat_id, None)
        if ok:
            edit_message(chat_id, message_id, "🚀 **عملیات شروع شد!** فایل‌ها به زودی پس از پردازش برات ارسال میشن.")
        else:
            log.error("Workflow trigger failed: %s", info)
            edit_message(chat_id, message_id, "❌ خطایی در شروع دانلود رخ داد. لطفاً دوباره تلاش کنید.")


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


@app.route("/")
def health():
    return "Taha Downloader is running!", 200


@app.route("/setup")
def setup_webhook():
    base_url = os.environ.get("RENDER_EXTERNAL_URL") or request.url_root.rstrip("/")
    target = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    r = requests.post(f"{BALE_API}/setWebhook", json={"url": target}, timeout=20)
    return jsonify({"set_url": target, "bale_response": r.json() if r.content else {}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
