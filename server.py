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

app = FastAPI()

# --- CONFIGURATION (Set these in Render's Environment Variables) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

convo_history = []
SYSTEM_PROMPT = "You are a cute emotional desk robot. Speak briefly (1-2 sentences)."

# Global variables for the Dashboard
last_interaction = {"user": "None yet", "ai": "Waiting for robot..."}

@app.get("/")
async def root():
    from fastapi.responses import HTMLResponse
    html_content = f"""
    <html>
        <head>
            <title>Robot Dashboard</title>
            <style>
                body {{ font-family: sans-serif; background: #121212; color: white; text-align: center; padding: 50px; }}
                .box {{ background: #1e1e1e; padding: 20px; border-radius: 15px; display: inline-block; min-width: 300px; border: 1px solid #333; }}
                h1 {{ color: #00ff88; }}
                .label {{ color: #888; font-size: 0.8em; margin-bottom: 5px; }}
                .text {{ font-size: 1.2em; margin-bottom: 20px; color: #fff; }}
            </style>
            <meta http-equiv="refresh" content="3"> 
        </head>
        <body>
            <h1>Robot Brain Dashboard</h1>
            <div class="box">
                <div class="label">YOU SAID:</div>
                <div class="text">"{last_interaction['user']}"</div>
                <hr style="border: 0; border-top: 1px solid #333;">
                <div class="label">ROBOT REPLIED:</div>
                <div class="text" style="color: #00ff88;">{last_interaction['ai']}</div>
            </div>
            <p style="color: #444; font-size: 0.8em;">Page auto-refreshes every 3 seconds</p>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

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
    global convo_history
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *convo_history[-4:], {"role": "user", "content": text}]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={"model": "openrouter/free", "messages": messages},
                timeout=10.0
            )
            if response.status_code != 200:
                print(f"OpenRouter Error: {response.status_code} - {response.text}")
                return "My brain feels a bit slow right now."
            
            ai_text = response.json()['choices'][0]['message']['content']
            convo_history.append({"role": "user", "content": text})
            convo_history.append({"role": "assistant", "content": ai_text})
            return ai_text
    except Exception as e:
        print(f"AI Connection Error: {e}")
        return "I'm having trouble connecting to my brain."

async def generate_speech(text):
    # Use Edge-TTS for a very natural voice with -15% speed
    voice = "en-US-AnaNeural" 
    communicate = edge_tts.Communicate(text, voice, rate="-40%")
    
    mp3_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data.extend(chunk["data"])
    
    # Convert MP3 to PCM
    audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
    audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
    return audio.raw_data

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
                    # Direct text test (skips STT)
                    user_text = command.replace("QUERY:", "").strip()
                    print(f"Test Query: {user_text}")
                    ai_text = await get_ai_response(user_text)
                    
                    last_interaction["user"] = user_text
                    last_interaction["ai"] = ai_text
                    
                    await websocket.send_text(ai_text)
                    pcm_voice = await generate_speech(ai_text)
                    await websocket.send_bytes(pcm_voice)
                elif command == "START":
                    recording = True
                    audio_data = bytearray()
                elif command == "STOP":
                    recording = False
                    user_text = transcribe_audio_groq(audio_data)
                    if user_text.strip():
                        ai_text = await get_ai_response(user_text)
                        
                        # Update the Dashboard
                        last_interaction["user"] = user_text
                        last_interaction["ai"] = ai_text
                        
                        # 1. Send the text response back first
                        await websocket.send_text(ai_text)
                        
                        # 2. Generate and send the voice response
                        pcm_voice = await generate_speech(ai_text)
                        await websocket.send_bytes(pcm_voice)
            
            elif "bytes" in message and recording:
                audio_data.extend(message["bytes"])
    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
