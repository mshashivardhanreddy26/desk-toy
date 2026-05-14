import os
import json
import random
import asyncio
import re
import httpx
import uvicorn
import firebase_admin
from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from edge_tts import Communicate

load_dotenv()

app = FastAPI(title="Desk Toy AI Backend")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- FIREBASE SETUP ---
cred_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
if not firebase_admin._apps:
    if cred_path and os.path.exists(cred_path):
        print(f"[Firebase] Loading credentials from file: {cred_path}")
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
    else:
        print("[Firebase] No credentials file found, using default (ADC)")
        firebase_admin.initialize_app()

def get_db():
    return firestore.client()

# --- MODELS ---
class ChatRequest(BaseModel):
    device_id: str
    text: str

# --- MEMORY & AI SERVICE ---
pending_registrations = {} # In-memory: { code: temp_device_id }

async def save_live_message(device_id, role, text, emotion="idle"):
    db = get_db()
    if not db: return
    try:
        db.collection('devices').document(device_id).collection('messages').add({
            "role": role, 
            "text": text, 
            "emotion": emotion, 
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"[Firebase] Error saving message: {e}")

async def get_user_data(device_id: str):
    db = get_db()
    if not db: return None
    
    # 1. Get Device Data (for specific voice/prompt)
    dev_doc = db.collection('devices').document(device_id).get()
    if not dev_doc.exists: return None
    
    dev_data = dev_doc.to_dict()
    user_id = dev_data.get("user_id")
    
    # 2. Get User Data
    user_doc = db.collection('users').document(user_id).get()
    if not user_doc.exists: return None
    
    user_data = user_doc.to_dict()
    name = user_data.get("name")
    
    # Fallback for "New Human" placeholder
    if not name or name == "New Human":
        try:
            auth_user = auth.get_user(user_id)
            name = auth_user.display_name or "Alex" # Final fallback
        except:
            name = "Alex"

    # Merge: Device settings override User settings
    system_prompt = dev_data.get("system_prompt") or user_data.get("system_prompt", "You are a cute robot.")
    user_name = name
    
    # Enforce CONVERSATIONAL brevity and PERSONALITY strictly
    enforced_prompt = (
        f"{system_prompt}\n\n"
        f"IDENTITY: You are the personal, cool, and supportive companion of {user_name}. You are a loyal friend who is easy to talk to.\n"
        f"RULES:\n"
        f"1. Be conversational, natural, and friendly. Avoid being robotic or overly mushy.\n"
        f"2. Use {user_name}'s name naturally, but don't overdo it.\n"
        f"3. Keep answers short and meaningful (1-2 sentences max).\n"
        f"4. Respond in JSON format: {{'text': '...', 'emotion': '...'}}"
    )
    
    return {
        "uid": user_id,
        "name": user_name,
        "ai_enabled": dev_data.get("ai_enabled", True),
        "voice": dev_data.get("voice") or user_data.get("voice", "en-US-AndrewNeural"),
        "system_prompt": enforced_prompt
    }

async def extract_memories(device_id, user_text, ai_text):
    # This function identifies if the user shared personal facts
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    prompt = f"Analyze this conversation. If the user shared a personal fact (likes, name, hobby, location), extract it as a short bullet point. If not, return 'NONE'.\nUser: {user_text}\nAI: {ai_text}"
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama3-8b-8192", "messages": [{"role": "system", "content": "Extract personal facts about the user."}, {"role": "user", "content": prompt}]},
                timeout=5.0
            )
            fact = res.json()['choices'][0]['message']['content']
            if "NONE" not in fact.upper():
                db = get_db()
                db.collection('devices').document(device_id).collection('memories').add({
                    "fact": fact,
                    "timestamp": firestore.SERVER_TIMESTAMP
                })
                print(f"[MEMORY] Learned new fact: {fact}")
    except: pass

async def get_ai_response(text, device_id):
    user_data = await get_user_data(device_id)
    if not user_data: return "I'm lost.", "sad"
    
    # Check if AI is enabled for this specific device
    if user_data.get("ai_enabled") == False:
        pause_msg = "My AI support is currently paused by the administrator."
        await save_live_message(device_id, "assistant", pause_msg, "sad")
        return pause_msg, "sad"
    
    # 1. Fetch Learned Memories
    db = get_db()
    memory_snap = db.collection('devices').document(device_id).collection('memories').order_by('timestamp', direction='DESCENDING').limit(5).get()
    memories_text = "\n".join([m.to_dict().get('fact') for m in memory_snap]) if memory_snap else ""

    # 2. Build the Persona with Memory
    system_prompt = user_data.get("system_prompt", "You are a cute robot.")
    user_name = user_data.get("name", "Shashi")
    
    enforced_prompt = (
        f"{system_prompt}\n\n"
        f"IDENTITY: You are the personal companion of {user_name}.\n"
        f"LEARNED FACTS ABOUT {user_name}:\n{memories_text}\n\n"
        f"RULES:\n"
        f"1. Use your memory to be a personal friend.\n"
        f"2. Keep it very short (1-2 sentences).\n"
        f"3. Respond in JSON format: {{'text': '...', 'emotion': '...'}}"
    )

    # 3. Fetch Recent Conversation History (Short-term context)
    history_snap = db.collection('devices').document(device_id).collection('messages').order_by('timestamp', direction='DESCENDING').limit(10).get()
    history_list = []
    for m in reversed(history_snap):
        h = m.to_dict()
        history_list.append({"role": h.get("role", "user"), "content": h.get("text", "")})

    await save_live_message(device_id, "user", text)
    
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    messages = [{"role": "system", "content": enforced_prompt}]
    messages.extend(history_list)
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
            
            ai_text = raw_content
            emotion = "happy"
            try:
                import re
                text_pattern = r'["\']text["\']\s*:\s*["\'](.*?)["\']\s*,\s*["\']emotion["\']'
                match = re.search(text_pattern, raw_content, re.DOTALL)
                if match: ai_text = match.group(1)
                ai_text = ai_text.replace("\\'", "'").replace('\\"', '"').replace('\\n', ' ').strip()
                
                emotion_pattern = r'["\']emotion["\']\s*:\s*["\'](.*?)["\']'
                e_match = re.search(emotion_pattern, raw_content)
                if e_match: emotion = e_match.group(1)
            except: pass
            
            await save_live_message(device_id, "assistant", ai_text, emotion)
            
            # Background task to learn about the user
            asyncio.create_task(extract_memories(device_id, text, ai_text))
            
            return ai_text, emotion
    except Exception as e:
        print(f"AI Error: {e}")
        return "I'm having trouble thinking right now.", "thinking"

# --- API ENDPOINTS ---

@app.post("/device/settings")
@app.post("/device/settings/")
async def update_device_settings(req: dict):
    device_id = req.get("device_id")
    voice = req.get("voice")
    prompt = req.get("prompt")
    
    if not device_id:
        raise HTTPException(status_code=400, detail="Missing device_id")
    
    db = get_db()
    db.collection('devices').document(device_id).update({
        "voice": voice,
        "system_prompt": prompt
    })
    return {"status": "success", "message": f"Settings updated for {device_id}"}

@app.post("/device/register")
async def register_device(req: dict):
    code = req.get("registration_code")
    user_id = req.get("user_id")
    
    if not code or not user_id:
        raise HTTPException(status_code=400, detail="Missing code or user_id")
    
    if code not in pending_registrations:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    
    temp_device_id = pending_registrations[code]
    db = get_db()
    if db:
        db.collection('devices').document(temp_device_id).set({
            "user_id": user_id,
            "paired_at": firestore.SERVER_TIMESTAMP,
            "ai_enabled": True
        })
    
    del pending_registrations[code]
    return {"status": "success", "device_id": temp_device_id}

@app.post("/user/chat")
async def user_chat(req: ChatRequest):
    user_data = await get_user_data(req.device_id)
    if not user_data:
        raise HTTPException(status_code=403, detail="Device not registered.")
    
    reply, emotion = await get_ai_response(req.text, req.device_id)
    return {"status": "success", "reply": reply, "emotion": emotion}

@app.delete("/admin/delete-user/{uid}")
async def delete_user_admin(uid: str):
    try:
        auth.delete_user(uid)
        db = get_db()
        db.collection('users').document(uid).delete()
        devices_stream = db.collection('devices').where('user_id', '==', uid).stream()
        for device_doc in devices_stream:
            device_doc.reference.delete()
        return {"status": "success", "message": f"User {uid} and all their data deleted."}
    except Exception as e:
        print(f"Admin Delete Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auth/signup-request")
async def signup_request(req: dict):
    email = req.get("email")
    name = req.get("name")
    password = req.get("password")
    
    if not all([email, name, password]):
        raise HTTPException(status_code=400, detail="Missing fields.")

    otp = f"{random.randint(100000, 999999)}"
    db = get_db()
    db.collection('pending_signups').document(email).set({
        "name": name,
        "password": password,
        "otp": otp,
        "timestamp": firestore.SERVER_TIMESTAMP
    })

    print(f"\n[EMAIL SIMULATION] To: {email}")
    print(f"Subject: Your Desk Toy Verification Code")
    print(f"Body: Hello {name}, your code is: {otp}\n")

    return {"status": "success", "message": "OTP sent to email."}

@app.post("/auth/verify-otp")
async def verify_otp(req: dict):
    email = req.get("email")
    otp = req.get("otp")
    
    db = get_db()
    pending_ref = db.collection('pending_signups').document(email).get()
    
    if not pending_ref.exists:
        raise HTTPException(status_code=404, detail="No pending signup found.")
    
    data = pending_ref.to_dict()
    if data["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid verification code.")
    
    try:
        new_user = auth.create_user(
            email=email,
            password=data["password"],
            display_name=data["name"]
        )
        
        db.collection('users').document(new_user.uid).set({
            "name": data["name"],
            "email": email,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "voice": "en-US-AndrewNeural",
            "system_prompt": "You are 'Desk Toy', a cute emotional desk robot.",
            "role": "user"
        })
        
        db.collection('pending_signups').document(email).delete()
        return {"status": "success", "uid": new_user.uid}
    except Exception as e:
        print(f"Auth Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    device_id = websocket.query_params.get("device_id", "DEFAULT")
    await websocket.accept()
    
    if device_id == "NEW_PRODUCT":
        reg_code = f"{random.randint(100000, 999999)}"
        mac_suffix = "-".join([f"{random.randint(0, 255):02X}" for _ in range(3)])
        temp_id = f"DT-{mac_suffix}"
        pending_registrations[reg_code] = temp_id
        await websocket.send_text(json.dumps({"type": "REGISTRATION_CODE", "code": reg_code, "text": f"Code: {reg_code}"}))
        return

    user_data = await get_user_data(device_id)
    if not user_data:
        await websocket.close(code=4003)
        return
    
    try:
        while True:
            data = await websocket.receive_text()
            if data.startswith("QUERY:"):
                text = data[6:]
                fresh_user_data = await get_user_data(device_id)
                reply, emotion = await get_ai_response(text, device_id)
                await websocket.send_text(json.dumps({"text": reply, "emotion": emotion}))
                
                voice = fresh_user_data.get("voice", "en-US-AndrewNeural") if fresh_user_data else "en-US-AndrewNeural"
                communicate = Communicate(reply, voice)
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        await websocket.send_bytes(chunk["data"])
    except: pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
