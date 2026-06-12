"""
WartinLabs Voice Agent – Fixed name extraction & accumulator
"""

from __future__ import annotations

import asyncio, json, os, re, sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import dateutil.parser
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    Frame, TextFrame, TranscriptionFrame, LLMFullResponseEndFrame,
    MetricsFrame, UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.metrics.metrics import (
    TTFBMetricsData, ProcessingMetricsData,
    LLMUsageMetricsData, TTSUsageMetricsData,
)

from rag_engine import retrieve, ensure_index
from lead_capture import send_lead_email

# ── Global registry ──────────────────────────────────────────
bot_interceptors: dict[str, "LeadState"] = {}

# ── Colour helpers ──────────────────────────────────────────
Y, CY, RS = "\033[1;33m", "\033[1;36m", "\033[0m"

def _render(event: str, **kw):
    print(f"\n{Y}{'─'*55}{RS}\n  {CY}📡 {event}{RS}   " +
          "  ".join(f"{k}={v}" for k, v in kw.items()) +
          f"\n{Y}{'─'*55}{RS}\n")

# ── System prompt ────────────────────────────────────────────
SYSTEM_TEMPLATE = """\
You are Aria, a warm, friendly AI voice assistant for WartinLabs.

PERSONALITY:
- Conversational, warm, patient. 2-4 short sentences max per reply.
- Never use bullet points, asterisks, markdown or lists.
- Numbers spoken naturally: "ten thousand dollars". Email: "info at wartinlabs dot com".

COMPANY:
- WartinLabs: AI solutions, custom software, SaaS, voice agents, automation.
- Office: 2217, 2nd Floor, Corenthum Tower, Noida-62, UP, India (near Electronic City Metro).
- Contact: info@wartinlabs.com  |  +91 6387541924
- We do NOT offer flight booking or travel services.

SCOPE & SAFETY:
- Answer factual questions directly (e.g. "Who is PM of India?" → "Narendra Modi").
- Harmful requests: "I can't help with that. Let's talk about something else."
- Unclear input: "I'm sorry, I didn't catch that — could you please repeat?"

LEAD CAPTURE (when user wants to start a project / consult / hire):
1. Warmly say you'll connect them with the team.
2. Ask ONE field at a time in this exact order:
   a. Full name
   b. Email address
   c. Phone number
   d. Brief project description
   e. Estimated budget
   f. Ask: "What date works best for a consultation call?" (do NOT ask for time yet)
   g. After user gives a date, ask: "What time works best for you on that day?"
3. After ALL seven fields confirm them and say EXACTLY:
   "Our team will reach out to you within 24 hours."

RULES:
- Never fabricate prices, timelines, or client names.
- Never guess an email address. If the user gives an incomplete email, politely ask them to provide the full email address.
- Goodbye → one warm farewell sentence, nothing more.

RELEVANT KNOWLEDGE:
{context}
"""

OPENING = (
    "Welcome to WartinLabs! I'm Aria, your AI assistant. "
    "We specialise in AI solutions, custom software, and digital transformation. "
    "How can I help you today?"
)

FIELD_ORDER = ["name", "email", "phone", "project", "budget", "contact_date", "contact_time"]

# ── Safe fire-and-forget ─────────────────────────────────────
def _fire(coro):
    fut = asyncio.ensure_future(coro)
    def _on_done(f):
        try:
            exc = f.exception()
            if exc:
                logger.error(f"❌ _fire raised: {exc!r}")
        except asyncio.CancelledError:
            pass
    fut.add_done_callback(_on_done)
    return fut

# ── Field extractors ─────────────────────────────────────────

def _extract_name(text: str) -> Optional[str]:
    # 1. Explicit pattern: "my name is X" or "I'm X"
    m = re.search(
        r"(?:my name is|i(?:'m| am|'m))\s+([A-Za-z][A-Za-z\s\-]{1,40})",
        text, re.IGNORECASE
    )
    if m:
        raw = re.sub(r'[^\w\s-]', '', m.group(1)).strip()
        _noise = {"yes","no","okay","ok","sure","my","i","the","a","an","is","am","are","name","me"}
        parts = [w for w in raw.split() if w.lower() not in _noise]
        if parts:
            return " ".join(parts[:3])

    # 2. Fallback: if text looks like a plain full name (two capitalized words), accept it
    #    but only when the current field is actually "name" (handled in caller by index)
    words = text.strip().split()
    if len(words) >= 2 and all(w[0].isupper() and w.isalpha() for w in words[:2]):
        # Filter out common filler words that start with capital (like "Yes," etc.)
        filler = {"yes","no","okay","ok","sure","hi","hello","thanks","thank","you"}
        name_words = [w for w in words if w.lower() not in filler]
        if len(name_words) >= 2:
            return " ".join(name_words[:3])

    return None

def _extract_email(text: str) -> Optional[str]:
    m = re.search(r"[\w.+\-]+@[\w.\-]+\.\w+", text)
    if m:
        return m.group(0)
    m = re.search(r"([a-zA-Z0-9._%+-]+)\s+at\s+(\w+)\s+dot\s+(\w+)", text, re.IGNORECASE)
    if m:
        username = m.group(1).lower()
        fillers = {"yes","no","okay","sure","ok","hi","hello","my","me","i","the"}
        if username in fillers:
            logger.warning(f"🚫 Ignoring filler email username: '{username}'")
            return None
        return f"{m.group(1)}@{m.group(2)}.{m.group(3)}"
    return None

def _extract_phone(text: str) -> Optional[str]:
    spoken = re.sub(
        r"\b(zero|one|two|three|four|five|six|seven|eight|nine)\b",
        lambda m: str(["zero","one","two","three","four","five","six","seven","eight","nine"].index(m.group().lower())),
        text, flags=re.IGNORECASE
    )
    m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", spoken)
    return m.group(0).strip() if m else None

def _extract_project(text: str) -> Optional[str]:
    stripped = text.strip()
    return stripped[:200] if len(stripped) >= 15 else None

def _extract_budget(text: str) -> Optional[str]:
    return text.strip()[:100] if re.search(r"\d", text) else None

_DATE_SIGNALS = re.compile(
    r"\b(\d{1,2}(st|nd|rd|th)?[\s\-/]\w+|"
    r"\w+\s+\d{1,2}(st|nd|rd|th)?|"
    r"\d{1,2}[/\-]\d{1,2}([/\-]\d{2,4})?|"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(tomorrow|next\s+\w+|this\s+(monday|tuesday|wednesday|thursday|friday)))\b",
    re.IGNORECASE
)

def _extract_date(text: str) -> Optional[str]:
    if not _DATE_SIGNALS.search(text):
        return None
    try:
        dt = dateutil.parser.parse(text, fuzzy=True, default=datetime.now())
        if dt.date() < datetime.now().date():
            dt = dt.replace(year=dt.year + 1)
        return dt.date().isoformat()
    except:
        return None

_TIME_SIGNALS = re.compile(
    r"\b(\d{1,2}(:\d{2})?\s*(am|pm)|"
    r"\d{1,2}:\d{2}|"
    r"(noon|midnight))\b",
    re.IGNORECASE
)

def _extract_time(text: str) -> Optional[str]:
    if not _TIME_SIGNALS.search(text):
        return None
    try:
        dt = dateutil.parser.parse(text, fuzzy=True, default=datetime.now())
        return dt.strftime("%H:%M")
    except:
        return None

EXTRACTORS = {
    "name": _extract_name, "email": _extract_email, "phone": _extract_phone,
    "project": _extract_project, "budget": _extract_budget,
    "contact_date": _extract_date, "contact_time": _extract_time,
}

# ── Intent & completion detectors ───────────────────────────
_LEAD_TRIGGERS = [
    "contact", "reach out", "get in touch", "book", "consultation",
    "quote", "proposal", "start a project", "discuss", "hire",
    "want to work", "interested in", "build", "develop", "create",
    "enquiry", "inquiry", "call me", "connect me", "project",
    "want to sell", "want to make", "need a website", "need an app",
    "i need", "we need", "looking for",
]
_COMPLETION_PHRASES = [
    "reach out to you within 24 hours",
    "our team will reach out",
    "get back to you within 24 hours",
]

def _wants_lead(text: str) -> bool:
    return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

def _is_completion(text: str) -> bool:
    return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# ── Utterance accumulator (increased gap for email fragments) ─
class UtteranceAccumulator:
    def __init__(self, flush_cb: Callable, gap_seconds: float = 4.0):
        self._cb = flush_cb
        self._gap = gap_seconds
        self._buf: list[str] = []
        self._task: Optional[asyncio.Task] = None

    def feed(self, text: str):
        self._buf.append(text)
        if self._task:
            self._task.cancel()
        self._task = asyncio.ensure_future(self._delayed_flush())

    async def _delayed_flush(self):
        await asyncio.sleep(self._gap)
        if self._buf:
            full = " ".join(self._buf).strip()
            self._buf.clear()
            self._task = None
            if full:
                await self._cb(full)

# ═════════════════════════════════════════════════════════════
# LeadState
# ═════════════════════════════════════════════════════════════
class LeadState:
    def __init__(self, ws_callback, session_id: str):
        self._ws = ws_callback
        self._sid = session_id
        self.active = False
        self.collected: dict[str, str] = {}
        self.field_index = 0
        self.email_sent = False
        self.date_extracted = False
        self.time_extracted = False

    def activate(self):
        if not self.active:
            self.active = True
            self.field_index = 0
            logger.warning("🎯 LEAD ACTIVE")

    async def emit(self, event: str, data: dict):
        if not self._ws:
            return
        try:
            await self._ws(event, {**data, "session_id": self._sid})
        except Exception as e:
            logger.error(f"❌ ws emit '{event}' failed: {e}")

    async def emit_field(self, field: str, value: str):
        await self.emit("field_collected", {"field": field, "value": value, "valid": True})
        _render("FIELD_COLLECTED", field=field, value=value)

    async def emit_calendar_event(self, iso_date: str, iso_time: str):
        if not iso_date or not iso_time:
            return
        try:
            start_iso = f"{iso_date}T{iso_time}:00"
            datetime.fromisoformat(start_iso)
        except ValueError:
            start_iso = f"{iso_date}T{iso_time}"
        await self.emit("calendar_event", {
            "title": "WartinLabs Consultation Call",
            "start_iso": start_iso,
        })
        _render("CALENDAR_EVENT", start_iso=start_iso)

    async def emit_progress(self):
        done = list(self.collected.keys())
        left = [f for f in FIELD_ORDER if f not in self.collected]
        await self.emit("lead_progress", {
            "completed": done, "remaining": left,
            "percent": int(len(done) / len(FIELD_ORDER) * 100),
        })

    async def show_calendar(self):
        await self.emit("show_calendar", {"message": "Please select a date."})

    async def show_time_picker(self, date: str):
        await self.emit("show_time_picker", {
            "date": date, "message": f"Select a time for {date}.",
            "slots": ["09:00","10:00","11:00","14:00","15:00","16:00","17:00"],
        })

    async def handle_frontend_message(self, msg_type: str, data: dict):
        if msg_type == "date_selected":
            d = data.get("date", "")
            if d:
                self.collected["contact_date"] = d
                self.date_extracted = True
                self.field_index = FIELD_ORDER.index("contact_time")  # skip to time
                await self.emit_field("contact_date", d)
                await self.emit_progress()
                await self.show_time_picker(d)
        elif msg_type == "time_selected":
            t = data.get("time", "")
            if t and "contact_date" in self.collected:
                self.collected["contact_time"] = t
                self.time_extracted = True
                self.field_index = len(FIELD_ORDER)  # all done
                await self.emit_field("contact_time", t)
                await self.emit_calendar_event(self.collected["contact_date"], t)
                await self.emit_progress()

    def try_extract(self, user_text: str):
        if not self.active or self.field_index >= len(FIELD_ORDER):
            return
        field = FIELD_ORDER[self.field_index]
        extractor = EXTRACTORS.get(field)
        if not extractor:
            return
        logger.info(f"🔍 Extracting [{field}] (index {self.field_index}) from: '{user_text[:80]}'")

        # ---- DATE ----
        if field == "contact_date":
            iso = _extract_date(user_text)
            if iso:
                self.collected[field] = iso
                self.date_extracted = True
                try:
                    display = datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %B")
                except:
                    display = iso
                _fire(self.emit_field(field, display))
                _fire(self.emit_progress())
                self.field_index = FIELD_ORDER.index("contact_time")
                _fire(self.show_time_picker(iso))
            return

        # ---- TIME ----
        if field == "contact_time":
            t = _extract_time(user_text)
            if t:
                self.collected[field] = t
                self.time_extracted = True
                try:
                    display = datetime.strptime(t, "%H:%M").strftime("%-I:%M %p").lower()
                except:
                    display = t
                _fire(self.emit_field(field, display))
                if "contact_date" in self.collected:
                    _fire(self.emit_calendar_event(self.collected["contact_date"], t))
                _fire(self.emit_progress())
                self.field_index = len(FIELD_ORDER)
            return

        # ---- ALL OTHER FIELDS (name, email, phone, project, budget) ----
        value = extractor(user_text)
        if value:
            self.collected[field] = value
            self.field_index += 1  # Advance to next field
            _fire(self.emit_field(field, value))
            _fire(self.emit_progress())
            if field == "budget" and not self.date_extracted:
                _fire(self.show_calendar())

    @property
    def all_collected(self) -> bool:
        return all(f in self.collected for f in FIELD_ORDER)

    def get_lead_data(self) -> dict:
        return {
            "name":         self.collected.get("name",         "Not provided"),
            "email":        self.collected.get("email",        "Not provided"),
            "phone":        self.collected.get("phone",        "Not provided"),
            "requirements": self.collected.get("project",      "Discussed"),
            "budget":       self.collected.get("budget",       "Not specified"),
            "contact_date": self.collected.get("contact_date", "Not specified"),
            "contact_time": self.collected.get("contact_time", "ASAP"),
        }

# ═════════════════════════════════════════════════════════════
# TranscriptionInterceptor
# ═════════════════════════════════════════════════════════════
class TranscriptionInterceptor(FrameProcessor):
    def __init__(self, lead: LeadState, ws_callback, session_id: str, terminator):
        super().__init__()
        self._lead = lead
        self._ws = ws_callback
        self._sid = session_id
        self._terminator = terminator
        self._accum = UtteranceAccumulator(self._on_utterance, gap_seconds=4.0)

    async def _on_utterance(self, full_text: str):
        logger.info(f"UTTERANCE [{self._sid}]: {full_text}")
        if self._ws:
            try:
                await self._ws("user_text", {"text": full_text, "session_id": self._sid})
            except Exception as e:
                logger.error(f"❌ ws user_text failed: {e}")

        if self._terminator.is_goodbye(full_text):
            await self._terminator.trigger_end()
            return

        if _wants_lead(full_text) and not self._lead.active:
            self._lead.activate()

        if self._lead.active:
            self._lead.try_extract(full_text)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = (frame.text or "").strip()
            if text:
                self._accum.feed(text)
        await self.push_frame(frame, direction)

# ═════════════════════════════════════════════════════════════
# EmailTrigger
# ═════════════════════════════════════════════════════════════
class EmailTrigger(FrameProcessor):
    def __init__(self, lead: LeadState, ws_callback, session_id: str):
        super().__init__()
        self._lead = lead
        self._ws = ws_callback
        self._sid = session_id
        self._buffer = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TextFrame):
                self._buffer += frame.text or ""
            elif isinstance(frame, LLMFullResponseEndFrame):
                if (not self._lead.email_sent
                        and self._lead.all_collected
                        and _is_completion(self._buffer)):
                    self._lead.email_sent = True
                    lead_data = self._lead.get_lead_data()
                    logger.warning("📧 SENDING LEAD EMAIL")
                    try:
                        sent = await send_lead_email(lead_data)
                        if self._ws:
                            await self._ws("lead_captured", {
                                "data": lead_data,
                                "email_sent": sent,
                                "session_id": self._sid,
                            })
                        _render("LEAD_CAPTURED", email_sent=sent, name=lead_data["name"])
                    except Exception as e:
                        logger.error(f"Email error: {e}")
                self._buffer = ""
        await self.push_frame(frame, direction)

# ── Session terminator ───────────────────────────────────────
_BYE = ["goodbye","good bye","bye","end session","disconnect","end call","hang up",
        "that's all","i'm done","have a nice day","exit","quit"]
class SessionTerminator(FrameProcessor):
    def __init__(self, ws, sid):
        super().__init__()
        self._ws, self._sid = ws, sid
        self._triggered, self._task = False, None
    def set_task(self, t): self._task = t
    def is_goodbye(self, t): return any(p in t.lower() for p in _BYE)
    async def trigger_end(self):
        if self._triggered or not self._task: return
        self._triggered = True
        if self._ws: await self._ws("session_ended", {"session_id": self._sid})
        await asyncio.sleep(1.5)
        await self._task.queue_frame(EndFrame())
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

# ── E2E tracker & metrics ────────────────────────────────────
class E2ETracker(FrameProcessor):
    def __init__(self, e2e_list):
        super().__init__()
        self._e2e = e2e_list
        self._stop = None
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStoppedSpeakingFrame): self._stop = asyncio.get_event_loop().time()
        elif isinstance(frame, BotStartedSpeakingFrame) and self._stop:
            e2e = asyncio.get_event_loop().time() - self._stop
            self._e2e.append(round(e2e, 4))
            logger.info(f"🎯 E2E: {e2e:.3f}s (n={len(self._e2e)})")
            self._stop = None
        await self.push_frame(frame, direction)

class MetricsCollector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.ttfb = defaultdict(list)
        self.proc = defaultdict(list)
        self.llm = []
        self.tts = []
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
            if not raw: return
            for md in (raw if isinstance(raw, list) else [raw]):
                if isinstance(md, TTFBMetricsData): self.ttfb[md.processor].append(md.value)
                elif isinstance(md, ProcessingMetricsData): self.proc[md.processor].append(md.value)
                elif isinstance(md, LLMUsageMetricsData):
                    v = getattr(md, "value", md)
                    self.llm.append((getattr(v, "prompt_tokens", 0), getattr(v, "completion_tokens", 0)))
                elif isinstance(md, TTSUsageMetricsData): self.tts.append(getattr(md, "value", 0))
        await self.push_frame(frame, direction)

# ═════════════════════════════════════════════════════════════
# run_bot
# ═════════════════════════════════════════════════════════════
async def run_bot(room_url: str, token: str, session_id: str,
                  ws_callback: Optional[Callable] = None):
    ensure_index()
    e2e = []

    lead = LeadState(ws_callback, session_id)
    bot_interceptors[session_id] = lead

    terminator = SessionTerminator(ws_callback, session_id)
    transc_interceptor = TranscriptionInterceptor(lead, ws_callback, session_id, terminator)

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
    llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"],
                         model="llama-3.1-8b-instant", temperature=0.6, max_tokens=160)
    transport = DailyTransport(room_url, token, "Aria",
        DailyParams(audio_in_enabled=True, audio_out_enabled=True,
                    vad_analyzer=SileroVADAnalyzer()))

    ctx_text = retrieve("WartinLabs services overview", top_k=2)
    sys_msg = SYSTEM_TEMPLATE.format(context=ctx_text or "General WartinLabs knowledge.")
    messages = [
        {"role": "system",    "content": sys_msg},
        {"role": "user",      "content": "Hello"},
        {"role": "assistant", "content": OPENING},
    ]
    context = OpenAILLMContext(messages)
    ctx_agg = llm.create_context_aggregator(context)

    email_trigger = EmailTrigger(lead, ws_callback, session_id)
    tracker = E2ETracker(e2e)
    metrics = MetricsCollector()

    pipeline = Pipeline([
        transport.input(),
        tracker,
        stt,
        transc_interceptor,
        ctx_agg.user(),
        llm,
        email_trigger,
        tts,
        transport.output(),
        ctx_agg.assistant(),
        metrics,
    ])

    task = PipelineTask(pipeline, params=PipelineParams(
        allow_interruptions=True, enable_metrics=True, enable_usage_metrics=True))
    terminator.set_task(task)

    @transport.event_handler("on_first_participant_joined")
    async def on_first(transport, participant):
        pid = participant["id"]
        logger.info(f"Participant joined: {pid}")
        await transport.capture_participant_audio(pid)
        if ws_callback:
            await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
        await task.queue_frames([TextFrame(OPENING)])

    logger.warning(f"🚀 Starting pipeline for session {session_id}")
    runner = PipelineRunner()
    try:
        await runner.run(task)
    finally:
        bot_interceptors.pop(session_id, None)