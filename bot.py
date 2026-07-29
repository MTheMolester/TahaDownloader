import os
import re
import time
import json
import logging
import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("taha-downloader")

# ── Environment Variables ──────────────────────────────────────────────────
BALE_TOKEN = os.environ["BALE_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_WORKFLOW_FILE = os.environ.get("GITHUB_WORKFLOW_FILE", "yt-bale.yml")
GITHUB_REF = os.environ.get("GITHUB_REF", "main")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# The Force Join variable!
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL") 
ALLOWED_USERS = {u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()}

BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

app = Flask(__name__)
SESSIONS = {}

URL_RE = re.compile(r"https?://\S+")
QUALITIES = ["8K", "4K", "1080p", "720p", "480p", "360p", "240p"]

# ── Bale API Helpers ────────────────────────────────────────────────────────
def api_call(method, payload):
    try:
        r = requests.post(f"{BALE_API}/{method}", json=payload, timeout=20)
        return r.json() if r.content else {}
    except Exception: return {}

def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api_call("sendMessage", payload)

def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard is not None: payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api_call("editMessageText", payload)

def send_photo(chat_id, photo_url, caption, keyboard=None):
    try:
        img_data = requests.get(photo_url).content
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if keyboard: payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        r = requests.post(f"{BALE_API}/sendPhoto", data=payload, files={"photo": ("thumb.jpg", img_data)}, verify=False)
        if not r.ok: raise Exception("Failed to send photo")
    except Exception:
        send_message(chat_id, f"[🖼 کاور]({photo_url})\n\n{caption}", keyboard)

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text: 
        payload["text"] = text
        payload["show_alert"] = show_alert
    return api_call("answerCallbackQuery", payload)

def btn(text, data): return {"text": text, "callback_data": data}

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


# ── Channel Membership Checker ──────────────────────────────────────────────
def check_membership(user_id):
    if not REQUIRED_CHANNEL:
        return True # Skips if you didn't add the env variable
        
    payload = {"chat_id": REQUIRED_CHANNEL, "user_id": user_id}
    try:
        r = requests.post(f"{BALE_API}/getChatMember", json=payload, timeout=5)
        data = r.json()
        if data.get("ok"):
            status = data["result"]["status"]
            if status in ["member", "administrator", "creator"]:
                return True
    except Exception:
        pass
    return False

def force_join_message(chat_id, message_id=None):
    channel_link = f"https://ble.ir/{REQUIRED_CHANNEL.replace('@', '')}"
    text = "⚠️ **برای استفاده از ربات، ابتدا باید در کانال ما عضو شوید!**\n\nپس از عضویت در کانال، روی دکمه «بررسی عضویت» کلیک کنید."
    kb = [
        [{"text": "📣 عضویت در کانال", "url": channel_link}],
        [btn("🔄 بررسی عضویت", "main:check_join")]
    ]
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)


# ── YouTube API Helpers ─────────────────────────────────────────────────────
def extract_yt_id(url):
    m = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return m.group(1) if m else None

def parse_pt_duration(duration_str):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not m: return "0:00"
    h, m, s = [int(x) if x else 0 for x in m.groups()]
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_subs(count_str):
    try:
        c = int(count_str)
        if c >= 1000000: return f"{c/1000000:.1f}M"
        if c >= 1000: return f"{c/1000:.1f}K"
        return str(c)
    except: return count_str

def yt_api(endpoint, params):
    params["key"] = YOUTUBE_API_KEY
    try: return requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params, timeout=10).json()
    except: return {}

# ── Interactive Menus ───────────────────────────────────────────────────────
def send_main_menu(chat_id, message_id=None):
    SESSIONS[chat_id] = {"state": "MAIN_MENU", "extras": {"subs": True, "comments": True, "description": True, "thumbnail": True}}
    kb = [
        [btn("🔴 یوتیوب (YouTube)", "main:youtube")],
        [btn("🎵 تیک‌تاک", "main:tiktok"), btn("📸 اینستاگرام", "main:instagram")],
        [btn("🐦 توییتر / X", "main:twitter"), btn("🤖 ردیت", "main:reddit")],
        [btn("🌐 سایر لینک‌ها (Any Video)", "main:other")]
    ]
    text = "👋 **به ربات طاها دانلودر خوش آمدید!**\nلطفاً پلتفرم مورد نظر خود را انتخاب کنید:"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def send_yt_menu(chat_id, message_id=None):
    SESSIONS[chat_id]["platform"] = "youtube"
    kb = [
        [btn("🔍 جستجوی ویدیو (با کلمه کلیدی)", "ytm:search_vid")],
        [btn("👤 جستجوی کانال", "ytm:search_chan")],
        [btn("🔗 دانلود با لینک مستقیم", "ytm:direct")],
        [btn("🔙 بازگشت به منوی اصلی", "main:back")]
    ]
    text = "🔴 **منوی یوتیوب**\nلطفاً روش جستجو یا دانلود را انتخاب کنید:"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def fetch_preview(chat_id, video_id):
    data = yt_api("videos", {"part": "snippet,contentDetails", "id": video_id})
    if not data or not data.get("items"):
        send_message(chat_id, "❌ خطا در برقراری ارتباط با یوتیوب یا ویدیو یافت نشد.")
        return
        
    item = data["items"][0]
    title = item["snippet"]["title"]
    channel = item["snippet"]["channelTitle"]
    dur = parse_pt_duration(item["contentDetails"]["duration"])
    
    thumbnails = item["snippet"]["thumbnails"]
    thumb = thumbnails.get("maxres", thumbnails.get("high", thumbnails.get("default")))["url"]

    SESSIONS[chat_id]["url"] = f"https://youtu.be/{video_id}"
    caption = f"🎬 **عنوان:** {title}\n👤 **کانال:** {channel}\n⏱ **زمان:** {dur}"
    kb = [[btn("✅ تایید و انتخاب کیفیت", "preview:confirm"), btn("❌ انصراف", "main:back")]]
    
    send_photo(chat_id, thumb, caption, kb)

def yt_search_videos(chat_id, query):
    data = yt_api("search", {"part": "snippet", "type": "video", "q": query, "maxResults": 5})
    if not data or not data.get("items"):
        send_message(chat_id, "❌ هیچ ویدیویی پیدا نشد.")
        return
        
    kb = []
    for item in data["items"]:
        vid = item["id"]["videoId"]
        title = item["snippet"]["title"][:35] + "..." if len(item["snippet"]["title"]) > 35 else item["snippet"]["title"]
        kb.append([btn(f"🎬 {title}", f"vid:{vid}")])
    kb.append([btn("🔙 بازگشت", "main:back")])
    send_message(chat_id, f"🔍 نتایج جستجو برای: `{query}`", kb)

def yt_search_channels(chat_id, query):
    data = yt_api("search", {"part": "snippet", "type": "channel", "q": query, "maxResults": 5})
    if not data or not data.get("items"):
        send_message(chat_id, "❌ هیچ کانالی پیدا نشد.")
        return
        
    c_ids = ",".join([i["snippet"]["channelId"] for i in data["items"]])
    stats_data = yt_api("channels", {"part": "statistics", "id": c_ids})
    stats = {i["id"]: i["statistics"].get("subscriberCount", "0") for i in stats_data.get("items", [])}
    
    kb = []
    for item in data["items"]:
        cid = item["snippet"]["channelId"]
        title = item["snippet"]["title"]
        subs = format_subs(stats.get(cid, "0"))
        kb.append([btn(f"👤 {title} ({subs} Subs)", f"chan:{cid}")])
    kb.append([btn("🔙 بازگشت", "main:back")])
    send_message(chat_id, f"👤 نتایج جستجوی کانال برای: `{query}`", kb)

def yt_channel_videos(chat_id, channel_id):
    data = yt_api("search", {"part": "snippet", "type": "video", "channelId": channel_id, "order": "date", "maxResults": 5})
    if not data or not data.get("items"):
        send_message(chat_id, "❌ این کانال ویدیویی ندارد.")
        return
        
    kb = []
    for item in data["items"]:
        vid = item["id"]["videoId"]
        title = item["snippet"]["title"][:40] + "..." if len(item["snippet"]["title"]) > 40 else item["snippet"]["title"]
        kb.append([btn(f"🎬 {title}", f"vid:{vid}")])
    kb.append([btn("🔙 بازگشت", "main:back")])
    send_message(chat_id, "📺 جدیدترین ویدیوهای این کانال:", kb)

def ask_format(chat_id, message_id=None):
    kb = [[btn("🎬 ویدیو (MP4)", "fmt:mp4"), btn("🎧 فقط صوت (MP3)", "fmt:mp3")]]
    text = "چه فرمتی دوست داری دانلود بشه؟"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def ask_quality(chat_id, message_id):
    rows = [[btn(QUALITIES[i], f"q:{QUALITIES[i]}"), btn(QUALITIES[i + 1], f"q:{QUALITIES[i + 1]}")] for i in range(0, len(QUALITIES) - 1, 2)]
    if len(QUALITIES) % 2: rows.append([btn(QUALITIES[-1], f"q:{QUALITIES[-1]}")])
    edit_message(chat_id, message_id, "کیفیت مورد نظرت رو انتخاب کن:", rows)

def ask_extras(chat_id, message_id):
    s = SESSIONS.get(chat_id)
    if not s: return
    e = s["extras"]
    def check(key): return "✅" if e.get(key) else "❌"

    kb = []
    if s.get("format") == "mp4":
        kb.append([btn(f"{check('subs')} زیرنویس (Subtitles)", "toggle:subs")])
    kb.append([
        btn(f"{check('comments')} کامنت‌ها", "toggle:comments"),
        btn(f"{check('description')} توضیحات", "toggle:description")
    ])
    kb.append([btn(f"{check('thumbnail')} (thumbnail) کاور", "toggle:thumbnail")])
    kb.append([btn("🚀 تایید و مرحله بعد", "confirm:extras")])
    edit_message(chat_id, message_id, "⚙️ **تنظیمات جانبی:**\nبا کلیک روی هر گزینه می‌توانید آن را فعال (✅) یا غیرفعال (❌) کنید. سپس تایید کنید:", kb)

def ask_confirm(chat_id, message_id):
    s = SESSIONS[chat_id]
    e = s["extras"]
    lines = [
        "📋 *مرور نهایی سفارش:*",
        f"🔗 پلتفرم: `{s['platform'].upper()}`",
        f"📦 فرمت: `{s['format'].upper()}`",
    ]
    if s["format"] == "mp4":
        lines.append(f"🎚 کیفیت: `{s['quality']}`")
        lines.append(f"🔤 زیرنویس: `{'دارد' if e['subs'] else 'ندارد'}`")
        
    lines.append(f"💬 کامنت‌ها: `{'دارد' if e['comments'] else 'ندارد'}`")
    lines.append(f"📝 توضیحات: `{'دارد' if e['description'] else 'ندارد'}`")
    lines.append(f"🖼 کاور: `{'دارد' if e['thumbnail'] else 'ندارد'}`")
    
    kb = [[btn("🚀 شروع دانلود", "confirm:go"), btn("❌ انصراف", "main:back")]]
    edit_message(chat_id, message_id, "\n".join(lines), kb)


# ── Core Handlers ───────────────────────────────────────────────────────────
def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    if ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        send_message(chat_id, "🚫 شما مجاز به استفاده از این ربات نیستید.")
        return

    # 1. FORCE JOIN CHECK FOR TEXT MESSAGES
    if not check_membership(chat_id):
        force_join_message(chat_id)
        return

    if text in ("/start", "/help"):
        send_main_menu(chat_id)
        return

    s = SESSIONS.get(chat_id)
    if not s:
        send_main_menu(chat_id)
        return

    state = s.get("state")
    
    if state == "WAITING_OTHER_LINK":
        match = URL_RE.search(text)
        if match:
            s["url"] = match.group(0)
            ask_format(chat_id)
        else: send_message(chat_id, "❌ لینک نامعتبر است. لطفاً یک لینک معتبر بفرستید.")
            
    elif state == "WAITING_YT_LINK":
        vid_id = extract_yt_id(text)
        if vid_id: fetch_preview(chat_id, vid_id)
        else: send_message(chat_id, "❌ لینک یوتیوب نامعتبر است. مجدداً تلاش کنید.")
            
    elif state == "WAITING_YT_SEARCH":
        send_message(chat_id, "⏳ در حال جستجو...")
        yt_search_videos(chat_id, text)
        
    elif state == "WAITING_YT_CHANNEL":
        send_message(chat_id, "⏳ در حال جستجوی کانال...")
        yt_search_channels(chat_id, text)
        
    else:
        match = URL_RE.search(text)
        if match:
            url = match.group(0)
            if "youtu" in url:
                vid_id = extract_yt_id(url)
                if vid_id: 
                    SESSIONS[chat_id] = {"platform": "youtube", "extras": {"subs": True, "comments": True, "description": True, "thumbnail": True}}
                    fetch_preview(chat_id, vid_id)
            else:
                SESSIONS[chat_id] = {"platform": "other", "url": url, "extras": {"subs": False, "comments": False, "description": False, "thumbnail": False}}
                ask_format(chat_id)
        else:
            send_main_menu(chat_id)

def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    
    # Check the "Verify Membership" button directly
    if data == "main:check_join":
        if check_membership(chat_id):
            answer_callback(cq["id"], "✅ عضویت شما تایید شد!", show_alert=True)
            send_main_menu(chat_id, message_id)
        else:
            answer_callback(cq["id"], "❌ هنوز در کانال عضو نشده‌اید!", show_alert=True)
        return

    answer_callback(cq["id"])

    # 2. FORCE JOIN CHECK FOR ALL OTHER BUTTON CLICKS
    if not check_membership(chat_id):
        force_join_message(chat_id, message_id)
        return

    s = SESSIONS.get(chat_id)
    if not s and not data.startswith("main:"):
        send_main_menu(chat_id, message_id)
        return

    kind, _, value = data.partition(":")

    if kind == "main":
        if value == "back": send_main_menu(chat_id, message_id)
        elif value == "youtube": send_yt_menu(chat_id, message_id)
        else:
            SESSIONS.setdefault(chat_id, {})["platform"] = value
            SESSIONS[chat_id]["state"] = "WAITING_OTHER_LINK"
            SESSIONS[chat_id]["extras"] = {"subs": True, "comments": True, "description": True, "thumbnail": True}
            edit_message(chat_id, message_id, f"شما `{value.upper()}` را انتخاب کردید.\n\n🔗 لطفاً لینک ویدیوی خود را ارسال کنید:")
            
    elif kind == "ytm":
        if value == "direct":
            SESSIONS[chat_id]["state"] = "WAITING_YT_LINK"
            edit_message(chat_id, message_id, "🔗 لطفاً لینک ویدیوی یوتیوب را بفرستید:")
        elif value == "search_vid":
            SESSIONS[chat_id]["state"] = "WAITING_YT_SEARCH"
            edit_message(chat_id, message_id, "🔍 کلمه یا جمله مورد نظر خود را برای جستجو در یوتیوب بفرستید:")
        elif value == "search_chan":
            SESSIONS[chat_id]["state"] = "WAITING_YT_CHANNEL"
            edit_message(chat_id, message_id, "👤 نام کانال یوتیوب مورد نظر را بفرستید:")
            
    elif kind == "vid":
        fetch_preview(chat_id, value)
        
    elif kind == "chan":
        yt_channel_videos(chat_id, value)
        
    elif kind == "preview":
        if value == "confirm": ask_format(chat_id) 

    elif kind == "fmt":
        s["format"] = value
        if value == "mp3":
            s["quality"] = None
            s["extras"]["subs"] = False
            ask_extras(chat_id, message_id)
        else: ask_quality(chat_id, message_id)
        
    elif kind == "q":
        s["quality"] = value
        ask_extras(chat_id, message_id)
        
    elif kind == "toggle":
        s["extras"][value] = not s["extras"][value]
        ask_extras(chat_id, message_id)
        
    elif kind == "confirm":
        if value == "extras":
            ask_confirm(chat_id, message_id)
            return
        if value == "go":
            e = s["extras"]
            inputs = {
                "YT_URL": s["url"],
                "PLATFORM": s["platform"],
                "CHAT_ID": chat_id,
                "YT_QUALITY": s.get("quality") or "1080p",
                "YT_FORMAT": s["format"],
                "GET_SUBS": "true" if e.get("subs") else "false",
                "GET_COMMENTS": "true" if e.get("comments") else "false",
                "GET_DESC": "true" if e.get("description") else "false",
                "GET_THUMBNAIL": "true" if e.get("thumbnail") else "false",
                "MESSAGE_ID": str(message_id)
            }
            ok, info = trigger_workflow(inputs)
            SESSIONS.pop(chat_id, None)
            if ok: edit_message(chat_id, message_id, "⏳ **در حال ارسال دستور به سرور...**")
            else: edit_message(chat_id, message_id, "❌ خطایی در شروع دانلود رخ داد.")

@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "callback_query" in update: handle_callback(update["callback_query"])
        elif "message" in update: handle_message(update["message"])
    except Exception: pass
    return jsonify({"ok": True})

@app.route("/")
def health(): return "Taha Downloader Complete V4 is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
