import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
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
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL") 

UPSTASH_URL = os.environ.get("UPSTASH_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "@YourAdminID")

BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
app = Flask(__name__)
SESSIONS = {}

URL_RE = re.compile(r"https?://\S+")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$") 
QUALITIES = ["8K", "4K", "1080p", "720p", "480p", "360p", "240p"]

# ── Database & CRM Logic ──────────────────────────────────────────────────
def db_cmd(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return None
    try:
        r = requests.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, json=list(args), timeout=5)
        return r.json().get("result")
    except Exception: return None

def is_approved(user_id):
    if str(user_id) == ADMIN_ID: return True
    return db_cmd("SISMEMBER", "approved_users", str(user_id)) == 1

def approve_user(user_id): db_cmd("SADD", "approved_users", str(user_id))
def revoke_user(user_id): db_cmd("SREM", "approved_users", str(user_id))
def get_all_users(): return db_cmd("SMEMBERS", "approved_users") or []

def save_user_info(user_id, name, username):
    data = json.dumps({"name": name, "username": username}, ensure_ascii=False)
    db_cmd("SET", f"uinfo:{user_id}", data)

def get_user_info(user_id):
    res = db_cmd("GET", f"uinfo:{user_id}")
    if res:
        try: return json.loads(res)
        except: pass
    return {"name": str(user_id), "username": ""}

def log_history(user_id, action_type, title, channel, thumb, desc, details):
    ir_time = (datetime.utcnow() + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d %H:%M")
    entry = {
        "type": action_type, "title": title, "channel": channel,
        "thumb": thumb, "desc": desc, "details": details, "time": ir_time
    }
    db_cmd("LPUSH", f"hist:{user_id}", json.dumps(entry, ensure_ascii=False))
    db_cmd("LTRIM", f"hist:{user_id}", "0", "19") 

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

def delete_message(chat_id, message_id):
    return api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def send_photo(chat_id, photo_url, caption, keyboard=None):
    try:
        img_data = requests.get(photo_url).content
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if keyboard: payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        r = requests.post(f"{BALE_API}/sendPhoto", data=payload, files={"photo": ("thumb.jpg", img_data)}, verify=False)
        if not r.ok: raise Exception("Failed")
    except Exception: send_message(chat_id, f"[🖼 کاور]({photo_url})\n\n{caption}", keyboard)

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text: payload.update({"text": text, "show_alert": show_alert})
    return api_call("answerCallbackQuery", payload)

def btn(text, data): return {"text": text, "callback_data": data}

def trigger_workflow(inputs):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    r = requests.post(url, headers=headers, json={"ref": GITHUB_REF, "inputs": inputs}, timeout=20)
    return r.status_code == 204, r.text

# ── Security Blockers ───────────────────────────────────────────────────────
def check_membership(user_id):
    if not REQUIRED_CHANNEL: return True 
    try:
        r = requests.post(f"{BALE_API}/getChatMember", json={"chat_id": REQUIRED_CHANNEL, "user_id": user_id}, timeout=5).json()
        if r.get("ok") and r["result"]["status"] in ["member", "administrator", "creator"]: return True
    except Exception: pass
    return False

def force_join_message(chat_id, message_id=None):
    channel_link = f"https://ble.ir/{REQUIRED_CHANNEL.replace('@', '')}"
    text = "⚠️ **برای استفاده از ربات، ابتدا باید در کانال ما عضو شوید!**\n\nپس از عضویت، روی «بررسی عضویت» کلیک کنید."
    kb = [[{"text": "📣 عضویت در کانال", "url": channel_link}], [btn("🔄 بررسی عضویت", "main:check_join")]]
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def access_denied_message(chat_id, user_id, message_id=None):
    text = f"⛔️ **دسترسی شما فعال نیست!**\n\nشما برای استفاده از این ربات نیاز به مجوز دارید.\n🆔 **شناسه عددی شما:** `{user_id}`\n\nلطفاً این شناسه را برای مدیر ارسال کنید تا دسترسی شما فعال شود:\n👤 **ارتباط با مدیر:** {ADMIN_USERNAME}"
    if message_id: edit_message(chat_id, message_id, text)
    else: send_message(chat_id, text)

# ── 🔍 Search Engine 2.0 Logic ──────────────────────────────────────────────
def yt_api(endpoint, params):
    params["key"] = YOUTUBE_API_KEY
    try: return requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params, timeout=10).json()
    except: return {}

def render_search_results(chat_id, message_id, query=None, channel_id=None, page_token=None, page_num=1):
    params = {"part": "snippet", "type": "video", "maxResults": 5}
    
    if query: params["q"] = query
    if channel_id: 
        params["channelId"] = channel_id
        params["order"] = "date"
    if page_token: params["pageToken"] = page_token
    
    data = yt_api("search", params)
    if not data or not data.get("items"):
        text = "❌ هیچ ویدیویی پیدا نشد."
        if message_id: return edit_message(chat_id, message_id, text, [[btn("🔙 بازگشت", "main:back")]])
        else: return send_message(chat_id, text, [[btn("🔙 بازگشت", "main:back")]])

    items = data["items"]
    
    # Save video IDs so the numbered buttons know what to click
    SESSIONS.setdefault(chat_id, {})["search_results"] = [item["id"]["videoId"] for item in items]
    
    header = f"🔍 **نتایج جستجو برای:** `{query}`" if query else "📺 **جدیدترین ویدیوهای کانال:**"
    lines = [header, f"📄 **صفحه:** {page_num}\n"]
    
    numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    row_numbers = []
    
    for i, item in enumerate(items):
        title = item["snippet"]["title"]
        channel = item["snippet"]["channelTitle"]
        pub_date = item["snippet"]["publishTime"][:10]
        
        lines.append(f"{numbers[i]} **{title}**")
        lines.append(f"👤 {channel} | 📅 {pub_date}\n")
        row_numbers.append(btn(str(i+1), f"res:{i}"))
        
    kb = [row_numbers]
    nav_row = []
    
    # Generate infinite pagination tokens
    if data.get("nextPageToken"):
        SESSIONS[chat_id]["next_token"] = data["nextPageToken"]
        nav_row.append(btn("⬅️ بعدی", f"page:next:{page_num+1}"))
    if data.get("prevPageToken"):
        SESSIONS[chat_id]["prev_token"] = data["prevPageToken"]
        nav_row.append(btn("➡️ قبلی", f"page:prev:{page_num-1}"))
        
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 بازگشت به منو", "main:back")])
    
    text = "\n".join(lines)
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def render_channel_search(chat_id, message_id, query, page_token=None, page_num=1):
    params = {"part": "snippet", "type": "channel", "q": query, "maxResults": 5}
    if page_token: params["pageToken"] = page_token
    
    data = yt_api("search", params)
    if not data or not data.get("items"):
        text = "❌ هیچ کانالی پیدا نشد."
        if message_id: return edit_message(chat_id, message_id, text, [[btn("🔙 بازگشت", "main:back")]])
        else: return send_message(chat_id, text, [[btn("🔙 بازگشت", "main:back")]])

    items = data["items"]
    SESSIONS.setdefault(chat_id, {})["chan_results"] = [item["snippet"]["channelId"] for item in items]
    
    lines = [f"👤 **نتایج جستجوی کانال برای:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    row_numbers = []
    
    for i, item in enumerate(items):
        title = item["snippet"]["title"]
        desc = item["snippet"].get("description", "")[:60] + "..."
        lines.append(f"{numbers[i]} **{title}**")
        lines.append(f"📝 {desc}\n")
        row_numbers.append(btn(str(i+1), f"chan_res:{i}"))
        
    kb = [row_numbers]
    nav_row = []
    
    if data.get("nextPageToken"):
        SESSIONS[chat_id]["chan_next"] = data["nextPageToken"]
        nav_row.append(btn("⬅️ بعدی", f"cpage:next:{page_num+1}"))
    if data.get("prevPageToken"):
        SESSIONS[chat_id]["chan_prev"] = data["prevPageToken"]
        nav_row.append(btn("➡️ قبلی", f"cpage:prev:{page_num-1}"))
        
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 بازگشت به منو", "main:back")])
    
    text = "\n".join(lines)
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

# ── Interactive Menus ───────────────────────────────────────────────────────
def send_admin_menu(chat_id, message_id=None):
    SESSIONS[chat_id] = {"state": "ADMIN_MENU"}
    kb = [
        [btn("➕ افزودن کاربر", "admin:add"), btn("➖ حذف کاربر", "admin:rev")],
        [btn("👥 لیست کاربران مجاز", "admin:list")],
        [btn("🔙 خروج از پنل", "main:back")]
    ]
    text = "👑 **پنل مدیریت پیشرفته CRM**\nاز گزینه‌های زیر برای مدیریت کاربران و بررسی فعالیت‌ها استفاده کنید:"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def send_main_menu(chat_id, message_id=None):
    SESSIONS[chat_id] = {"state": "MAIN_MENU", "extras": {"subs": True, "comments": True, "description": True, "thumbnail": True, "trim_start": None, "trim_end": None}}
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

def parse_pt_duration(duration_str):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not m: return "0:00"
    h, m, s = [int(x) if x else 0 for x in m.groups()]
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def extract_yt_id(url):
    m = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return m.group(1) if m else None

def fetch_preview(chat_id, video_id, message_id=None):
    data = yt_api("videos", {"part": "snippet,contentDetails", "id": video_id})
    if not data or not data.get("items"): return send_message(chat_id, "❌ خطا در یافتن ویدیو.")
    item = data["items"][0]
    title, channel = item["snippet"]["title"], item["snippet"]["channelTitle"]
    dur = parse_pt_duration(item["contentDetails"]["duration"])
    desc = item["snippet"].get("description", "")[:150] + "..."
    thumb = item["snippet"]["thumbnails"].get("maxres", item["snippet"]["thumbnails"].get("high", item["snippet"]["thumbnails"].get("default")))["url"]
    
    SESSIONS[chat_id]["url"] = f"https://youtu.be/{video_id}"
    SESSIONS[chat_id]["vid_info"] = {"title": title, "channel": channel, "thumb": thumb, "desc": desc}
    
    kb = [[btn("✅ تایید و انتخاب کیفیت", "preview:confirm"), btn("❌ انصراف", "main:back")]]
    
    if message_id: delete_message(chat_id, message_id)
    send_photo(chat_id, thumb, f"🎬 **عنوان:** {title}\n👤 **کانال:** {channel}\n⏱ **زمان:** {dur}", kb)

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
    trim_lbl = f"✂️ برش ({e['trim_start']} تا {e['trim_end']})" if e.get("trim_start") else "✂️ برش ویدیو (کلیک کنید)"
    kb.append([btn(trim_lbl, "trim:toggle")])
    if s.get("format") == "mp4": kb.append([btn(f"{check('subs')} زیرنویس (Subtitles)", "toggle:subs")])
    kb.append([btn(f"{check('comments')} کامنت‌ها", "toggle:comments"), btn(f"{check('description')} توضیحات", "toggle:description")])
    kb.append([btn(f"{check('thumbnail')} (thumbnail) کاور", "toggle:thumbnail")])
    kb.append([btn("🚀 تایید و مرحله بعد", "confirm:extras")])
    
    text = "⚙️ **تنظیمات جانبی:**\nبا کلیک روی هر گزینه می‌توانید آن را تغییر دهید:"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def ask_confirm(chat_id, message_id):
    s = SESSIONS[chat_id]
    e = s["extras"]
    lines = ["📋 *مرور نهایی سفارش:*", f"🔗 پلتفرم: `{s['platform'].upper()}`", f"📦 فرمت: `{s['format'].upper()}`"]
    if s["format"] == "mp4":
        lines.append(f"🎚 کیفیت: `{s['quality']}`")
        lines.append(f"🔤 زیرنویس: `{'دارد' if e['subs'] else 'ندارد'}`")
        
    trim_status = f"از {e['trim_start']} تا {e['trim_end']}" if e.get("trim_start") else "ندارد (کل ویدیو)"
    lines.append(f"✂️ برش: `{trim_status}`")
    lines.append(f"💬 کامنت‌ها: `{'دارد' if e['comments'] else 'ندارد'}`")
    lines.append(f"📝 توضیحات: `{'دارد' if e['description'] else 'ندارد'}`")
    lines.append(f"🖼 کاور: `{'دارد' if e['thumbnail'] else 'ندارد'}`")
    
    kb = [[btn("🚀 شروع دانلود", "confirm:go"), btn("❌ انصراف", "main:back")]]
    edit_message(chat_id, message_id, "\n".join(lines), kb)

# ── Core Handlers ───────────────────────────────────────────────────────────
def handle_message(msg):
    if msg.get("chat", {}).get("type", "") != "private": return 
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg.get("from", {}).get("id", chat_id))
    text = (msg.get("text") or "").strip()

    first_name = msg.get("from", {}).get("first_name", "کاربر")
    username = msg.get("from", {}).get("username", "")
    save_user_info(user_id, first_name, username)

    if not check_membership(user_id): return force_join_message(chat_id)
        
    if text == "/admin":
        if user_id == ADMIN_ID: send_admin_menu(chat_id)
        return

    if not is_approved(user_id): return access_denied_message(chat_id, user_id)

    if text in ("/start", "/help"): return send_main_menu(chat_id)

    s = SESSIONS.get(chat_id)
    if not s: return send_main_menu(chat_id)

    state = s.get("state")
    
    if state == "WAITING_TRIM_START":
        if TIME_RE.match(text):
            s["extras"]["trim_start"] = text
            s["state"] = "WAITING_TRIM_END"
            send_message(chat_id, "✅ **حالا زمان پایان را بفرستید.**\nمثال: `02:45`")
        else: send_message(chat_id, "❌ فرمت اشتباه است. لطفاً مثل `01:30` بفرستید:")
            
    elif state == "WAITING_TRIM_END":
        if TIME_RE.match(text):
            s["extras"]["trim_end"] = text
            s["state"] = "EXTRAS_MENU"
            send_message(chat_id, f"✅ برش تنظیم شد: از {s['extras']['trim_start']} تا {text}")
            ask_extras(chat_id, None)
        else: send_message(chat_id, "❌ فرمت اشتباه است. لطفاً مثل `02:45` بفرستید:")
            
    elif state == "WAITING_ADMIN_ADD":
        approve_user(text)
        send_message(chat_id, f"✅ کاربر `{text}` با موفقیت تایید شد.")
        send_message(text, "🎉 **دسترسی شما توسط مدیر فعال شد!**\nاکنون می‌توانید با ارسال /start از ربات استفاده کنید.")
        return send_admin_menu(chat_id)
        
    elif state == "WAITING_ADMIN_REV":
        revoke_user(text)
        send_message(chat_id, f"❌ دسترسی کاربر `{text}` لغو شد.")
        return send_admin_menu(chat_id)

    elif state == "WAITING_OTHER_LINK":
        match = URL_RE.search(text)
        if match:
            s["url"] = match.group(0)
            ask_format(chat_id)
        else: send_message(chat_id, "❌ لینک نامعتبر است.")
            
    elif state == "WAITING_YT_LINK":
        vid_id = extract_yt_id(text)
        if vid_id: fetch_preview(chat_id, vid_id)
        else: send_message(chat_id, "❌ لینک یوتیوب نامعتبر است.")
            
    elif state == "WAITING_YT_SEARCH":
        send_message(chat_id, "⏳ در حال جستجو...")
        log_history(user_id, "search", f"جستجوی ویدیو: {text}", "-", "", "-", {})
        SESSIONS[chat_id]["search_query"] = text
        SESSIONS[chat_id]["search_mode"] = "video"
        render_search_results(chat_id, None, query=text, page_num=1)
        
    elif state == "WAITING_YT_CHANNEL":
        send_message(chat_id, "⏳ در حال جستجوی کانال...")
        log_history(user_id, "search", f"جستجوی کانال: {text}", "-", "", "-", {})
        SESSIONS[chat_id]["search_query"] = text
        SESSIONS[chat_id]["search_mode"] = "channel"
        render_channel_search(chat_id, None, query=text, page_num=1)
        
    else:
        match = URL_RE.search(text)
        if match:
            url = match.group(0)
            if "youtu" in url:
                vid_id = extract_yt_id(url)
                if vid_id: 
                    SESSIONS[chat_id] = {"platform": "youtube", "extras": {"subs": True, "comments": True, "description": True, "thumbnail": True, "trim_start": None, "trim_end": None}}
                    fetch_preview(chat_id, vid_id)
            else:
                SESSIONS[chat_id] = {"platform": "other", "url": url, "extras": {"subs": False, "comments": False, "description": False, "thumbnail": False, "trim_start": None, "trim_end": None}}
                ask_format(chat_id)
        else: send_main_menu(chat_id)

def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    user_id = str(cq.get("from", {}).get("id", chat_id))
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    
    if data == "main:check_join":
        if check_membership(user_id):
            answer_callback(cq["id"], "✅ عضویت شما تایید شد!", show_alert=True)
            send_main_menu(chat_id, message_id)
        else: answer_callback(cq["id"], "❌ هنوز در کانال عضو نشده‌اید!", show_alert=True)
        return

    answer_callback(cq["id"])

    if not check_membership(user_id): return force_join_message(chat_id, message_id)
    if not is_approved(user_id) and user_id != ADMIN_ID: return access_denied_message(chat_id, user_id, message_id)

    s = SESSIONS.get(chat_id)
    if not s and not data.startswith("main:") and not data.startswith("admin"): return send_main_menu(chat_id, message_id)

    kind, _, value = data.partition(":")
    
    # NEW SEARCH PAGINATION & CLICKS
    if kind == "page":
        direction, _, p_num = value.partition(":")
        token = s.get("next_token") if direction == "next" else s.get("prev_token")
        
        if s.get("search_mode") == "video":
            render_search_results(chat_id, message_id, query=s.get("search_query"), channel_id=s.get("search_channel"), page_token=token, page_num=int(p_num))
            
    elif kind == "cpage":
        direction, _, p_num = value.partition(":")
        token = s.get("chan_next") if direction == "next" else s.get("chan_prev")
        render_channel_search(chat_id, message_id, query=s.get("search_query"), page_token=token, page_num=int(p_num))
        
    elif kind == "res":
        # User clicked a video number! (1 to 5)
        vid_id = s.get("search_results", [])[int(value)]
        fetch_preview(chat_id, vid_id, message_id)
        
    elif kind == "chan_res":
        # User clicked a channel number! Convert UI to show their videos
        chan_id = s.get("chan_results", [])[int(value)]
        SESSIONS[chat_id]["search_mode"] = "video"
        SESSIONS[chat_id]["search_query"] = None
        SESSIONS[chat_id]["search_channel"] = chan_id
        render_search_results(chat_id, message_id, channel_id=chan_id, page_num=1)
        
    elif kind == "trim" and value == "toggle":
        if s["extras"].get("trim_start"): 
            s["extras"]["trim_start"] = None
            s["extras"]["trim_end"] = None
            ask_extras(chat_id, message_id)
        else:
            SESSIONS[chat_id]["state"] = "WAITING_TRIM_START"
            edit_message(chat_id, message_id, "✂️ **برش ویدیو**\nلطفاً زمان **شروع** برش را بفرستید.\nمثال: `01:15` یا `00:30`")
            
    elif kind == "admin":
        if user_id != ADMIN_ID: return
        if value == "add":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD"}
            edit_message(chat_id, message_id, "➕ شناسه (ID) کاربری که می‌خواهید تایید کنید را ارسال کنید:")
        elif value == "rev":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_REV"}
            edit_message(chat_id, message_id, "➖ شناسه (ID) کاربری که می‌خواهید حذف کنید را ارسال کنید:")
        elif value == "list":
            users = list(get_all_users())[:30]
            kb = []
            for u in users:
                u_info = get_user_info(u)
                label = f"👤 {u_info.get('name')} (@{u_info.get('username')})" if u_info.get('username') else f"👤 {u_info.get('name')}"
                kb.append([btn(label, f"admin_u:{u}")])
            kb.append([btn("🔙 بازگشت به پنل اصلی", "admin:back")])
            delete_message(chat_id, message_id)
            send_message(chat_id, "👥 **لیست کاربران مجاز:**\nبرای مشاهده تاریخچه، روی نام کاربر کلیک کنید.", kb)
        elif value == "back":
            delete_message(chat_id, message_id)
            send_admin_menu(chat_id)

    elif kind == "admin_u":
        if user_id != ADMIN_ID: return
        target_user = value
        history_raw = db_cmd("LRANGE", f"hist:{target_user}", "0", "9")
        kb = []
        for i, h_str in enumerate(history_raw or []):
            try:
                h = json.loads(h_str)
                title = h.get("title", "Unknown")[:30]
                icon = "🎬" if h.get("type") == "download" else "🔍"
                kb.append([btn(f"{icon} {title}", f"admin_h:{target_user}:{i}")])
            except: pass
        kb.append([btn("🔙 بازگشت به لیست", "admin:list")])
        
        u_info = get_user_info(target_user)
        text = f"🗂 **تاریخچه فعالیت کاربر:**\n👤 {u_info.get('name')} (@{u_info.get('username')})"
        delete_message(chat_id, message_id)
        send_message(chat_id, text, kb)
        
    elif kind == "admin_h":
        if user_id != ADMIN_ID: return
        target_user, _, index = value.partition(":")
        history_raw = db_cmd("LINDEX", f"hist:{target_user}", index)
        if not history_raw: return
        
        h = json.loads(history_raw)
        d = h.get("details", {})
        
        lines = [
            f"🎬 **عنوان:** {h.get('title', '-')}",
            f"👤 **کانال:** {h.get('channel', '-')}",
            f"📝 **توضیحات:** {h.get('desc', '-')}\n",
            "⚙️ **تنظیمات دانلود:**",
            f"🔗 پلتفرم: {d.get('platform', '-')}",
            f"📦 فرمت: {d.get('format', '-')}",
        ]
        if d.get("format") == "mp4": lines.append(f"🎚 کیفیت: {d.get('quality', '-')}")
        
        ex = d.get("extras", {})
        if ex:
            lines.append(f"🔤 زیرنویس: {'✅' if ex.get('subs') else '❌'}")
            lines.append(f"💬 کامنت‌ها: {'✅' if ex.get('comments') else '❌'}")
            lines.append(f"🖼 کاور: {'✅' if ex.get('thumbnail') else '❌'}")
            if ex.get('trim_start'): lines.append(f"✂️ برش: از {ex['trim_start']} تا {ex['trim_end']}")
            
        lines.append(f"\n🕒 **زمان:** {h.get('time', '-')}")
        
        kb = [[btn("🔙 بازگشت به تاریخچه کاربر", f"admin_u:{target_user}")]]
        delete_message(chat_id, message_id)
        send_photo(chat_id, h.get("thumb", ""), "\n".join(lines), kb)

    elif kind == "main":
        if value == "back": send_main_menu(chat_id, message_id)
        elif value == "youtube": send_yt_menu(chat_id, message_id)
        else:
            SESSIONS.setdefault(chat_id, {})["platform"] = value
            SESSIONS[chat_id]["state"] = "WAITING_OTHER_LINK"
            SESSIONS[chat_id]["extras"] = {"subs": True, "comments": True, "description": True, "thumbnail": True, "trim_start": None, "trim_end": None}
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
        if value == "extras": return ask_confirm(chat_id, message_id)
        if value == "go":
            
            vid_title = s.get("vid_info", {}).get("title", s.get("url", "لینک ناشناس"))
            vid_channel = s.get("vid_info", {}).get("channel", "-")
            vid_desc = s.get("vid_info", {}).get("desc", "-")
            vid_thumb = s.get("vid_info", {}).get("thumb", "https://via.placeholder.com/600x400.png?text=No+Thumbnail")
            
            log_history(
                user_id=user_id, action_type="download", title=vid_title, channel=vid_channel, 
                thumb=vid_thumb, desc=vid_desc, 
                details={"platform": s["platform"], "format": s["format"], "quality": s.get("quality"), "extras": s["extras"]}
            )
            
            e = s["extras"]
            inputs = {
                "YT_URL": s["url"], "PLATFORM": s["platform"], "CHAT_ID": chat_id,
                "YT_QUALITY": s.get("quality") or "1080p", "YT_FORMAT": s["format"],
                "GET_SUBS": "true" if e.get("subs") else "false", "GET_COMMENTS": "true" if e.get("comments") else "false",
                "GET_DESC": "true" if e.get("description") else "false", "GET_THUMBNAIL": "true" if e.get("thumbnail") else "false",
                "TRIM_START": e.get("trim_start") or "", "TRIM_END": e.get("trim_end") or "",
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
        if "channel_post" in update: return jsonify({"ok": True})
        if "callback_query" in update: handle_callback(update["callback_query"])
        elif "message" in update: handle_message(update["message"])
    except Exception: pass
    return jsonify({"ok": True})

@app.route("/")
def health(): return "Taha Downloader Search Engine 2.0 running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
