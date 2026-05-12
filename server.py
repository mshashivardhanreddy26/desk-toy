import os
import io
import json
import wave
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import edge_tts
from pydub import AudioSegment
import uvicorn
import asyncio
import re

app = FastAPI()

# --- PERSISTENT STATE ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MEMORY_FILE = "memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {"user_name": "Friend", "convo_history": []}

def save_memory(name, history):
    with open(MEMORY_FILE, "w") as f:
        json.dump({"user_name": name, "convo_history": history[-10:]}, f)

# Initial Load
mem = load_memory()
user_name = mem["user_name"]
convo_history = mem["convo_history"]
current_emotion = "idle"
last_interaction = {"user": "None yet", "ai": "Waiting...", "emotion": "idle"}
system_prompt = """You are 'Bit', a cute emotional desk robot. 
Keep answers under 2 sentences. 
If the user tells you their name, you MUST update the "user_name" field in your JSON.
Format your response EXACTLY as JSON:
{"emotion": "one of: idle, happy, excited, thinking, sleepy, sad", "text": "...", "user_name": "..."}"""

# --- UTILS ---
def clean_json(raw_res):
    try:
        json_match = re.search(r'\{.*\}', raw_res, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        return {"text": raw_res, "emotion": "happy", "user_name": None}
    except:
        return {"text": raw_res, "emotion": "thinking", "user_name": None}

def transcribe_audio_groq(audio_bytes):
    try:
        with io.BytesIO() as wav_buffer:
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(audio_bytes)
            wav_data = wav_buffer.getvalue()

        files = {"file": ("audio.wav", wav_data)}
        response = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            data={"model": "whisper-large-v3"},
            files=files,
            timeout=10.0
        )
        return response.json().get("text", "")
    except Exception as e:
        print(f"STT Error: {e}")
        return ""

async def get_ai_response(text):
    global convo_history, user_name, current_emotion, last_interaction
    # Combined system prompt for better model compatibility
    full_system = f"{system_prompt}\nThe current user's name is {user_name}."
    
    messages = [{"role": "system", "content": full_system}]
    messages.extend(convo_history[-6:])
    messages.append({"role": "user", "content": text})

    try:
        async with httpx.AsyncClient() as client:
            # LATEST GROQ MODEL (Llama 3.3 70B)
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile", "messages": messages},
                timeout=15.0
            )
            
            res_data = response.json()
            if 'choices' not in res_data:
                print(f"!!! GROQ BRAIN ERROR: {res_data}")
                return "My brain is buzzing!", "thinking"

            data = clean_json(res_data['choices'][0]['message']['content'])
            ai_text = data.get("text", "I'm here!")
            current_emotion = data.get("emotion", "happy")
            if data.get("user_name"): user_name = data.get("user_name")
            
            convo_history.append({"role": "user", "content": text})
            convo_history.append({"role": "assistant", "content": ai_text})
            last_interaction.update({"user": text, "ai": ai_text, "emotion": current_emotion})
            save_memory(user_name, convo_history)
            return ai_text, current_emotion
    except Exception as e:
        print(f"Brain Error: {e}")
        return "I need a little rest!", "sleepy"

async def generate_speech(text):
    try:
        print(f"VOICE-BOX: Starting for '{text}'")
        communicate = edge_tts.Communicate(text, "en-US-AndrewNeural", rate="-10%")
        mp3_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio": mp3_data.extend(chunk["data"])
        
        if not mp3_data:
            print("VOICE-BOX: !!! FAILED (No data from Edge-TTS)")
            return None
            
        # Convert to PCM using pydub
        audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        print(f"VOICE-BOX: SUCCESS! Created {len(audio.raw_data)} bytes")
        return audio.raw_data
    except Exception as e:
        print(f"VOICE-BOX: !!! CRASHED: {e}")
        return None

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    chat_html = "".join([f'<div class="chat-msg {m["role"]}">{m["content"]}</div>' for m in convo_history[-10:]])
    emoji_map = {"idle": "😐", "happy": "😊", "excited": "🤩", "thinking": "🤔", "sleepy": "😴", "sad": "😢"}
    return f"""
    <html>
        <head>
            <title>Bit Control Center</title>
            <style>
                :root {{ --primary: #00ff88; --bg: #0a0a0a; --card: #161616; }}
                body {{ font-family: 'Segoe UI', sans-serif; background: var(--bg); color: white; margin: 0; display: flex; height: 100vh; overflow: hidden; }}
                .sidebar {{ width: 320px; background: var(--card); padding: 20px; border-right: 1px solid #333; display: flex; flex-direction: column; overflow-y: auto; }}
                .main {{ flex: 1; display: flex; flex-direction: column; padding: 20px; }}
                .card {{ background: #222; border-radius: 15px; padding: 20px; margin-bottom: 20px; border: 1px solid #333; }}
                h2 {{ color: var(--primary); margin-top: 0; font-size: 1.2em; border-bottom: 1px solid #333; padding-bottom: 10px; }}
                .emotion-view {{ font-size: 5em; text-align: center; margin: 10px 0; }}
                .chat-area {{ flex: 1; background: #111; border-radius: 15px; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; border: 1px solid #222; }}
                .chat-msg {{ padding: 10px 15px; border-radius: 12px; max-width: 80%; font-size: 0.95em; line-height: 1.4; }}
                .chat-msg.user {{ background: #333; align-self: flex-end; color: #eee; }}
                .chat-msg.assistant {{ background: var(--primary); color: black; align-self: flex-start; font-weight: 500; }}
                textarea {{ width: 100%; height: 100px; background: #000; color: #00ff88; border: 1px solid #444; border-radius: 8px; padding: 10px; font-family: monospace; }}
                button {{ background: var(--primary); color: black; border: none; padding: 12px; border-radius: 8px; font-weight: bold; cursor: pointer; width: 100%; margin-top: 10px; }}
                .label {{ color: #888; font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px; }}
            </style>
            <meta http-equiv="refresh" content="5">
        </head>
        <body>
            <div class="sidebar">
                <h2>🔋 System Stats</h2>
                <div class="card">
                    <div class="label">User: {user_name}</div>
                    <div class="emotion-view">{emoji_map.get(current_emotion, "🤖")}</div>
                    <div style="text-align:center; font-weight:bold; color:var(--primary)">{current_emotion.upper()}</div>
                </div>
                <form action="/update_prompt" method="post" class="card">
                    <div class="label">Personality Editor</div>
                    <textarea name="prompt">{system_prompt}</textarea>
                    <button type="submit">SAVE BRAIN</button>
                </form>
            </div>
            <div class="main">
                <h2>💬 Live Conversation History</h2>
                <div class="chat-area">{chat_html}</div>
            </div>
        </body>
    </html>
    """

@app.post("/update_prompt")
async def update_prompt(prompt: str = Form(...)):
    global system_prompt
    system_prompt = prompt
    return RedirectResponse(url="/", status_code=303)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    global last_interaction
    audio_data = bytearray()
    recording = False
    try:
        while True:
            try:
                message = await websocket.receive()
            except: break
            if "text" in message:
                cmd = message["text"]
                if cmd.startswith("QUERY:"):
                    text = cmd.replace("QUERY:", "").strip()
                    reply, emo = await get_ai_response(text)
                    await websocket.send_text(json.dumps({"text": reply, "emotion": emo}))
                    voice = await generate_speech(reply)
                    if voice:
                        # 2KB chunks and 0.05s sleep for Maximum ESP32 Stability
                        chunk_size = 2048
                        print(f"WS-SEND: Sending {len(voice)} bytes in 2KB chunks...")
                        for i in range(0, len(voice), chunk_size):
                            await websocket.send_bytes(voice[i:i + chunk_size])
                            await asyncio.sleep(0.05) 
                        print("WS-SEND: All chunks sent!")
                    else:
                        print("WS-SEND: !!! Skipped (No voice)")
                elif cmd == "START":
                    recording = True
                    audio_data = bytearray()
                elif cmd == "STOP":
                    recording = False
                    user_text = transcribe_audio_groq(audio_data)
                    if user_text.strip():
                        reply, emo = await get_ai_response(user_text)
                        await websocket.send_text(json.dumps({"text": reply, "emotion": emo}))
                        voice = await generate_speech(reply)
                        if voice:
                            chunk_size = 2048
                            print(f"WS-SEND: Sending {len(voice)} bytes in 2KB chunks...")
                            for i in range(0, len(voice), chunk_size):
                                await websocket.send_bytes(voice[i:i + chunk_size])
                                await asyncio.sleep(0.05)
                            print("WS-SEND: All chunks sent!")
                        else:
                            print("WS-SEND: !!! Skipped (No voice)")
            elif "bytes" in message and recording:
                audio_data.extend(message["bytes"])
    except WebSocketDisconnect: pass
    finally: print("Connection closed.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
