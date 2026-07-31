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

TRIM_START = os.environ.get('TRIM_START')
TRIM_END = os.environ.get('TRIM_END')

height_map = {
    '8K': 4320, '4K': 2160, '1080p': 1080,
    '720p': 720, '480p': 480, '360p': 360, '240p': 240
}
target_height = height_map.get(TARGET_QUALITY, 1080)
DYNAMIC_QUALITY_STRING = f"bestvideo[height<={target_height}]+bestaudio/best"

def update_progress(text):
    # NOW PRINTS TO GITHUB CONSOLE AS WELL AS SENDING TO BALE
    clean_text = text.replace('*', '').replace('`', '')
    print(f"\n[BOT STATE] {clean_text}", flush=True)
    
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

def split_and_rename(file_path, max_mb=45.0):
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    ext = Path(file_path).suffix
    random_id = uuid.uuid4().hex[:8]  
    
    if size_mb <= max_mb:
        new_name = f"{random_id}-part000{ext}"
        os.rename(file_path, new_name)
        print(f"[FFMPEG] File is {size_mb:.1f} MB (Under {max_mb} MB limit). No splitting required.", flush=True)
        return [new_name]
        
    update_progress(f"✂️ **در حال پردازش سریع و برش ویدیو...**\n(حجم فایل: {size_mb:.1f} MB)")
    print(f"[FFMPEG] Commencing high-speed segmentation for {size_mb:.1f} MB file...", flush=True)
    
    total_duration = get_duration(file_path)
    if total_duration <= 0: total_duration = 10.0 
    
    avg_mb_per_sec = size_mb / total_duration
    safe_segment_time = int((max_mb * 0.85) / avg_mb_per_sec) 
    if safe_segment_time < 5: safe_segment_time = 5
    
    print(f"[FFMPEG] Cutting video every {safe_segment_time} seconds to stay under {max_mb} MB limit.", flush=True)
    
    subprocess.run([
        'ffmpeg', '-y', '-nostdin', '-i', file_path, 
        '-c', 'copy', '-f', 'segment', 
        '-segment_time', str(safe_segment_time), 
        '-reset_timestamps', '1', 
        f"{random_id}-part%03d{ext}"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    parts = sorted(glob.glob(f"{random_id}-part*{ext}"))
    
    final_parts = []
    for p in parts:
        if os.path.getsize(p) > 100 * 1024: 
            final_parts.append(p)
        else:
            os.remove(p)
            
    print(f"[FFMPEG] Success! Video split into {len(final_parts)} parts.", flush=True)
    return final_parts

def send_text_to_bale(message_text):
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            requests.post(url, data={'chat_id': CHAT_ID, 'text': message_text}, timeout=30, verify=False)
            break
        except Exception: time.sleep(2)

def send_photo_to_bale(file_path):
    print(f"[UPLOAD] Uploading thumbnail: {file_path}", flush=True)
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendPhoto"
    with open(file_path, 'rb') as f:
        for attempt in range(3):
            try:
                requests.post(url, data={'chat_id': CHAT_ID, 'caption': "🖼 کاور ویدیو (Thumbnail)"}, files={'photo': f}, timeout=30, verify=False)
                break
            except Exception as e: 
                print(f"[UPLOAD ERROR] Thumbnail upload failed on attempt {attempt+1}: {e}", flush=True)
                time.sleep(2)

def send_to_bale(file_path, part, total):
    ext = Path(file_path).suffix.lower()
    endpoint = "sendAudio" if ext == ".mp3" else "sendVideo"
    file_key = "audio" if ext == ".mp3" else "video"
    
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/{endpoint}"
    filename = Path(file_path).name
    caption_text = f"📂 {filename}\n(بخش {part} از {total})"
    
    print(f"[UPLOAD] Starting upload for part {part}/{total}: {filename} (Size: {os.path.getsize(file_path) / (1024*1024):.1f} MB)...", flush=True)
    
    with open(file_path, 'rb') as f:
        for attempt in range(4):
            try:
                response = requests.post(url, data={'chat_id': CHAT_ID, 'caption': caption_text}, files={file_key: f}, timeout=150, verify=False)
                response.raise_for_status()
                print(f"[UPLOAD] Successfully uploaded part {part}/{total}!", flush=True)
                break
            except requests.exceptions.RequestException as e: 
                print(f"[UPLOAD ERROR] Part {part} failed on attempt {attempt+1}. Retrying... Error: {e}", flush=True)
                time.sleep(5)

def main():
    if not URL: 
        print("[ERROR] No URL provided. Exiting.", flush=True)
        return
    
    update_progress("🔄 **سرور استارت خورد...**\nدر حال دریافت اطلاعات مدیا...")
    print(f"[INIT] URL: {URL} | FORMAT: {FORMAT} | QUALITY: {TARGET_QUALITY}", flush=True)

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
        print("[INIT] Loaded YouTube cookies successfully.", flush=True)
        
    if TRIM_START and TRIM_END:
        ydl_opts.extend([
            '--download-sections', f"*{TRIM_START}-{TRIM_END}",
            '--force-keyframes-at-cuts'
        ])
        print(f"[INIT] Trim arguments applied: {TRIM_START} to {TRIM_END}", flush=True)
        
    ydl_opts.append(URL)
    
    try:
        process = subprocess.Popen(ydl_opts, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
        last_update = time.time()
        for line in process.stdout:
            print(line, end="", flush=True) # Forces line to show in GitHub immediately
            if "[download]" in line and "%" in line and "ETA" in line:
                now = time.time()
                if now - last_update > 4.5: 
                    clean_line = line.replace("[download]", "").strip()
                    # Only send to Bale, don't spam GitHub with every percentage 
                    if MESSAGE_ID:
                        url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/editMessageText"
                        try: requests.post(url, data={'chat_id': CHAT_ID, 'message_id': MESSAGE_ID, 'text': f"⬇️ **در حال دانلود...**\n`{clean_line}`"}, timeout=5, verify=False)
                        except Exception: pass
                    last_update = now
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)
    except Exception as e:
        update_progress("❌ خطا در دانلود ویدیو. ممکن است لینک خراب باشد یا نیاز به کوکی‌های جدید داشته باشید.")
        print(f"[CRITICAL ERROR] Download failed: {e}", flush=True)
        return
    
    update_progress("✅ **دانلود به اتمام رسید!**\nدر حال آماده‌سازی و پردازش فایل...")
    
    if FORMAT == 'mp3': files = glob.glob("temp_download.mp3")
    else: files = glob.glob("temp_download.mp4") + glob.glob("temp_download.webm") + glob.glob("temp_download.mkv")
        
    if not files:
        update_progress("❌ هیچ فایل رسانه‌ای پیدا نشد.")
        print("[ERROR] No media file was found in the directory after download.", flush=True)
        return
    
    media_file = files[0]

    update_progress("📤 **در حال آپلود فایل‌ها و ضمایم به چت شما...**")

    if GET_THUMBNAIL:
        thumb_files = glob.glob("temp_download.jpg") + glob.glob("temp_download.webp") + glob.glob("temp_download.png")
        if thumb_files: send_photo_to_bale(thumb_files[0])

    desc_file = "temp_download.description"
    if GET_DESC and os.path.exists(desc_file):
        print("[UPLOAD] Processing description file...", flush=True)
        with open(desc_file, 'r', encoding='utf-8') as f: description_text = f.read().strip()
        if description_text:
            if len(description_text) > 4000: description_text = description_text[:4000] + "\n\n[متن به دلیل طولانی بودن خلاصه شد...]"
            send_text_to_bale(f"📝 **توضیحات:**\n\n{description_text}")
            
    if PLATFORM == 'youtube' and GET_COMMENTS:
        info_files = glob.glob("temp_download.info.json")
        if info_files:
            print("[UPLOAD] Extracting and formatting comments...", flush=True)
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
                    print("[UPLOAD] Comments successfully uploaded.", flush=True)
            except Exception as e: 
                print(f"[ERROR] Failed to process comments: {e}", flush=True)

    actual_height = get_video_height(media_file) if FORMAT != 'mp3' else 0
    
    print(f"[PROCESS] Pre-processing check. Target resolution: {target_height}p, Actual: {actual_height}p", flush=True)
    
    parts = split_and_rename(media_file)
    
    print(f"[UPLOAD STAGE] Beginning upload loop for {len(parts)} parts...", flush=True)
    for i, part in enumerate(parts):
        send_to_bale(part, i+1, len(parts))
        time.sleep(2) 
        
    if FORMAT != 'mp3' and 0 < actual_height < target_height:
        send_text_to_bale(f"⚠️ کیفیت {TARGET_QUALITY} موجود نبود، به همین دلیل کیفیت {actual_height}p دانلود شد.")
        print(f"[INFO] Alerted user that requested {TARGET_QUALITY} was downgraded to {actual_height}p.", flush=True)
        
    update_progress("🎉 **فایل‌ها با موفقیت برای شما ارسال شد!**")
    print("[SYSTEM] Workflow completed successfully. Exiting.", flush=True)

if __name__ == "__main__":
    main()
