import os
import io
import json
import wave
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import edge_tts
from pydub import AudioSegment
import uvicorn
import asyncio
import re

app = FastAPI()

# --- CONFIGURATION ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Memory State
convo_history = []
user_name = "Friend"
last_interaction = {"user": "None yet", "ai": "Waiting...", "emotion": "idle"}

SYSTEM_PROMPT = """
You are a cute emotional desk robot companion for children.
Always answer briefly using simple language.
Never answer NSFW or harmful questions.
Be playful, warm, and emotionally expressive.
Keep responses under 2 sentences.

If the user tells you their name, remember it and use it in future responses to be more personal.
Format your response exactly as JSON:
{
  "emotion": "one of: idle, happy, excited, thinking, sleepy, sad",
  "text": "your spoken response",
  "user_name": "If the user just told you their name, put it here. Otherwise, leave as null."
}
"""

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
    global convo_history, user_name, last_interaction
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "system", "content": f"The current user's name is {user_name}."})
    messages.extend(convo_history[-4:])
    messages.append({"role": "user", "content": text})

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": "openrouter/free", 
                    "messages": messages
                },
                timeout=15.0
            )
            
            if response.status_code != 200:
                print(f"API Error: {response.status_code} {response.text}")
                return "My brain is a bit fuzzy!", "thinking"

            raw_res = response.json()['choices'][0]['message']['content']
            
            # --- ROBUST JSON CLEANING ---
            # Sometimes AI adds ```json ... ``` blocks, we need to strip them
            json_match = re.search(r'\{.*\}', raw_res, re.DOTALL)
            if json_match:
                clean_json = json_match.group(0)
                data = json.loads(clean_json)
            else:
                # If AI didn't return JSON, fallback
                data = {"text": raw_res, "emotion": "happy"}

            ai_text = data.get("text", "Hello!")
            emotion = data.get("emotion", "happy")
            new_name = data.get("user_name")
            
            if new_name and new_name != "null" and len(new_name) < 20:
                user_name = new_name
            
            convo_history.append({"role": "user", "content": text})
            convo_history.append({"role": "assistant", "content": ai_text})
            
            last_interaction["user"] = text
            last_interaction["ai"] = ai_text
            last_interaction["emotion"] = emotion
            
            return ai_text, emotion
            
    except Exception as e:
        print(f"AI Processing Error: {e}")
        return "I'm having a little nap right now!", "sleepy"

async def generate_speech(text):
    # Upgraded to 24000Hz for Crystal Clear Voice
    voice = "en-US-AndrewNeural" 
    communicate = edge_tts.Communicate(text, voice, rate="-10%")
    mp3_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data.extend(chunk["data"])
    
    audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
    audio = audio.set_frame_rate(24000).set_channels(1).set_sample_width(2)
    return audio.raw_data

@app.get("/")
async def root():
    from fastapi.responses import HTMLResponse
    emoji_map = {"idle": "😐", "happy": "😊", "excited": "🤩", "thinking": "🤔", "sleepy": "😴", "sad": "😢"}
    emo_icon = emoji_map.get(last_interaction['emotion'], "🤖")
    
    html_content = f"""
    <html>
        <head>
            <title>Bit Dashboard</title>
            <style>
                body {{ font-family: sans-serif; background: #121212; color: white; text-align: center; padding: 50px; }}
                .box {{ background: #1e1e1e; padding: 20px; border-radius: 15px; display: inline-block; min-width: 350px; border: 1px solid #333; }}
                .emotion {{ font-size: 4em; margin-bottom: 10px; }}
                h1 {{ color: #00ff88; margin-top: 0; }}
                .label {{ color: #888; font-size: 0.8em; margin-bottom: 5px; text-transform: uppercase; }}
                .text {{ font-size: 1.2em; margin-bottom: 20px; color: #fff; line-height: 1.4; }}
                .name-tag {{ background: #00ff88; color: black; padding: 5px 15px; border-radius: 20px; font-weight: bold; font-size: 0.8em; }}
            </style>
            <meta http-equiv="refresh" content="3"> 
        </head>
        <body>
            <div class="name-tag">Talking to: {user_name}</div>
            <h1>Bit the Robot</h1>
            <div class="box">
                <div class="emotion">{emo_icon}</div>
                <div class="label">You said:</div>
                <div class="text">"{last_interaction['user']}"</div>
                <hr style="border: 0; border-top: 1px solid #333; margin: 20px 0;">
                <div class="label">Bit says:</div>
                <div class="text" style="color: #00ff88;">{last_interaction['ai']}</div>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    global last_interaction
    audio_data = bytearray()
    recording = False

    try:
        while True:
            message = await websocket.receive()
            if "text" in message:
                command = message["text"]
                if command.startswith("QUERY:"):
                    user_text = command.replace("QUERY:", "").strip()
                    ai_text, emotion = await get_ai_response(user_text)
                    await websocket.send_text(json.dumps({"text": ai_text, "emotion": emotion}))
                    pcm_voice = await generate_speech(ai_text)
                    await websocket.send_bytes(pcm_voice)
                elif command == "START":
                    recording = True
                    audio_data = bytearray()
                elif command == "STOP":
                    recording = False
                    user_text = transcribe_audio_groq(audio_data)
                    if user_text.strip():
                        ai_text, emotion = await get_ai_response(user_text)
                        await websocket.send_text(json.dumps({"text": ai_text, "emotion": emotion}))
                        pcm_voice = await generate_speech(ai_text)
                        await websocket.send_bytes(pcm_voice)
            elif "bytes" in message and recording:
                audio_data.extend(message["bytes"])
    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
