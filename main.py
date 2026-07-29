import os
import sys
import math
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
GET_SUBS = os.environ.get('GET_SUBS', 'true').lower() == 'true'
BALE_TOKEN = os.environ.get('BALE_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')

height_map = {
    '8K': 4320, '4K': 2160, '1080p': 1080,
    '720p': 720, '480p': 480, '360p': 360, '240p': 240
}
target_height = height_map.get(TARGET_QUALITY, 1080)
DYNAMIC_QUALITY_STRING = f"bestvideo[height<={target_height}]+bestaudio/best"

def get_duration(file_path):
    cmd = [
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0

def get_video_height(file_path):
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=height',
        '-of', 'default=noprint_wrappers=1:nokey=1', file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0

def split_and_rename(file_path, max_mb=9.5):
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    ext = Path(file_path).suffix
    random_id = uuid.uuid4().hex[:8]  
    
    if size_mb <= max_mb:
        new_name = f"{random_id}-part000{ext}"
        os.rename(file_path, new_name)
        return [new_name]
        
    print(f"File is {size_mb:.2f}MB. Splitting...", flush=True)
    
    total_duration = get_duration(file_path)
    parts = []
    current_start = 0.0
    part_idx = 0
    
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
                else:
                    os.remove(out_name)

        target_mb = max_mb - 1.0 
        guess_dur = (target_mb / size_mb) * total_duration
        if guess_dur <= 0: guess_dur = 10.0
        
        best_file = None
        best_dur = 0.0
        
        for attempt in range(12): 
            out_name = f"temp_{random_id}_{part_idx}{ext}"
            if os.path.exists(out_name): os.remove(out_name)
                
            subprocess.run(['ffmpeg', '-y', '-nostdin', '-ss', str(current_start), '-i', file_path, '-t', str(guess_dur), '-c', 'copy', out_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            chunk_size = 0
            if os.path.exists(out_name):
                chunk_size = os.path.getsize(out_name) / (1024 * 1024)

            if chunk_size == 0:
                if os.path.exists(out_name): os.remove(out_name)
                if (current_start + guess_dur) >= (total_duration - 2.0):
                    break 
                else:
                    guess_dur += 5.0 
                    continue

            if chunk_size > max_mb:
                os.remove(out_name)
                ratio = target_mb / chunk_size
                guess_dur = guess_dur * ratio
            else:
                best_file = out_name
                best_dur = guess_dur
                if chunk_size >= (max_mb - 2.0) or (current_start + guess_dur >= total_duration):
                    break
                ratio = target_mb / max(chunk_size, 0.5)
                ratio = min(ratio, 2.5) 
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
        
        if actual_dur < 1.0 and (total_duration - current_start) < 2.0:
            break
            
    return parts

def send_text_to_bale(message_text):
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            requests.post(url, data={'chat_id': CHAT_ID, 'text': message_text}, timeout=30, verify=False)
            break
        except Exception:
            time.sleep(2)

def send_to_bale(file_path, part, total):
    ext = Path(file_path).suffix.lower()
    endpoint = "sendAudio" if ext == ".mp3" else "sendVideo"
    file_key = "audio" if ext == ".mp3" else "video"
    
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/{endpoint}"
    filename = Path(file_path).name
    actual_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    
    # Farsi caption formatting
    caption_text = f"📂 {filename}\n(بخش {part} از {total})"
    
    print(f"Uploading {filename} ({actual_size_mb:.2f} MB) via {endpoint}...", flush=True)
    with open(file_path, 'rb') as f:
        for attempt in range(4):
            try:
                response = requests.post(
                    url,
                    data={'chat_id': CHAT_ID, 'caption': caption_text},
                    files={file_key: f}, 
                    timeout=150,
                    verify=False 
                )
                response.raise_for_status()
                print(f"✅ Successfully sent {filename}", flush=True)
                break
            except requests.exceptions.RequestException as e:
                print(f"❌ Upload attempt {attempt+1} failed: {e}", flush=True)
                time.sleep(5)

def main():
    if not URL:
        print("❌ No URL provided by GitHub Actions!", flush=True)
        sys.exit(1)

    cookies_args = []
    if os.path.exists('cookies.txt') and os.path.getsize('cookies.txt') > 0:
        cookies_args = ['--cookies', 'cookies.txt']

    # ── Phase 1: metadata only (comments/description/info) — runs ONCE. ──
    # This is the expensive, slow-paginated part (comment threads etc).
    # Nothing after this point ever repeats it, even on download retries.
    if PLATFORM == 'youtube':
        meta_opts = [
            sys.executable, '-m', 'yt_dlp',
            '--output', 'temp_download.%(ext)s',
            '--no-check-certificates',
            '--verbose',
            '--impersonate', 'chrome',
            '--retries', '10',
            '--socket-timeout', '30',
            '--force-ipv4',
            '--legacy-server-connect',
            '--no-playlist',
            '--skip-download',
            '--write-description',
            '--write-info-json',
            '--write-comments',
            '--extractor-args', 'youtube:max-comments=1000',
            '--js-runtimes', 'node',
            '--remote-components', 'ejs:github',
            '--sleep-requests', '1',
            *cookies_args,
            URL,
        ]
        print("⏳ Phase 1/2: fetching metadata, description, comments...", flush=True)
        try:
            subprocess.run(meta_opts, check=True)
        except subprocess.CalledProcessError:
            print("❌ Metadata fetch failed.", flush=True)
            send_text_to_bale("❌ خطا در دریافت اطلاعات ویدیو. لطفاً بعداً دوباره امتحان کنید.")
            sys.exit(1)

    # ── Phase 2: actual media download — this is the part that retries. ──
    ydl_opts = [
        sys.executable, '-m', 'yt_dlp',
        '--output', 'temp_download.%(ext)s',
        '--no-check-certificates',
        '--verbose',
        '--retries', '10',
        '--fragment-retries', '10',
        '--socket-timeout', '30',
        '--force-ipv4',
        '--legacy-server-connect',
        '--sleep-requests', '1',
        '--sleep-subtitles', '5',
        '--retry-sleep', 'extractor:linear=1:10:2',
    ]

    if PLATFORM == 'youtube':
        # Reuse the metadata already fetched in Phase 1 — no re-extraction,
        # no re-fetching comments, no re-solving JS challenges.
        ydl_opts.extend(['--load-info-json', 'temp_download.info.json'])
    else:
        ydl_opts.extend(['--impersonate', 'chrome', '--no-playlist', '--write-description'])

    if FORMAT == 'mp3':
        ydl_opts.extend([
            '--format', 'bestaudio/best',
            '--extract-audio',
            '--audio-format', 'mp3',
            '--audio-quality', '0', 
            '--embed-metadata',         
            '--embed-thumbnail',        
            '--convert-thumbnails', 'jpg' 
        ])
    else:
        ydl_opts.extend([
            '--format', DYNAMIC_QUALITY_STRING, 
            '--merge-output-format', 'mp4',
            '--embed-metadata'
        ])
        
        if GET_SUBS:
            ydl_opts.extend([
                '--write-subs',
                '--write-auto-subs',
                '--sub-langs', 'fa,en',
                '--embed-subs',
                '--compat-options', 'no-keep-subs'
            ])

    ydl_opts.extend(cookies_args)
    ydl_opts.append(URL)  # required even with --load-info-json

    max_attempts = 3
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"⏳ Phase 2/2: downloading media (attempt {attempt}/{max_attempts})...", flush=True)
            subprocess.run(ydl_opts, check=True)
            last_error = None
            break
        except subprocess.CalledProcessError as e:
            last_error = e
            print(f"❌ Download attempt {attempt}/{max_attempts} failed.", flush=True)
            if attempt < max_attempts:
                backoff = 15 * attempt
                print(f"⏳ Retrying in {backoff}s (metadata/comments will NOT be re-fetched)...", flush=True)
                time.sleep(backoff)

    if last_error is not None:
        print("❌ Download crashed after all retries. Check the verbose logs above.", flush=True)
        send_text_to_bale("❌ خطا در دانلود ویدیو پس از چند تلاش. لطفاً بعداً دوباره امتحان کنید.")
        sys.exit(1)
    
    if FORMAT == 'mp3':
        files = glob.glob("temp_download.mp3")
    else:
        files = glob.glob("temp_download.mp4") + glob.glob("temp_download.webm") + glob.glob("temp_download.mkv")
        
    if not files:
        print("Download failed: No media file found.", flush=True)
        send_text_to_bale("❌ هیچ فایل رسانه‌ای پیدا نشد.")
        sys.exit(1)
    
    media_file = files[0]
    print(f"Download complete: {media_file}", flush=True)

    # Description Sender
    desc_file = "temp_download.description"
    if os.path.exists(desc_file):
        with open(desc_file, 'r', encoding='utf-8') as f:
            description_text = f.read().strip()
        
        if description_text:
            if len(description_text) > 4000:
                description_text = description_text[:4000] + "\n\n[متن به دلیل طولانی بودن خلاصه شد...]"
            send_text_to_bale(f"📝 **توضیحات ویدیو:**\n\n{description_text}")
            
    # YouTube Comment Parser
    if PLATFORM == 'youtube':
        info_files = glob.glob("temp_download.info.json")
        if info_files:
            try:
                with open(info_files[0], 'r', encoding='utf-8') as f:
                    info_data = json.load(f)
                    
                comments = info_data.get('comments')
                if comments:
                    comments_file_path = "YouTube_Comments.txt"
                    with open(comments_file_path, "w", encoding="utf-8") as f:
                        f.write(f"Comments for: {info_data.get('title', 'Media')}\n")
                        f.write("="*50 + "\n\n")
                        for c in comments:
                            author = c.get('author', 'Unknown')
                            text = c.get('text', '').replace('\n', '\n  ') 
                            likes = c.get('like_count', 0)
                            is_reply = c.get('parent', 'root') != 'root'
                            if is_reply:
                                f.write(f"    ↳ {author} ({likes} likes):\n    {text}\n\n")
                            else:
                                f.write(f"👤 {author} ({likes} likes):\n  {text}\n\n")
                                
                    print(f"Uploading {comments_file_path} to Bale...", flush=True)
                    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendDocument"
                    with open(comments_file_path, 'rb') as f:
                        for attempt in range(4):
                            try:
                                response = requests.post(
                                    url,
                                    data={'chat_id': CHAT_ID, 'caption': "💬 کامنت‌ها و پاسخ‌ها"},
                                    files={'document': f},
                                    timeout=150,
                                    verify=False
                                )
                                response.raise_for_status()
                                print(f"✅ Successfully sent {comments_file_path}", flush=True)
                                break
                            except Exception as e:
                                print(f"❌ Comment upload attempt {attempt+1} failed: {e}", flush=True)
                                time.sleep(5)
            except Exception as e:
                print(f"Failed to parse comments: {e}", flush=True)

    actual_height = get_video_height(media_file) if FORMAT != 'mp3' else 0
    parts = split_and_rename(media_file)
    
    for i, part in enumerate(parts):
        send_to_bale(part, i+1, len(parts))
        time.sleep(2) 
        
    if FORMAT != 'mp3' and 0 < actual_height < target_height:
        send_text_to_bale(f"⚠️ کیفیت {TARGET_QUALITY} موجود نبود، به همین دلیل کیفیت {actual_height}p دانلود شد.")

    print("🎉 Pipeline finished.", flush=True)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"💥 Unhandled crash: {e}", flush=True)
        try:
            send_text_to_bale(f"❌ خطای غیرمنتظره در پردازش: {e}")
        except Exception:
            pass
        sys.exit(1)
