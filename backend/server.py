"""
WartinLabs Voice Agent – FastAPI Server
────────────────────────────────────────
• POST /start-session  → create Daily room, spawn bot, return room_url + token
• POST /end-session/{id}
• WS   /ws/{id}        → real-time transcript events to the frontend
• GET  /               → serves frontend/index.html (static)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

# ── path setup ───────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / ".env")

# ── app ──────────────────────────────────────────────────────
app = FastAPI(title="WartinLabs Voice Agent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── in-memory state ──────────────────────────────────────────
ws_clients:  Dict[str, WebSocket]    = {}
bot_tasks:   Dict[str, asyncio.Task] = {}


# ─────────────────────────────────────────────────────────────
# WebSocket – real-time transcript feed
# ─────────────────────────────────────────────────────────────
@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    ws_clients[session_id] = websocket
    logger.info(f"WS connected: {session_id}")
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.pop(session_id, None)
        logger.info(f"WS disconnected: {session_id}")


async def _emit(event: str, data: dict):
    sid = data.get("session_id", "")
    ws  = ws_clients.get(sid)
    if ws:
        try:
            await ws.send_text(json.dumps({"type": event, "data": data}))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Daily helpers
# ─────────────────────────────────────────────────────────────
async def _daily_post(path: str, body: dict) -> dict:
    key = os.environ.get("DAILY_API_KEY", "")
    if not key:
        raise HTTPException(500, "DAILY_API_KEY not configured")
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"https://api.daily.co/v1/{path}",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=body,
        ) as r:
            if r.status not in (200, 201):
                txt = await r.text()
                raise HTTPException(502, f"Daily error [{r.status}]: {txt}")
            return await r.json()


async def _create_room() -> dict:
    return await _daily_post("rooms", {
        "properties": {
            "max_participants": 2,
            "exp": int(time.time()) + 3600,
            "enable_prejoin_ui": False,
        }
    })


async def _make_token(room_name: str, owner: bool) -> str:
    data = await _daily_post("meeting-tokens", {
        "properties": {
            "room_name": room_name,
            "is_owner": owner,
            "exp": int(time.time()) + 3600,
        }
    })
    return data["token"]


# ─────────────────────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "WartinLabs Voice Agent v2"}


@app.post("/start-session")
async def start_session():
    sid = uuid.uuid4().hex[:10]
    try:
        room       = await _create_room()
        room_url   = room["url"]
        room_name  = room["name"]
        user_token = await _make_token(room_name, owner=False)
        bot_token  = await _make_token(room_name, owner=True)
    except Exception as exc:
        logger.error(f"Session setup failed: {exc}")
        raise HTTPException(500, str(exc))

    async def _run():
        try:
            from bot import run_bot
            await run_bot(room_url, bot_token, sid, ws_callback=_emit)
        except Exception as exc:
            logger.error(f"Bot [{sid}] crashed: {exc}")
        finally:
            bot_tasks.pop(sid, None)

    bot_tasks[sid] = asyncio.create_task(_run())
    logger.info(f"Session {sid} started – room: {room_url}")
    return JSONResponse({"session_id": sid, "room_url": room_url, "token": user_token})


@app.post("/end-session/{session_id}")
async def end_session(session_id: str):
    task = bot_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
    ws = ws_clients.pop(session_id, None)
    if ws:
        try: await ws.close()
        except Exception: pass
    return {"status": "ended", "session_id": session_id}


@app.get("/sessions")
async def sessions():
    return {"bots": list(bot_tasks.keys()), "ws": list(ws_clients.keys())}


# ─────────────────────────────────────────────────────────────
# Startup: pre-build RAG index
# ─────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("WartinLabs Voice Agent starting …")
    loop = asyncio.get_event_loop()
    try:
        from rag_engine import ensure_index
        await loop.run_in_executor(None, ensure_index)
        logger.info("RAG index ready ✓")
    except Exception as exc:
        logger.error(f"RAG startup failed: {exc}")


# ─────────────────────────────────────────────────────────────
# Serve frontend (must be last)
# ─────────────────────────────────────────────────────────────
FRONTEND = ROOT / "frontend"
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        log_level="info",
        reload=False,
    )
