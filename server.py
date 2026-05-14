import os
import io
import json
import httpx
import random
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from firebase_admin import firestore
import firebase_admin
from firebase_admin import credentials
import edge_tts
import re

app = FastAPI(title="Desk Toy Professional SaaS")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELS ---
class RegisterRequest(BaseModel):
    user_id: str
    registration_code: str

class ChatRequest(BaseModel):
    device_id: str
    text: str

# --- STATE ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
pending_registrations = {} # { "code": "temp_device_id" }
_db = None

# --- FIREBASE SERVICE ---
def get_db():
    global _db
    if _db: return _db
    try:
        if not firebase_admin._apps:
            cred_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_STR")
            if cred_json:
                cred = credentials.Certificate(json.loads(cred_json))
                firebase_admin.initialize_app(cred, {'projectId': os.getenv("FIREBASE_PROJECT_ID")})
                _db = firestore.client()
        return _db
    except Exception as e:
        print(f"Firebase Error: {e}")
        return None

# --- AI SERVICE ---
async def save_live_message(device_id, role, text, emotion="idle"):
    db = get_db()
    if not db: return
    try:
        db.collection('devices').document(device_id).collection('messages').add({
            "role": role, "text": text, "emotion": emotion, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except: pass

async def get_user_data(device_id):
    db = get_db()
    if not db: return None
    try:
        device_ref = db.collection('devices').document(device_id).get()
        if not device_ref.exists: return None
        user_id = device_ref.to_dict().get('user_id')
        user_ref = db.collection('users').document(user_id).get()
        if not user_ref.exists:
            data = {"name": "Friend", "voice": "en-US-AndrewNeural", "history": [], "system_prompt": "You are 'Bit', a cute robot."}
            db.collection('users').document(user_id).set(data)
            return {**data, "user_id": user_id}
        return {**user_ref.to_dict(), "user_id": user_id}
    except: return None

async def get_ai_response(text, user_data, device_id):
    await save_live_message(device_id, "user", text)
    history = user_data.get("history", [])
    system_prompt = user_data.get("system_prompt")
    memory = user_data.get("memory", "")
    
    messages = [{"role": "system", "content": f"{system_prompt}\nUser memory: {memory}. Format response as JSON: {{'text': '...', 'emotion': '...', 'new_fact': '...'}}"}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": text})

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile", "messages": messages},
                timeout=15.0
            )
            data = res.json()
            raw_content = data['choices'][0]['message']['content']
            # Simple extraction
            match = re.search(r'\{.*\}', raw_content, re.DOTALL)
            parsed = json.loads(match.group(0)) if match else {"text": raw_content, "emotion": "happy"}
            
            ai_text = parsed.get("text", "I'm here!")
            emotion = parsed.get("emotion", "happy")
            
            await save_live_message(device_id, "assistant", ai_text, emotion)
            
            # Save history (mock for now, usually you'd update Firestore)
            return ai_text, emotion
    except:
        return "My brain is buzzing!", "thinking"

async def generate_speech(text, voice_name):
    try:
        communicate = edge_tts.Communicate(text, voice_name or "en-US-AndrewNeural")
        mp3_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio": mp3_data.extend(chunk["data"])
        return mp3_data
    except: return None

# --- ROUTES ---

@app.get("/")
async def root():
    return {"status": "online", "system": "Desk Toy SaaS", "db": "ready" if get_db() else "offline"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    device_id = websocket.query_params.get("device_id", "DEFAULT")
    await websocket.accept()
    
    if device_id == "NEW_PRODUCT":
        reg_code = f"{random.randint(100000, 999999)}"
        temp_id = f"PRODUCT-{random.randint(1000, 9999)}"
        pending_registrations[reg_code] = temp_id
        await websocket.send_text(json.dumps({"type": "REGISTRATION_CODE", "code": reg_code, "text": f"Code: {reg_code}"}))
        device_id = temp_id

    user_data = await get_user_data(device_id) or {"name": "Friend", "voice": "en-US-AndrewNeural", "user_id": "temp"}
    
    try:
        while True:
            msg = await websocket.receive()
            if "text" in msg:
                query = msg["text"]
                if query.startswith("QUERY:"):
                    text = query.replace("QUERY:", "").strip()
                    reply, emo = await get_ai_response(text, user_data, device_id)
                    await websocket.send_text(json.dumps({"text": reply, "emotion": emo}))
                    voice = await generate_speech(reply, user_data.get("voice"))
                    if voice:
                        for i in range(0, len(voice), 2048):
                            await websocket.send_bytes(voice[i:i+2048])
                            await asyncio.sleep(0.05)
    except: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
