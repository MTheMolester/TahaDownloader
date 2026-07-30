import os
import sys
import time
import uuid
import glob
import subprocess
import json
from pathlib import Path
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
os.environ['PYTHONHTTPSVERIFY'] = '0'

URL = os.environ.get('YT_URL')
PLATFORM = os.environ.get('PLATFORM', 'youtube').lower()
FORMAT = os.environ.get('YT_FORMAT', 'mp4').lower()
TARGET_QUALITY = os.environ.get('YT_QUALITY', '1080p') 
BALE_TOKEN = os.environ.get('BALE_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
MESSAGE_ID = os.environ.get('MESSAGE_ID')

GET_SUBS = os.environ.get('GET_SUBS', 'true').lower() == 'true'
GET_COMMENTS = os.environ.get('GET_COMMENTS', 'true').lower() == 'true'
GET_DESC = os.environ.get('GET_DESC', 'true').lower() == 'true'
GET_THUMBNAIL = os.environ.get('GET_THUMBNAIL', 'true').lower() == 'true'

# Fetch trim variables safely from the GitHub environment
TRIM_START = os.environ.get('TRIM_START')
TRIM_END = os.environ.get('TRIM_END')

height_map = {
    '8K': 4320, '4K': 2160, '1080p': 1080,
    '720p': 720, '480p': 480, '360p': 360, '240p': 240
}
target_height = height_map.get(TARGET_QUALITY, 1080)
DYNAMIC_QUALITY_STRING = f"bestvideo[height<={target_height}]+bestaudio/best"

def update_progress(text):
    if not MESSAGE_ID: return
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/editMessageText"
    try: requests.post(url, data={'chat_id': CHAT_ID, 'message_id': MESSAGE_ID, 'text': text}, timeout=10, verify=False)
    except Exception: pass

def get_duration(file_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    try: return float(result.stdout.strip())
    except ValueError: return 0.0

def get_video_height(file_path):
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=height', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    try: return int(result.stdout.strip())
    except ValueError: return 0

def split_and_rename(file_path, max_mb=9.5):
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    ext = Path(file_path).suffix
    random_id = uuid.uuid4().hex[:8]  
    
    if size_mb <= max_mb:
        new_name = f"{random_id}-part000{ext}"
        os.rename(file_path, new_name)
        return [new_name]
        
    update_progress("✂️ **در حال پردازش و برش ویدیو...**\n(چون حجم فایل بالاتر از حد مجاز بله است)")
    total_duration = get_duration(file_path)
    parts, current_start, part_idx = [], 0.0, 0
    
    while current_start < total_duration:
        estimated_remaining_mb = ((total_duration - current_start) / total_duration) * size_mb
        
        if estimated_remaining_mb <= (max_mb * 0.85):
            out_name = f"temp_{random_id}_{part_idx}{ext}"
            if os.path.exists(out_name): os.remove(out_name)
            subprocess.run(['ffmpeg', '-y', '-nostdin', '-ss', str(current_start), '-i', file_path, '-c', 'copy', out_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            if os.path.exists(out_name) and os.path.getsize(out_name) > 0:
                chunk_size = os.path.getsize(out_name) / (1024 * 1024)
                if chunk_size <= max_mb:
                    final_name = f"{random_id}-part{part_idx:03d}{ext}"
                    os.rename(out_name, final_name)
                    parts.append(final_name)
                    break 
                else: os.remove(out_name)

        target_mb = max_mb - 1.0 
        guess_dur = (target_mb / size_mb) * total_duration
        if guess_dur <= 0: guess_dur = 10.0
        best_file, best_dur = None, 0.0
        
        for attempt in range(12): 
            out_name = f"temp_{random_id}_{part_idx}{ext}"
            if os.path.exists(out_name): os.remove(out_name)
            subprocess.run(['ffmpeg', '-y', '-nostdin', '-ss', str(current_start), '-i', file_path, '-t', str(guess_dur), '-c', 'copy', out_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            chunk_size = os.path.getsize(out_name) / (1024 * 1024) if os.path.exists(out_name) else 0

            if chunk_size == 0:
                if os.path.exists(out_name): os.remove(out_name)
                if (current_start + guess_dur) >= (total_duration - 2.0): break 
                else: guess_dur += 5.0; continue

            if chunk_size > max_mb:
                os.remove(out_name)
                guess_dur = guess_dur * (target_mb / chunk_size)
            else:
                best_file, best_dur = out_name, guess_dur
                if chunk_size >= (max_mb - 2.0) or (current_start + guess_dur >= total_duration): break
                ratio = min((target_mb / max(chunk_size, 0.5)), 2.5) 
                if ratio < 1.15: ratio = 1.2
                guess_dur = guess_dur * ratio

        if not best_file or not os.path.exists(best_file):
            best_file = f"temp_{random_id}_{part_idx}{ext}"
            best_dur = 5.0
            subprocess.run(['ffmpeg', '-y', '-nostdin', '-ss', str(current_start), '-i', file_path, '-t', '5', '-c', 'copy', best_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if not os.path.exists(best_file) or os.path.getsize(best_file) == 0:
            if os.path.exists(best_file): os.remove(best_file)
            break 

        final_name = f"{random_id}-part{part_idx:03d}{ext}"
        os.rename(best_file, final_name)
        parts.append(final_name)
        
        actual_dur = get_duration(final_name)
        if actual_dur <= 0.5: actual_dur = best_dur
            
        current_start += actual_dur
        part_idx += 1
        if actual_dur < 1.0 and (total_duration - current_start) < 2.0: break
            
    return parts

def send_text_to_bale(message_text):
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            requests.post(url, data={'chat_id': CHAT_ID, 'text': message_text}, timeout=30, verify=False)
            break
        except Exception: time.sleep(2)

def send_photo_to_bale(file_path):
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendPhoto"
    with open(file_path, 'rb') as f:
        for attempt in range(3):
            try:
                requests.post(url, data={'chat_id': CHAT_ID, 'caption': "🖼 کاور ویدیو (Thumbnail)"}, files={'photo': f}, timeout=30, verify=False)
                break
            except Exception: time.sleep(2)

def send_to_bale(file_path, part, total):
    ext = Path(file_path).suffix.lower()
    endpoint = "sendAudio" if ext == ".mp3" else "sendVideo"
    file_key = "audio" if ext == ".mp3" else "video"
    
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/{endpoint}"
    filename = Path(file_path).name
    caption_text = f"📂 {filename}\n(بخش {part} از {total})"
    
    with open(file_path, 'rb') as f:
        for attempt in range(4):
            try:
                response = requests.post(url, data={'chat_id': CHAT_ID, 'caption': caption_text}, files={file_key: f}, timeout=150, verify=False)
                response.raise_for_status()
                break
            except requests.exceptions.RequestException: time.sleep(5)

def main():
    if not URL: return
    
    update_progress("🔄 **سرور استارت خورد...**\nدر حال دریافت اطلاعات مدیا...")

    ydl_opts = [
        sys.executable, '-m', 'yt_dlp',
        '--output', 'temp_download.%(ext)s',
        '--no-check-certificates',
        '--impersonate', 'chrome',
        '--retries', '10', 
        '--fragment-retries', '10', 
        '--socket-timeout', '30',
        '--force-ipv4', 
        '--legacy-server-connect',
        '--no-playlist'
    ]

    if GET_DESC: ydl_opts.append('--write-description')
    if GET_THUMBNAIL: ydl_opts.extend(['--write-thumbnail', '--convert-thumbnails', 'jpg'])

    if PLATFORM == 'youtube':
        ydl_opts.extend(['--js-runtimes', 'node', '--remote-components', 'ejs:github'])
        if GET_COMMENTS: ydl_opts.extend(['--write-info-json', '--write-comments', '--extractor-args', 'youtube:max-comments=1000'])
    
    if FORMAT == 'mp3':
        ydl_opts.extend(['--format', 'bestaudio/best', '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0', '--embed-metadata', '--embed-thumbnail', '--convert-thumbnails', 'jpg'])
    else:
        ydl_opts.extend(['--format', DYNAMIC_QUALITY_STRING, '--merge-output-format', 'mp4', '--embed-metadata'])
        if GET_SUBS: ydl_opts.extend(['--write-subs', '--write-auto-subs', '--sub-langs', 'fa.*,en.*', '--embed-subs', '--compat-options', 'no-keep-subs'])
    
    if os.path.exists('cookies.txt') and os.path.getsize('cookies.txt') > 0:
        ydl_opts.extend(['--cookies', 'cookies.txt'])
        
    # Inject the trimming commands directly into the CLI arguments
    if TRIM_START and TRIM_END:
        ydl_opts.extend([
            '--download-sections', f"*{TRIM_START}-{TRIM_END}",
            '--force-keyframes-at-cuts'
        ])
        
    ydl_opts.append(URL)
    
    try:
        process = subprocess.Popen(ydl_opts, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
        last_update = time.time()
        for line in process.stdout:
            print(line, end="")
            if "[download]" in line and "%" in line and "ETA" in line:
                now = time.time()
                if now - last_update > 4.5: 
                    clean_line = line.replace("[download]", "").strip()
                    update_progress(f"⬇️ **در حال دانلود...**\n`{clean_line}`")
                    last_update = now
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)
    except Exception:
        update_progress("❌ خطا در دانلود ویدیو. ممکن است لینک خراب باشد یا نیاز به کوکی‌های جدید داشته باشید.")
        return
    
    update_progress("✅ **دانلود به اتمام رسید!**\nدر حال آماده‌سازی و پردازش فایل...")
    
    if FORMAT == 'mp3': files = glob.glob("temp_download.mp3")
    else: files = glob.glob("temp_download.mp4") + glob.glob("temp_download.webm") + glob.glob("temp_download.mkv")
        
    if not files:
        update_progress("❌ هیچ فایل رسانه‌ای پیدا نشد.")
        return
    
    media_file = files[0]

    update_progress("📤 **در حال آپلود فایل‌ها و ضمایم به چت شما...**")

    if GET_THUMBNAIL:
        thumb_files = glob.glob("temp_download.jpg") + glob.glob("temp_download.webp") + glob.glob("temp_download.png")
        if thumb_files: send_photo_to_bale(thumb_files[0])

    desc_file = "temp_download.description"
    if GET_DESC and os.path.exists(desc_file):
        with open(desc_file, 'r', encoding='utf-8') as f: description_text = f.read().strip()
        if description_text:
            if len(description_text) > 4000: description_text = description_text[:4000] + "\n\n[متن به دلیل طولانی بودن خلاصه شد...]"
            send_text_to_bale(f"📝 **توضیحات:**\n\n{description_text}")
            
    if PLATFORM == 'youtube' and GET_COMMENTS:
        info_files = glob.glob("temp_download.info.json")
        if info_files:
            try:
                with open(info_files[0], 'r', encoding='utf-8') as f: info_data = json.load(f)
                comments = info_data.get('comments')
                if comments:
                    comments_file_path = "YouTube_Comments.txt"
                    with open(comments_file_path, "w", encoding="utf-8") as f:
                        f.write(f"Comments for: {info_data.get('title', 'Media')}\n")
                        f.write("="*50 + "\n\n")
                        for c in comments:
                            author, text, likes = c.get('author', 'Unknown'), c.get('text', '').replace('\n', '\n  '), c.get('like_count', 0)
                            if c.get('parent', 'root') != 'root': f.write(f"    ↳ {author} ({likes} likes):\n    {text}\n\n")
                            else: f.write(f"👤 {author} ({likes} likes):\n  {text}\n\n")
                    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendDocument"
                    with open(comments_file_path, 'rb') as f:
                        requests.post(url, data={'chat_id': CHAT_ID, 'caption': "💬 کامنت‌ها و پاسخ‌ها"}, files={'document': f}, timeout=150, verify=False)
            except Exception: pass

    actual_height = get_video_height(media_file) if FORMAT != 'mp3' else 0
    parts = split_and_rename(media_file)
    
    for i, part in enumerate(parts):
        send_to_bale(part, i+1, len(parts))
        time.sleep(2) 
        
    if FORMAT != 'mp3' and 0 < actual_height < target_height:
        send_text_to_bale(f"⚠️ کیفیت {TARGET_QUALITY} موجود نبود، به همین دلیل کیفیت {actual_height}p دانلود شد.")
        
    update_progress("🎉 **فایل‌ها با موفقیت برای شما ارسال شد!**")

if __name__ == "__main__":
    main()
