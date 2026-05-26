import os
import sys
import tempfile
import base64
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

# Decode Base64 cookies if present in the environment
cookies_b64 = os.getenv("YOUTUBE_COOKIES_BASE64")
if cookies_b64:
    try:
        cookies_data = base64.b64decode(cookies_b64.strip()).decode('utf-8')
        
        # Write to cookies.txt in CWD
        with open("cookies.txt", "w", encoding="utf-8", newline="\n") as f:
            f.write(cookies_data)
            
        # Also write to backend/cookies.txt if backend directory exists and we are in root CWD
        if os.path.exists("backend") and os.path.isdir("backend"):
            try:
                with open("backend/cookies.txt", "w", encoding="utf-8", newline="\n") as f:
                    f.write(cookies_data)
            except Exception:
                pass
                
        print("[YouTube Agent] Decoded and wrote cookies.txt successfully from environment variable.")
    except Exception as e:
        print(f"[YouTube Agent] Error decoding/writing cookies.txt from environment: {e}")

def download_youtube_audio(query: str, output_base: str):
    """
    Search YouTube for a song and download the best audio format.
    Returns: (actual_file_path, song_title)
    """
    # Check if a local cookies.txt file is present (essential for Render/VPS deployment)
    cookie_paths = ["cookies.txt", "backend/cookies.txt", "../cookies.txt"]
    cookie_file = None
    for cp in cookie_paths:
        if os.path.exists(cp):
            cookie_file = cp
            break
            
    sources = []
    if cookie_file:
        sources.append(('file', cookie_file))
        
    # Local browser fallbacks (skip on Linux/Render to avoid noisy unsupported platform / no database errors)
    if not sys.platform.startswith('linux'):
        for b in ['chrome', 'edge', 'firefox', 'brave', 'safari', 'opera']:
            sources.append(('browser', b))
        
    sources.append(('none', None))
    
    # Try different player clients to bypass DRM / blockages (mobile clients bypass cloud restrictions)
    client_options = [
        ['ios', 'android', 'mweb'], 
        ['default']
    ]
    
    # Configure a modern user agent (allows matching cookies and avoiding basic anti-bot blocks)
    # We default to Firefox to match the exported Firefox cookies.txt session
    default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
    user_agent = os.getenv("YT_DLP_USER_AGENT", default_ua)
    
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
                'extractor_args': {
                    'youtube': {
                        'player_client': clients
                    }
                }
            }
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
        
        # Load and convert format
        audio = AudioSegment.from_file(actual_file)
        audio = audio.set_frame_rate(22050).set_channels(1).set_sample_width(2)
        
        pcm_bytes = audio.raw_data
        print(f"[YouTube Agent] Resampled to 22050Hz Mono 16-bit. Total bytes: {len(pcm_bytes)}")
        return pcm_bytes, title
    except Exception as e:
        print(f"[YouTube Agent] Error: {e}")
        raise e
    finally:
        # Clean up the downloaded file
        if actual_file and os.path.exists(actual_file):
            try:
                os.remove(actual_file)
                print(f"[YouTube Agent] Cleaned up temp file: {actual_file}")
            except Exception as e:
                print(f"[YouTube Agent] Failed to remove temp file {actual_file}: {e}")

if __name__ == "__main__":
    import sys
    test_query = "Blinding Lights" if len(sys.argv) < 2 else sys.argv[1]
    print(f"Testing YouTube audio agent with query: '{test_query}'...")
    try:
        pcm, title = get_youtube_pcm(test_query)
        print(f"SUCCESS! Retrieved '{title}' ({len(pcm)} bytes of PCM).")
    except Exception as e:
        print(f"FAILURE: {e}")
