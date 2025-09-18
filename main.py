# main.py
import os
import time
import logging
import asyncio
from typing import Dict, List
from uuid import uuid4
import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse, HTMLResponse
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()

# ======= Logging (quiet) =======
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("alertbuddy")

app = FastAPI()

# -------------------
# MongoDB setup
# -------------------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(MONGO_URI)
db = client.alertbuddy

# Nightbot OAuth config (unchanged)
CLIENT_ID = os.environ.get("NIGHTBOT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("NIGHTBOT_CLIENT_SECRET")
SCOPES = os.environ.get("NIGHTBOT_SCOPES", "commands%20song_requests")
ANDROID_REDIRECT_URI = os.environ.get("ANDROID_REDIRECT_URI", "http://localhost:8000/nightbot/callback")
ANDROID_DEEP_LINK = os.environ.get("ANDROID_DEEP_LINK", "alertbuddy://nightbot/android/callback")
# Defaults for audio and gif (as requested)
DEFAULT_AUDIO = "https://commondatastorage.googleapis.com/codeskulptor-assets/week7-brrring.m4a"
DEFAULT_GIF = "https://i.giphy.com/LdOyjZ7io5Msw.gif"

# -------------------
# WebSocket tracking (ephemeral)
# -------------------
active_connections: Dict[str, List[WebSocket]] = {}
connections_lock = asyncio.Lock()

# -------------------
# Helpers: offload blocking DB ops
# -------------------
async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

async def db_find_user_by_puuid(puuid: str):
    return await run_blocking(db.users.find_one, {"puuid": puuid})

async def db_insert_notification(doc: dict):
    return await run_blocking(db.notifications.insert_one, doc)

async def db_find_undelivered(puuid: str):
    return await run_blocking(lambda: list(db.notifications.find({"puuid": puuid, "delivered": False}).sort("createdAt", 1)))

async def db_mark_delivered(object_id):
    return await run_blocking(db.notifications.update_one, {"_id": object_id}, {"$set": {"delivered": True, "deliveredAt": int(time.time())}})

# -------------------
# OAuth endpoints (unchanged)
# -------------------
@app.get("/nightbot/android/login")
async def nightbot_login(client_nonce: str):
    state = str(uuid4())
    await run_blocking(db.oauth_states.insert_one, {"state": state, "client_nonce": client_nonce, "created_at": int(time.time())})
    auth_url = (
        f"https://api.nightbot.tv/oauth2/authorize?"
        f"response_type=code&client_id={CLIENT_ID}&"
        f"redirect_uri={ANDROID_REDIRECT_URI}&scope={SCOPES}&state={state}"
    )
    return RedirectResponse(url=auth_url)

@app.get("/nightbot/android/callback")
async def nightbot_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    oauth_state = await run_blocking(db.oauth_states.find_one, {"state": state})
    if not oauth_state:
        raise HTTPException(status_code=400, detail="Invalid or expired state")
    client_nonce = oauth_state["client_nonce"]

    # Exchange code for token
    token_resp = httpx.post(
        "https://api.nightbot.tv/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ANDROID_REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10
    )
    if token_resp.status_code != 200:
        logger.warning("Token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        raise HTTPException(status_code=400, detail="Token exchange failed")
    token_data = token_resp.json()
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    expires_at = int(time.time()) + expires_in

    # Fetch channel
    channel_resp = httpx.get("https://api.nightbot.tv/1/channel", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    if channel_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch channel info")
    channel = channel_resp.json()["channel"]
    nightbot_id = channel["_id"]

    existing_user = await run_blocking(db.users.find_one, {"nightbot_id": nightbot_id})
    puuid = existing_user["puuid"] if existing_user else str(uuid4())

    # Allow extra fields for gif/audio (can be set by admin or later)
    user_doc = {
        "puuid": puuid,
        "nightbot_id": nightbot_id,
        "provider": channel.get("provider"),
        "providerId": channel.get("providerId"),
        "channel": {
            "name": channel.get("name"),
            "displayName": channel.get("displayName"),
            "avatar": channel.get("avatar"),
            "joined": channel.get("joined"),
            "admin": channel.get("admin"),
            "botId": channel.get("botId"),
        },
        # Optional custom alert assets (if absent use defaults)
        "alert_gif": channel.get("alert_gif") or None,
        "alert_audio": channel.get("alert_audio") or None,
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at
        },
        "updatedAt": int(time.time())
    }
    await run_blocking(db.users.update_one, {"nightbot_id": nightbot_id}, {"$set": user_doc}, True)
    await run_blocking(db.oauth_states.delete_one, {"state": state})

    deep_link = (
        f"{ANDROID_DEEP_LINK}?puuid={puuid}&client_nonce={client_nonce}"
        f"&displayName={channel.get('displayName')}"
        f"&avatar={channel.get('avatar')}"
    )
    return RedirectResponse(url=deep_link)

# -------------------
# Notification endpoint (filters titles with ₹, stores doc with gif/audio)
# -------------------
@app.post("/notifications")
async def receive_notification(request: Request):
    data = await request.json()
    puuid = data.get("puuid")
    title = (data.get("title") or "")
    text = data.get("text") or ""

    if not puuid:
        raise HTTPException(status_code=400, detail="Missing puuid")

    # Only process titles containing ₹
    if "₹" not in title:
        # ignore silently (or return 204)
        return JSONResponse({"status": "ignored", "reason": "title missing ₹"}, status_code=204)

    user = await db_find_user_by_puuid(puuid)
    if not user:
        raise HTTPException(status_code=404, detail="PUUID not found")

    # Convert "paid you" -> "donated" (case-insensitive)
    processed_title = title
    try:
        # make first letter uppercase for each word
        processed_title = " ".join([w.capitalize() for w in processed_title.split(" ")])
        processed_title = processed_title.replace("paid you", "donated").replace("Paid You", "donated")
    except Exception:
        pass

    # Build payload to save & broadcast (include user alert assets or defaults)
    payload = {
        "puuid": puuid,
        "title": processed_title,
        "text": text,
        "packageName": data.get("packageName"),
        "timestamp": data.get("timestamp", int(time.time() * 1000)),
        "alert_gif": user.get("alert_gif") if user.get("alert_gif") else DEFAULT_GIF,
        "alert_audio": user.get("alert_audio") if user.get("alert_audio") else DEFAULT_AUDIO
    }

    # Save to DB (delivered False)
    doc = {"puuid": puuid, "notification": payload, "delivered": False, "createdAt": int(time.time())}
    insert_result = await db_insert_notification(doc)

    # Broadcast immediately to active sockets (if any)
    async with connections_lock:
        sockets = active_connections.get(puuid, []).copy()
    if sockets:
        # attempt to send to all; if any succeed, mark delivered in DB
        delivered_any = False
        for ws in sockets:
            try:
                await ws.send_json(payload)
                delivered_any = True
            except Exception:
                pass
        if delivered_any:
            await db_mark_delivered(insert_result.inserted_id)

    return JSONResponse({"status": "saved", "id": str(insert_result.inserted_id)})

# -------------------
# WebSocket endpoint
# -------------------
@app.websocket("/alerts/{puuid}")
async def alerts_ws(websocket: WebSocket, puuid: str):
    # validate
    user = await db_find_user_by_puuid(puuid)
    if not user:
        await websocket.close(code=4000)
        return

    await websocket.accept()
    async with connections_lock:
        lst = active_connections.setdefault(puuid, [])
        lst.append(websocket)

    # send undelivered one-by-one (server side still sends them; client will queue one-by-one)
    try:
        pending = await db_find_undelivered(puuid)
        for notif in pending:
            try:
                await websocket.send_json(notif["notification"])
                await db_mark_delivered(notif["_id"])
            except Exception:
                pass

        # keep connection alive
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                await asyncio.sleep(0.1)
    finally:
        async with connections_lock:
            lst = active_connections.get(puuid, [])
            if websocket in lst:
                lst.remove(websocket)
            if not lst:
                active_connections.pop(puuid, None)

# -------------------
# Serve alerts HTML (observes puuid exists)
# -------------------
FRONTEND_PATH = os.path.join(os.path.dirname(__file__), "frontend", "alerts.html")

@app.get("/alerts/view/{puuid}")
async def view_alerts(puuid: str):
    user = await db_find_user_by_puuid(puuid)
    if not user:
        return HTMLResponse("<h1>Invalid PUUID</h1><p>This PUUID does not exist.</p>", status_code=404)
    # serve static file; page reads puuid from URL path
    return FileResponse(FRONTEND_PATH, media_type="text/html")

# -------------------
# Optional: debug endpoint (counts)
# -------------------
@app.get("/debug/connections")
async def debug_connections():
    async with connections_lock:
        info = {puuid: len(sockets) for puuid, sockets in active_connections.items()}
    return JSONResponse({"active_connections": info})
