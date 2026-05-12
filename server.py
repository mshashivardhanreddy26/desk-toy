import os
import io
import json
import wave
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from gtts import gTTS
from pydub import AudioSegment
import uvicorn

app = FastAPI()

# --- CONFIGURATION (Set these in Render's Environment Variables) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

convo_history = []
SYSTEM_PROMPT = "You are a cute emotional desk robot. Speak briefly (1-2 sentences)."

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

def get_ai_response(text):
    global convo_history
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *convo_history[-4:], {"role": "user", "content": text}]
    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={"model": "openrouter/free", "messages": messages},
            timeout=10.0
        )
        ai_text = response.json()['choices'][0]['message']['content']
        convo_history.append({"role": "user", "content": text})
        convo_history.append({"role": "assistant", "content": ai_text})
        return ai_text
    except:
        return "I'm having trouble thinking."

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("ESP32 Connected")
    audio_data = bytearray()
    recording = False

    try:
        while True:
            # Receive data (can be text command or binary audio)
            message = await websocket.receive()
            
            if "text" in message:
                command = message["text"]
                if command == "START":
                    print("Recording...")
                    recording = True
                    audio_data = bytearray()
                elif command == "STOP":
                    print("Processing...")
                    recording = False
                    user_text = transcribe_audio_groq(audio_data)
                    if user_text.strip():
                        ai_text = get_ai_response(user_text)
                        
                        # Generate TTS and convert to PCM
                        tts = gTTS(text=ai_text, lang='en')
                        mp3_fp = io.BytesIO()
                        tts.write_to_fp(mp3_fp)
                        mp3_fp.seek(0)
                        
                        audio = AudioSegment.from_file(mp3_fp, format="mp3")
                        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
                        
                        # Send back raw PCM bytes
                        await websocket.send_bytes(audio.raw_data)
            
            elif "bytes" in message and recording:
                audio_data.extend(message["bytes"])

    except WebSocketDisconnect:
        print("ESP32 Disconnected")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
