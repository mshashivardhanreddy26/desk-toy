import os
import sys
import tempfile
import base64
import shutil
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from pydub import AudioSegment

load_dotenv()

# Setup ffmpeg paths
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    print("[YouTube Agent] ffmpeg paths loaded via static-ffmpeg.")
except Exception as e:
    print(f"[YouTube Agent] Error loading static-ffmpeg paths: {e}")

# 1. THE COOKIE SCRUBBER (Critical for Render/Linux)
cookies_b64 = os.getenv("YOUTUBE_COOKIES_BASE64")
if cookies_b64:
    try:
        raw_cookies = base64.b64decode(cookies_b64.strip()).decode('utf-8')
        
        # Vigorously scrub any Windows carriage returns that survived encoding
        clean_cookies = raw_cookies.replace('\r\n', '\n').replace('\r', '').strip()
        
        # Write to cookies.txt in CWD with strict Linux line endings
        with open("cookies.txt", "w", encoding="utf-8", newline="\n") as f:
            f.write(clean_cookies)
            
        # Also write to backend/cookies.txt if backend directory exists
        if os.path.exists("backend") and os.path.isdir("backend"):
            try:
                with open("backend/cookies.txt", "w", encoding="utf-8", newline="\n") as f:
                    f.write(clean_cookies)
            except Exception:
                pass
                
        print("[YouTube Agent] Decoded, cleaned, and wrote cookies.txt successfully.")
    except Exception as e:
        print(f"[YouTube Agent] Error decoding/writing cookies.txt from environment: {e}")

def download_youtube_audio(query: str, output_base: str):
    """
    Search YouTube for a song and download the best audio format.
    Returns: (actual_file_path, song_title)
    """
    cookie_paths = ["cookies.txt", "backend/cookies.txt", "../cookies.txt"]
    cookie_file = next((cp for cp in cookie_paths if os.path.exists(cp)), None)
            
    sources = []
    if cookie_file:
        sources.append(('file', cookie_file))
        
    # Skip browser fallbacks on Linux (Render) to prevent crash loops
    if not sys.platform.startswith('linux'):
        for b in ['chrome', 'edge', 'firefox', 'brave', 'safari', 'opera']:
            sources.append(('browser', b))
        
    sources.append(('none', None))
    
    # Use mweb client as recommended by yt-dlp for PO Tokens
    client_options = [['mweb', 'ios', 'android'], None]
    
    default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
    user_agent = os.getenv("YT_DLP_USER_AGENT", default_ua)
    
    # 2. PO TOKEN PROVIDER HOOK
    bgutil_path = shutil.which('bgutil-pot')
    if bgutil_path:
        print(f"[YouTube Agent] Found bgutil-pot script at: {bgutil_path}. Activating PO Token solver.")
        
    for source_type, val in sources:
        for clients in client_options:
            ydl_opts = {
                'format': 'bestaudio/best',
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
                'default_search': 'ytsearch',
                'outtmpl': output_base + '.%(ext)s',
                'user_agent': user_agent,
            }
            
            # Dynamically build extractor args for clients and PO token
            extractor_args = {}
            if clients:
                extractor_args['youtube'] = {'player_client': clients}
            if bgutil_path:
                # This hooks the PO provider into yt-dlp
                extractor_args['youtubepot'] = {'bgutil_script': bgutil_path}
                
            if extractor_args:
                ydl_opts['extractor_args'] = extractor_args

            if source_type == 'file':
                ydl_opts['cookiefile'] = val
                print(f"[YouTube Agent] Trying download with cookies file ({val}) and clients {clients}")
            elif source_type == 'browser':
                ydl_opts['cookiesfrombrowser'] = (val,)
                print(f"[YouTube Agent] Trying download with browser cookies ({val}) and clients {clients}")
            else:
                print(f"[YouTube Agent] Trying download without cookies and clients {clients}")
                
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch1:{query}", download=True)
                    if 'entries' in info and len(info['entries']) > 0:
                        entry = info['entries'][0]
                        ext = entry.get('ext', 'webm')
                        actual_file = f"{output_base}.{ext}"
                        return actual_file, entry.get('title', 'Unknown')
                    elif 'id' in info:
                        ext = info.get('ext', 'webm')
                        actual_file = f"{output_base}.{ext}"
                        return actual_file, info.get('title', 'Unknown')
            except Exception as e:
                print(f"[YouTube Agent] Failed with {source_type} '{val}' and clients {clients}: {e}")
                continue
                
    raise Exception("Failed to download audio from YouTube. All cookie sources and browser fallbacks failed.")

def get_youtube_pcm(query: str):
    """
    Searches YouTube, downloads the audio, decodes it to 22050Hz Mono PCM (16-bit),
    cleans up temp files, and returns (pcm_bytes, song_title).
    """
    temp_dir = tempfile.gettempdir()
    output_base = os.path.join(temp_dir, f"yt_audio_{os.getpid()}")
    actual_file = None
    try:
        print(f"[YouTube Agent] Searching and downloading: {query}")
        actual_file, title = download_youtube_audio(query, output_base)
        
        if not actual_file or not os.path.exists(actual_file):
            raise Exception("Audio download failed.")
        
        print(f"[YouTube Agent] Download complete: {actual_file}. Processing...")
        
        # Load and convert format for the ESP32 I2S stream
        audio = AudioSegment.from_file(actual_file)
        audio = audio.set_frame_rate(22050).set_channels(1).set_sample_width(2)
        
        pcm_bytes = audio.raw_data
        print(f"[YouTube Agent] Resampled to 22050Hz Mono 16-bit. Total bytes: {len(pcm_bytes)}")
        return pcm_bytes, title
        
    except Exception as e:
        print(f"[YouTube Agent] Error: {e}")
        raise e
    finally:
        if actual_file and os.path.exists(actual_file):
            try:
                os.remove(actual_file)
                print(f"[YouTube Agent] Cleaned up temp file: {actual_file}")
            except Exception as e:
                print(f"[YouTube Agent] Failed to remove temp file: {e}")

if __name__ == "__main__":
    test_query = "Blinding Lights" if len(sys.argv) < 2 else sys.argv[1]
    print(f"Testing YouTube audio agent with query: '{test_query}'...")
    try:
        pcm, title = get_youtube_pcm(test_query)
        print(f"SUCCESS! Retrieved '{title}' ({len(pcm)} bytes of PCM).")
    except Exception as e:
        print(f"FAILURE: {e}")
