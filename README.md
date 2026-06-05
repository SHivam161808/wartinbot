# 🤖 WartinLabs AI Voice Agent — Linux Edition

**Ubuntu 22.04 · Python 3.11 · One-click install**

---

## 🚀 Quick Start — Three Steps

### Step 1 — Get your 4 free API keys

| Service | URL | Free Tier |
|---------|-----|-----------|
| **Deepgram** (STT + TTS) | https://console.deepgram.com | 12,000 min/year |
| **Groq** (LLM) | https://console.groq.com | Free tier |
| **Daily.co** (WebRTC) | https://dashboard.daily.co | 10,000 min/month |
| **Resend** (Email) | https://resend.com | 3,000 emails/month |

### Step 2 — Configure

```bash
cp .env.example .env
nano .env          # paste your 4 API keys
```

### Step 3 — Run

```bash
chmod +x start.sh
./start.sh
```

Then open **http://localhost:8000** in your browser. That's it.

---

## 📁 Project Structure

```
wartinlabs-voice-agent/
├── start.sh                  ← One-click launch (installs everything)
├── requirements.txt
├── .env.example
│
├── knowledge/
│   └── wartinlabs.md         ← Edit to update the knowledge base
│
├── backend/
│   ├── server.py             ← FastAPI server + WebSocket
│   ├── bot.py                ← Pipecat pipeline
│   ├── rag_engine.py         ← FAISS vector search
│   └── lead_capture.py       ← Resend email notifications
│
└── frontend/
    └── index.html            ← Standalone UI (no build step)
```

---

## 🧠 Tech Stack (all free)

| Component | Technology | Why |
|-----------|-----------|-----|
| Voice transport | Daily.co WebRTC | Best quality, free, native Linux |
| Speech-to-text | Deepgram Nova-2 | Most accurate, fast, generous free tier |
| Text-to-speech | Deepgram Aura | Natural voice, same API key |
| LLM | Groq + LLaMA 3.1-8B | Fastest free inference |
| VAD | Silero (local) | No cost, runs on CPU |
| RAG | FAISS + MiniLM (local) | No cost, no cloud, instant |
| Email | Resend | 3k free emails/month |

---

## 🔄 Updating the Knowledge Base

1. Edit `knowledge/wartinlabs.md`
2. Delete `backend/rag_cache.pkl` (if it exists)
3. Restart with `./start.sh` — index rebuilds automatically

---

## 🎯 Lead Capture

Aria automatically detects when a user wants to contact WartinLabs and collects:
- Full name, email, phone number
- Project requirements, budget, preferred contact time

A rich HTML email is sent to `WARTIN_LABS_EMAIL` via Resend immediately after.

---

## ⚙️ Customisation

**Change Aria's voice** — in `backend/bot.py`:
```python
voice="aura-asteria-en"   # warm female (default)
voice="aura-luna-en"      # soft female
voice="aura-orion-en"     # deep male
voice="aura-arcas-en"     # neutral male
```

**Change LLM model** — in `backend/bot.py`:
```python
model="llama-3.1-8b-instant"     # fast (default)
model="llama-3.1-70b-versatile"  # smarter
model="mixtral-8x7b-32768"       # alternative
```

---

## 🛠 Troubleshooting

| Problem | Fix |
|---------|-----|
| `daily-python not found` | Run `./start.sh` — it installs everything |
| `DAILY_API_KEY not configured` | Check `.env` file |
| No microphone in browser | Allow mic permission in browser |
| Slow first start | Downloading MiniLM model (~80 MB) — wait once |
| Email not sending | Verify `RESEND_API_KEY` and `FROM_EMAIL` domain |
