import os
import tempfile
from yt_dlp import YoutubeDL
from pydub import AudioSegment

# Setup ffmpeg paths
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    print("[YouTube Agent] ffmpeg paths loaded via static-ffmpeg.")
except Exception as e:
    print(f"[YouTube Agent] Error loading static-ffmpeg paths: {e}")

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
        
    # Local browser fallbacks
    for b in ['chrome', 'edge', 'firefox', 'brave', 'safari', 'opera']:
        sources.append(('browser', b))
        
    sources.append(('none', None))
    
    for source_type, val in sources:
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch',
            'outtmpl': output_base + '.%(ext)s',
        }
        if source_type == 'file':
            ydl_opts['cookiefile'] = val
            print(f"[YouTube Agent] Trying to search/download using cookies file: {val}")
        elif source_type == 'browser':
            ydl_opts['cookiesfrombrowser'] = (val,)
            print(f"[YouTube Agent] Trying to search/download using cookies from: {val}")
        else:
            print("[YouTube Agent] Trying to search/download without cookies (fallback)")
            
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
            print(f"[YouTube Agent] Failed with {source_type} '{val}': {e}")
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
