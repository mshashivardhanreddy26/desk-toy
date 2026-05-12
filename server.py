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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

convo_history = []
user_name = "Friend"
current_emotion = "idle"
system_prompt = """You are 'Bit', a cute emotional desk robot. 
Keep answers under 2 sentences. Format: {"emotion": "...", "text": "...", "user_name": "..."}"""

# --- UTILS ---
def clean_json(raw_res):
    try:
        json_match = re.search(r'\{.*\}', raw_res, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        return {"text": raw_res, "emotion": "happy", "user_name": None}
    except:
        return {"text": raw_res, "emotion": "thinking", "user_name": None}

async def get_ai_response(text):
    global convo_history, user_name, current_emotion
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "system", "content": f"The current user's name is {user_name}."})
    messages.extend(convo_history[-6:])
    messages.append({"role": "user", "content": text})

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={"model": "openrouter/free", "messages": messages},
                timeout=15.0
            )
            if response.status_code != 200:
                print(f"!!! OPENROUTER ERROR: {response.status_code} - {response.text}")
                return "My circuits are a bit tangled!", "sad"

            data = clean_json(response.json()['choices'][0]['message']['content'])
            
            ai_text = data.get("text", "I'm here!")
            current_emotion = data.get("emotion", "happy")
            if data.get("user_name"): user_name = data.get("user_name")
            
            convo_history.append({"role": "user", "content": text})
            convo_history.append({"role": "assistant", "content": ai_text})
            return ai_text, current_emotion
    except Exception as e:
        print(f"!!! SYSTEM ERROR: {e}")
        return "My circuits are a bit tangled!", "sad"

async def generate_speech(text):
    communicate = edge_tts.Communicate(text, "en-US-AndrewNeural", rate="-10%")
    mp3_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio": mp3_data.extend(chunk["data"])
    audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
    audio = audio.set_frame_rate(24000).set_channels(1).set_sample_width(2)
    return audio.raw_data

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    chat_html = "".join([f'<div class="chat-msg {m["role"]}">{m["content"]}</div>' for m in convo_history[-10:]])
    emoji_map = {"idle": "😐", "happy": "😊", "excited": "🤩", "thinking": "🤔", "sleepy": "😴", "sad": "😢"}
    
    return f"""
    <html>
        <head>
            <title>Bit Pro Dashboard</title>
            <style>
                :root {{ --primary: #00ff88; --bg: #0a0a0a; --card: #161616; }}
                body {{ font-family: 'Segoe UI', sans-serif; background: var(--bg); color: white; margin: 0; display: flex; height: 100vh; }}
                .sidebar {{ width: 300px; background: var(--card); padding: 20px; border-right: 1px solid #333; display: flex; flex-direction: column; }}
                .main {{ flex: 1; display: flex; flex-direction: column; padding: 20px; }}
                .card {{ background: #222; border-radius: 15px; padding: 20px; margin-bottom: 20px; border: 1px solid #333; }}
                h2 {{ color: var(--primary); margin-top: 0; font-size: 1.2em; }}
                .emotion-view {{ font-size: 5em; text-align: center; margin: 10px 0; }}
                .chat-area {{ flex: 1; background: #111; border-radius: 15px; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; border: 1px solid #222; }}
                .chat-msg {{ padding: 10px 15px; border-radius: 10px; max-width: 80%; font-size: 0.9em; }}
                .chat-msg.user {{ background: #333; align-self: flex-end; }}
                .chat-msg.assistant {{ background: var(--primary); color: black; align-self: flex-start; }}
                textarea {{ width: 100%; height: 100px; background: #000; color: #aaa; border: 1px solid #444; border-radius: 8px; padding: 10px; font-size: 0.8em; }}
                button {{ background: var(--primary); color: black; border: none; padding: 10px; border-radius: 8px; font-weight: bold; cursor: pointer; margin-top: 10px; }}
                .status {{ font-size: 0.8em; color: #888; }}
            </style>
            <meta http-equiv="refresh" content="5">
        </head>
        <body>
            <div class="sidebar">
                <h2>🤖 Bit Status</h2>
                <div class="card">
                    <div class="label">Current Emotion</div>
                    <div class="emotion-view">{emoji_map.get(current_emotion, "🤖")}</div>
                    <div style="text-align:center; text-transform:uppercase; font-weight:bold; color:var(--primary)">{current_emotion}</div>
                </div>
                <div class="card">
                    <div class="label">Remembered Name</div>
                    <div style="font-size: 1.5em; margin-top:10px;">{user_name}</div>
                </div>
                <form action="/update_prompt" method="post" class="card">
                    <div class="label">System Personality</div>
                    <textarea name="prompt">{system_prompt}</textarea>
                    <button type="submit">Update Brain</button>
                </form>
            </div>
            <div class="main">
                <h2>💬 Live Conversation</h2>
                <div class="chat-area">{chat_html}</div>
                <p class="status">Dashboard auto-updates every 5 seconds</p>
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
    global convo_history, current_emotion
    audio_data = bytearray()
    recording = False
    try:
        while True:
            message = await websocket.receive()
            if "text" in message:
                cmd = message["text"]
                if cmd.startswith("QUERY:"):
                    text = cmd.replace("QUERY:", "").strip()
                    reply, emo = await get_ai_response(text)
                    await websocket.send_text(json.dumps({"text": reply, "emotion": emo}))
                    await websocket.send_bytes(await generate_speech(reply))
                elif cmd == "START":
                    recording = True
                    audio_data = bytearray()
                elif cmd == "STOP":
                    recording = False
                    user_text = transcribe_audio_groq(audio_data)
                    if user_text.strip():
                        reply, emo = await get_ai_response(user_text)
                        await websocket.send_text(json.dumps({"text": reply, "emotion": emo}))
                        await websocket.send_bytes(await generate_speech(reply))
            elif "bytes" in message and recording:
                audio_data.extend(message["bytes"])
    except WebSocketDisconnect: pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
