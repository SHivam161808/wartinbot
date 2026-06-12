# from __future__ import annotations

# import asyncio, os, re, sys
# from collections import defaultdict
# from datetime import datetime, timedelta
# from pathlib import Path
# from typing import Callable, Optional

# import dateutil.parser
# from dotenv import load_dotenv
# from loguru import logger

# sys.path.insert(0, str(Path(__file__).resolve().parent))
# load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# from pipecat.audio.vad.silero import SileroVADAnalyzer
# from pipecat.frames.frames import (
#     Frame, TextFrame, TranscriptionFrame, LLMFullResponseEndFrame,
#     MetricsFrame, UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
# )
# from pipecat.pipeline.pipeline import Pipeline
# from pipecat.pipeline.runner import PipelineRunner
# from pipecat.pipeline.task import PipelineParams, PipelineTask
# from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# from pipecat.services.deepgram.stt import DeepgramSTTService
# from pipecat.services.deepgram.tts import DeepgramTTSService
# from pipecat.services.groq.llm import GroqLLMService
# from pipecat.transports.services.daily import DailyParams, DailyTransport
# from pipecat.metrics.metrics import (
#     TTFBMetricsData, ProcessingMetricsData,
#     LLMUsageMetricsData, TTSUsageMetricsData,
# )

# from rag_engine import retrieve, ensure_index
# from lead_capture import send_lead_email

# # ── Global registry ──────────────────────────────────────────
# bot_interceptors: dict[str, "LeadState"] = {}

# # ── Colour helpers ──────────────────────────────────────────
# Y, CY, RS = "\033[1;33m", "\033[1;36m", "\033[0m"

# def _render(event: str, **kw):
#     print(f"\n{Y}{'─'*55}{RS}\n  {CY}📡 {event}{RS}   " +
#           "  ".join(f"{k}={v}" for k, v in kw.items()) +
#           f"\n{Y}{'─'*55}{RS}\n")

# # ── System prompt ────────────────────────────────────────────
# SYSTEM_TEMPLATE = """\
# You are Aria, a warm, friendly AI voice assistant for WartinLabs.

# PERSONALITY:
# - Conversational, warm, patient. 2-4 short sentences max per reply.
# - Never use bullet points, asterisks, markdown or lists.
# - Numbers spoken naturally: "ten thousand dollars". Email: "info at wartinlabs dot com".

# COMPANY:
# - WartinLabs: AI solutions, custom software, SaaS, voice agents, automation.
# - Office: 2217, 2nd Floor, Corenthum Tower, Noida-62, UP, India (near Electronic City Metro).
# - Contact: info@wartinlabs.com  |  +91 6387541924
# - We do NOT offer flight booking or travel services.

# SCOPE & SAFETY:
# - Answer factual questions directly (e.g. "Who is PM of India?" → "Narendra Modi").
# - Harmful requests: "I can't help with that. Let's talk about something else."
# - Unclear input: "I'm sorry, I didn't catch that — could you please repeat?"

# LEAD CAPTURE (when user wants to start a project / consult / hire):
# 1. Warmly say you'll connect them with the team.
# 2. Ask ONE field at a time in this exact order:
#    a. Full name
#    b. Email address
#    c. Phone number
#    d. Brief project description
#    e. Estimated budget
#    f. Ask: "What date works best for a consultation call?" (do NOT ask for time yet)
#    g. After user gives a date, ask: "What time works best for you on that day?"
# 3. After ALL seven fields confirm them and say EXACTLY:
#    "Our team will reach out to you within 24 hours."

# RULES:
# - Never fabricate prices, timelines, or client names.
# - Goodbye → one warm farewell sentence, nothing more.
# - If user asks to correct an email: "Sure, please tell me your corrected email address."

# RELEVANT KNOWLEDGE:
# {context}
# """

# OPENING = (
#     "Welcome to WartinLabs! I'm Aria, your AI assistant. "
#     "We specialise in AI solutions, custom software, and digital transformation. "
#     "How can I help you today?"
# )

# FIELD_ORDER = ["name", "email", "phone", "project", "budget", "contact_date", "contact_time"]

# # ── Safe fire-and-forget ─────────────────────────────────────
# def _fire(coro):
#     fut = asyncio.ensure_future(coro)
#     def _on_done(f):
#         try:
#             exc = f.exception()
#             if exc:
#                 logger.error(f"❌ _fire raised: {exc!r}")
#         except asyncio.CancelledError:
#             pass
#     fut.add_done_callback(_on_done)
#     return fut

# # ── Field extractors ─────────────────────────────────────────

# # Only extract a name when the user explicitly says "my name is X" or "I'm X".
# # NO capitalised-word fallback — that causes false positives on every sentence.
# def _extract_name(text: str) -> Optional[str]:
#     m = re.search(
#         r"(?:my name is|i(?:'m| am|'m))\s+([A-Za-z][A-Za-z\s\-]{1,40})",
#         text, re.IGNORECASE
#     )
#     if not m:
#         return None
#     raw = re.sub(r'[^\w\s-]', '', m.group(1)).strip()
#     _noise = {"yes","no","okay","ok","sure","my","i","the","a","an","is","am","are","name","me"}
#     parts = [w for w in raw.split() if w.lower() not in _noise]
#     return " ".join(parts[:3]) if parts else None

# def _extract_email(text: str) -> Optional[str]:
#     m = re.search(r"[\w.+\-]+@[\w.\-]+\.\w+", text)
#     if m:
#         return m.group(0)
#     m = re.search(r"([a-zA-Z0-9._%+-]+)\s+at\s+(\w+)\s+dot\s+(\w+)", text, re.IGNORECASE)
#     if m:
#         return f"{m.group(1)}@{m.group(2)}.{m.group(3)}"
#     return None

# def _extract_phone(text: str) -> Optional[str]:
#     # Accept spoken digits: "seven zero seven eight..." → digits only
#     spoken = re.sub(
#         r"\b(zero|one|two|three|four|five|six|seven|eight|nine)\b",
#         lambda m: str(["zero","one","two","three","four","five","six","seven","eight","nine"].index(m.group().lower())),
#         text, flags=re.IGNORECASE
#     )
#     m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", spoken)
#     return m.group(0).strip() if m else None

# def _extract_project(text: str) -> Optional[str]:
#     # Require at least 15 chars and some content words to avoid partial fragments
#     stripped = text.strip()
#     return stripped[:200] if len(stripped) >= 15 else None

# def _extract_budget(text: str) -> Optional[str]:
#     return text.strip()[:100] if re.search(r"\d", text) else None

# _DATE_SIGNALS = re.compile(
#     r"\b(\d{1,2}(st|nd|rd|th)?[\s\-/]\w+|"
#     r"\w+\s+\d{1,2}(st|nd|rd|th)?|"
#     r"\d{1,2}[/\-]\d{1,2}([/\-]\d{2,4})?|"
#     r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
#     r"(tomorrow|next\s+\w+|this\s+(monday|tuesday|wednesday|thursday|friday)))\b",
#     re.IGNORECASE
# )

# def _extract_date(text: str) -> Optional[str]:
#     if not _DATE_SIGNALS.search(text):
#         return None
#     try:
#         dt = dateutil.parser.parse(text, fuzzy=True, default=datetime.now())
#         if dt.date() < datetime.now().date():
#             dt = dt.replace(year=dt.year + 1)
#         return dt.date().isoformat()
#     except:
#         return None

# _TIME_SIGNALS = re.compile(
#     r"\b(\d{1,2}(:\d{2})?\s*(am|pm)|"
#     r"\d{1,2}:\d{2}|"
#     r"(noon|midnight))\b",
#     re.IGNORECASE
# )

# def _extract_time(text: str) -> Optional[str]:
#     if not _TIME_SIGNALS.search(text):
#         return None
#     try:
#         dt = dateutil.parser.parse(text, fuzzy=True, default=datetime.now())
#         return dt.strftime("%H:%M")
#     except:
#         return None

# EXTRACTORS = {
#     "name": _extract_name, "email": _extract_email, "phone": _extract_phone,
#     "project": _extract_project, "budget": _extract_budget,
#     "contact_date": _extract_date, "contact_time": _extract_time,
# }

# # ── Intent & completion detectors ───────────────────────────
# _LEAD_TRIGGERS = [
#     "contact", "reach out", "get in touch", "book", "consultation",
#     "quote", "proposal", "start a project", "discuss", "hire",
#     "want to work", "interested in", "build", "develop", "create",
#     "enquiry", "inquiry", "call me", "connect me", "project",
#     "want to sell", "want to make", "need a website", "need an app",
#     "i need", "we need", "looking for",
# ]
# _COMPLETION_PHRASES = [
#     "reach out to you within 24 hours",
#     "our team will reach out",
#     "get back to you within 24 hours",
# ]

# def _wants_lead(text: str) -> bool:
#     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# def _is_completion(text: str) -> bool:
#     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# # ── Utterance accumulator ────────────────────────────────────
# # Deepgram fires many small final frames per sentence.
# # We accumulate within a 2-second window and only process the full sentence.
# class UtteranceAccumulator:
#     """Buffers TranscriptionFrame text and flushes after a silence gap."""

#     def __init__(self, flush_cb: Callable, gap_seconds: float = 1.8):
#         self._cb = flush_cb
#         self._gap = gap_seconds
#         self._buf: list[str] = []
#         self._task: Optional[asyncio.Task] = None

#     def feed(self, text: str):
#         self._buf.append(text)
#         if self._task:
#             self._task.cancel()
#         self._task = asyncio.ensure_future(self._delayed_flush())

#     async def _delayed_flush(self):
#         await asyncio.sleep(self._gap)
#         if self._buf:
#             full = " ".join(self._buf).strip()
#             self._buf.clear()
#             self._task = None
#             if full:
#                 await self._cb(full)

#     async def flush_now(self):
#         if self._task:
#             self._task.cancel()
#             self._task = None
#         if self._buf:
#             full = " ".join(self._buf).strip()
#             self._buf.clear()
#             if full:
#                 await self._cb(full)

# # ═════════════════════════════════════════════════════════════
# # LeadState
# # ═════════════════════════════════════════════════════════════
# class LeadState:
#     def __init__(self, ws_callback, session_id: str):
#         self._ws = ws_callback
#         self._sid = session_id
#         self.active = False
#         self.collected: dict[str, str] = {}
#         self.field_index = 0
#         self.email_sent = False
#         self.date_extracted = False
#         self.time_extracted = False

#     def activate(self):
#         if not self.active:
#             self.active = True
#             self.field_index = 0
#             logger.warning("🎯 LEAD ACTIVE")

#     async def emit(self, event: str, data: dict):
#         if not self._ws:
#             return
#         try:
#             await self._ws(event, {**data, "session_id": self._sid})
#         except Exception as e:
#             logger.error(f"❌ ws emit '{event}' failed: {e}")

#     async def emit_field(self, field: str, value: str):
#         """Emit field_collected — one event per captured field."""
#         await self.emit("field_collected", {"field": field, "value": value, "valid": True})
#         _render("FIELD_COLLECTED", field=field, value=value)

#     async def emit_calendar_event(self, iso_date: str, iso_time: str):
#         """
#         Emit calendar_event with ISO-8601 start time.
#         Format: {"title": "WartinLabs Consultation Call", "start_iso": "2026-07-05T17:00:00"}
#         """
#         if not iso_date or not iso_time:
#             return
#         try:
#             start_iso = f"{iso_date}T{iso_time}:00"
#             # Validate by parsing
#             datetime.fromisoformat(start_iso)
#         except ValueError:
#             start_iso = f"{iso_date}T{iso_time}"
#         await self.emit("calendar_event", {
#             "title": "WartinLabs Consultation Call",
#             "start_iso": start_iso,
#         })
#         _render("CALENDAR_EVENT", start_iso=start_iso)

#     async def emit_progress(self):
#         done = list(self.collected.keys())
#         left = [f for f in FIELD_ORDER if f not in self.collected]
#         await self.emit("lead_progress", {
#             "completed": done, "remaining": left,
#             "percent": int(len(done) / len(FIELD_ORDER) * 100),
#         })

#     async def show_calendar(self):
#         await self.emit("show_calendar", {"message": "Please select a date."})

#     async def show_time_picker(self, date: str):
#         await self.emit("show_time_picker", {
#             "date": date, "message": f"Select a time for {date}.",
#             "slots": ["09:00","10:00","11:00","14:00","15:00","16:00","17:00"],
#         })

#     async def handle_frontend_message(self, msg_type: str, data: dict):
#         """Handle date_selected / time_selected from the frontend picker."""
#         if msg_type == "date_selected":
#             d = data.get("date", "")
#             if d:
#                 self.collected["contact_date"] = d
#                 self.date_extracted = True
#                 self._advance("contact_date")
#                 await self.emit_field("contact_date", d)
#                 await self.emit_progress()
#                 await self.show_time_picker(d)

#         elif msg_type == "time_selected":
#             t = data.get("time", "")
#             if t and "contact_date" in self.collected:
#                 self.collected["contact_time"] = t
#                 self.time_extracted = True
#                 self._advance("contact_time")
#                 await self.emit_field("contact_time", t)
#                 await self.emit_calendar_event(self.collected["contact_date"], t)
#                 await self.emit_progress()

#     def _advance(self, field: str):
#         while (self.field_index < len(FIELD_ORDER)
#                and FIELD_ORDER[self.field_index] == field):
#             self.field_index += 1

#     def try_extract(self, user_text: str):
#         """Called with a complete utterance (after accumulator flush)."""
#         if not self.active or self.field_index >= len(FIELD_ORDER):
#             return
#         field = FIELD_ORDER[self.field_index]
#         extractor = EXTRACTORS.get(field)
#         if not extractor:
#             return
#         logger.info(f"🔍 Extracting [{field}] from: '{user_text[:80]}'")

#         if field == "contact_date":
#             iso = _extract_date(user_text)
#             if iso:
#                 self.collected["contact_date"] = iso
#                 self.date_extracted = True
#                 self._advance("contact_date")
#                 try:
#                     display = datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %B")
#                 except:
#                     display = iso
#                 _fire(self.emit_field("contact_date", display))
#                 _fire(self.emit_progress())
#                 _fire(self.show_time_picker(iso))
#             return

#         if field == "contact_time":
#             t = _extract_time(user_text)
#             if t:
#                 self.collected["contact_time"] = t
#                 self.time_extracted = True
#                 self._advance("contact_time")
#                 try:
#                     display = datetime.strptime(t, "%H:%M").strftime("%-I:%M %p").lower()
#                 except:
#                     display = t
#                 _fire(self.emit_field("contact_time", display))
#                 if "contact_date" in self.collected:
#                     _fire(self.emit_calendar_event(self.collected["contact_date"], t))
#                 _fire(self.emit_progress())
#             return

#         value = extractor(user_text)
#         if value:
#             self.collected[field] = value
#             self._advance(field)
#             _fire(self.emit_field(field, value))
#             _fire(self.emit_progress())
#             if field == "budget" and not self.date_extracted:
#                 _fire(self.show_calendar())

#     @property
#     def all_collected(self) -> bool:
#         return all(f in self.collected for f in FIELD_ORDER)

#     def get_lead_data(self) -> dict:
#         return {
#             "name":         self.collected.get("name",         "Not provided"),
#             "email":        self.collected.get("email",        "Not provided"),
#             "phone":        self.collected.get("phone",        "Not provided"),
#             "requirements": self.collected.get("project",      "Discussed"),
#             "budget":       self.collected.get("budget",       "Not specified"),
#             "contact_date": self.collected.get("contact_date", "Not specified"),
#             "contact_time": self.collected.get("contact_time", "ASAP"),
#         }

# # ═════════════════════════════════════════════════════════════
# # TranscriptionInterceptor — reads TranscriptionFrame from pipeline
# # ═════════════════════════════════════════════════════════════
# class TranscriptionInterceptor(FrameProcessor):
#     def __init__(self, lead: LeadState, ws_callback, session_id: str, terminator):
#         super().__init__()
#         self._lead = lead
#         self._ws = ws_callback
#         self._sid = session_id
#         self._terminator = terminator
#         # Accumulator: buffer partial Deepgram finals, flush after silence gap
#         self._accum = UtteranceAccumulator(self._on_utterance, gap_seconds=1.5)

#     async def _on_utterance(self, full_text: str):
#         """Called with a complete buffered utterance."""
#         logger.info(f"UTTERANCE [{self._sid}]: {full_text}")

#         # Only emit user_text for complete utterances, not every word fragment
#         if self._ws:
#             try:
#                 await self._ws("user_text", {"text": full_text, "session_id": self._sid})
#             except Exception as e:
#                 logger.error(f"❌ ws user_text failed: {e}")

#         if self._terminator.is_goodbye(full_text):
#             await self._terminator.trigger_end()
#             return

#         if _wants_lead(full_text) and not self._lead.active:
#             self._lead.activate()

#         if self._lead.active:
#             self._lead.try_extract(full_text)

#     async def process_frame(self, frame: Frame, direction: FrameDirection):
#         await super().process_frame(frame, direction)

#         if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
#             text = (frame.text or "").strip()
#             if text:
#                 self._accum.feed(text)

#         # Flush accumulator when user stops speaking (VAD signal)
#         if isinstance(frame, UserStoppedSpeakingFrame):
#             await self._accum.flush_now()

#         await self.push_frame(frame, direction)

# # ═════════════════════════════════════════════════════════════
# # EmailTrigger
# # ═════════════════════════════════════════════════════════════
# class EmailTrigger(FrameProcessor):
#     def __init__(self, lead: LeadState, ws_callback, session_id: str):
#         super().__init__()
#         self._lead = lead
#         self._ws = ws_callback
#         self._sid = session_id
#         self._buffer = ""

#     async def process_frame(self, frame: Frame, direction: FrameDirection):
#         await super().process_frame(frame, direction)
#         if direction == FrameDirection.DOWNSTREAM:
#             if isinstance(frame, TextFrame):
#                 self._buffer += frame.text or ""
#             elif isinstance(frame, LLMFullResponseEndFrame):
#                 if (not self._lead.email_sent
#                         and self._lead.all_collected
#                         and _is_completion(self._buffer)):
#                     self._lead.email_sent = True
#                     lead_data = self._lead.get_lead_data()
#                     logger.warning("📧 SENDING LEAD EMAIL")
#                     try:
#                         sent = await send_lead_email(lead_data)
#                         if self._ws:
#                             await self._ws("lead_captured", {
#                                 "data": lead_data,
#                                 "email_sent": sent,
#                                 "session_id": self._sid,
#                             })
#                         _render("LEAD_CAPTURED", email_sent=sent, name=lead_data["name"])
#                     except Exception as e:
#                         logger.error(f"Email error: {e}")
#                 self._buffer = ""
#         await self.push_frame(frame, direction)

# # ── Session terminator ───────────────────────────────────────
# _BYE = ["goodbye","good bye","bye","end session","disconnect","end call","hang up",
#         "that's all","i'm done","have a nice day","exit","quit"]

# class SessionTerminator(FrameProcessor):
#     def __init__(self, ws, sid):
#         super().__init__()
#         self._ws, self._sid = ws, sid
#         self._triggered, self._task = False, None

#     def set_task(self, t): self._task = t
#     def is_goodbye(self, t): return any(p in t.lower() for p in _BYE)

#     async def trigger_end(self):
#         if self._triggered or not self._task: return
#         self._triggered = True
#         if self._ws:
#             await self._ws("session_ended", {"session_id": self._sid})
#         await asyncio.sleep(1.5)
#         await self._task.queue_frame(EndFrame())

#     async def process_frame(self, frame, direction):
#         await super().process_frame(frame, direction)
#         await self.push_frame(frame, direction)

# # ── E2E tracker & metrics ────────────────────────────────────
# class E2ETracker(FrameProcessor):
#     def __init__(self, e2e_list):
#         super().__init__()
#         self._e2e = e2e_list
#         self._stop = None

#     async def process_frame(self, frame, direction):
#         await super().process_frame(frame, direction)
#         if isinstance(frame, UserStoppedSpeakingFrame):
#             self._stop = asyncio.get_event_loop().time()
#         elif isinstance(frame, BotStartedSpeakingFrame) and self._stop:
#             e2e = asyncio.get_event_loop().time() - self._stop
#             self._e2e.append(round(e2e, 4))
#             logger.info(f"🎯 E2E: {e2e:.3f}s (n={len(self._e2e)})")
#             self._stop = None
#         await self.push_frame(frame, direction)

# class MetricsCollector(FrameProcessor):
#     def __init__(self):
#         super().__init__()
#         self.ttfb = defaultdict(list)
#         self.proc = defaultdict(list)
#         self.llm = []
#         self.tts = []

#     async def process_frame(self, frame, direction):
#         await super().process_frame(frame, direction)
#         if isinstance(frame, MetricsFrame):
#             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
#             if not raw: return
#             for md in (raw if isinstance(raw, list) else [raw]):
#                 if isinstance(md, TTFBMetricsData): self.ttfb[md.processor].append(md.value)
#                 elif isinstance(md, ProcessingMetricsData): self.proc[md.processor].append(md.value)
#                 elif isinstance(md, LLMUsageMetricsData):
#                     v = getattr(md, "value", md)
#                     self.llm.append((getattr(v, "prompt_tokens", 0), getattr(v, "completion_tokens", 0)))
#                 elif isinstance(md, TTSUsageMetricsData): self.tts.append(getattr(md, "value", 0))
#         await self.push_frame(frame, direction)

# # ═════════════════════════════════════════════════════════════
# # run_bot
# # ═════════════════════════════════════════════════════════════
# async def run_bot(room_url: str, token: str, session_id: str,
#                   ws_callback: Optional[Callable] = None):
#     ensure_index()
#     e2e = []

#     lead = LeadState(ws_callback, session_id)
#     bot_interceptors[session_id] = lead

#     terminator = SessionTerminator(ws_callback, session_id)
#     transc_interceptor = TranscriptionInterceptor(lead, ws_callback, session_id, terminator)

#     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
#     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
#     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"],
#                          model="llama-3.1-8b-instant", temperature=0.6, max_tokens=160)
#     transport = DailyTransport(room_url, token, "Aria",
#         DailyParams(audio_in_enabled=True, audio_out_enabled=True,
#                     vad_analyzer=SileroVADAnalyzer()))

#     ctx_text = retrieve("WartinLabs services overview", top_k=2)
#     sys_msg = SYSTEM_TEMPLATE.format(context=ctx_text or "General WartinLabs knowledge.")
#     messages = [
#         {"role": "system",    "content": sys_msg},
#         {"role": "user",      "content": "Hello"},
#         {"role": "assistant", "content": OPENING},
#     ]
#     context = OpenAILLMContext(messages)
#     ctx_agg = llm.create_context_aggregator(context)

#     email_trigger = EmailTrigger(lead, ws_callback, session_id)
#     tracker = E2ETracker(e2e)
#     metrics = MetricsCollector()

#     pipeline = Pipeline([
#         transport.input(),
#         tracker,
#         stt,
#         transc_interceptor,
#         ctx_agg.user(),
#         llm,
#         email_trigger,
#         tts,
#         transport.output(),
#         ctx_agg.assistant(),
#         metrics,
#     ])

#     task = PipelineTask(pipeline, params=PipelineParams(
#         allow_interruptions=True, enable_metrics=True, enable_usage_metrics=True))
#     terminator.set_task(task)

#     @transport.event_handler("on_first_participant_joined")
#     async def on_first(transport, participant):
#         pid = participant["id"]
#         logger.info(f"Participant joined: {pid}")
#         await transport.capture_participant_audio(pid)
#         if ws_callback:
#             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
#         await task.queue_frames([TextFrame(OPENING)])

#     logger.warning(f"🚀 Starting pipeline for session {session_id}")
#     runner = PipelineRunner()
#     try:
#         await runner.run(task)
#     finally:
#         bot_interceptors.pop(session_id, None)

# # """
# # WartinLabs Voice Agent – Fully Optimized Pipecat Pipeline
# # Fixes:
# # - Proper context trimming (resets aggregator)
# # - Aggressive summarization for long conversations
# # - Selective RAG (skip short/filler messages)
# # - FAQ cache with fuzzy matching
# # - Structured lead collection (state machine)
# # - Reduced MAX_CONVERSATION_TURNS to 6
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable, Optional, List, Dict

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, a warm, friendly, and professional AI voice assistant for WartinLabs.

# # PERSONALITY & TONE:
# # - Be conversational, empathetic, and polite. Use phrases like "Sure thing!", "I'd be happy to help", "Let me check that for you".
# # - Responses MUST be 2-4 short sentences max (voice brevity matters). Avoid bullet points, lists, markdown.
# # - Speak numbers naturally: "ten thousand dollars" not "$10,000". Email: "info at wartinlabs dot com".

# # COMPANY DETAILS:
# # - WartinLabs office: 2217, 2nd Floor, Corenthum Tower, Noida-62, Uttar Pradesh, India. (Near Noida Electronic City Metro station)
# # - Contact: info@wartinlabs.com, phone +91 6387541924.

# # SERVICES:
# # - We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.
# # - We do NOT offer flight booking, travel reservations, or any travel agency services. If asked, politely state: "We don't provide flight booking services. Our expertise is in AI and software development."

# # HANDLING UNCLEAR OR NOISY INPUT:
# # - If you are unsure what the user said, politely ask: "I'm sorry, I didn't catch that. Could you please repeat?"
# # - Do not guess or respond with unrelated answers.

# # SENSITIVE OR HARMFUL REQUESTS:
# # - If the user asks to harm someone, commit violence, or anything illegal, immediately say: "I can't help with that request. Let's change the subject."
# # - Do not offer any advice or partial suggestions.

# # FACTUAL QUESTIONS:
# # - Answer common factual questions directly. For example: "Who is the Prime Minister of India?" → "Narendra Modi is the Prime Minister of India."
# # - Do not refuse to answer non-political factual questions.

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote, book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team.
# #   2. Collect details ONE AT A TIME in this exact order: full name, email, phone, project description, budget, preferred contact time.
# #   3. After collecting ALL six, confirm and say exactly: "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure: "Let me connect you with our team for the exact answer."
# # - NEVER fabricate prices, timelines, or client names.
# # - Keep every response SHORT for voice.
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence) and do NOT continue.
# # - If the user asks to change an email address, clearly say: "Sure, please tell me your corrected email address, and I'll update it."

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 1: FAQ Cache (fuzzy matching)
# # # ─────────────────────────────────────────────────────────────
# # FAQ_CACHE = {
# #     "office": "Our office is at 2217, 2nd Floor, Corenthum Tower, Noida-62, near the Electronic City Metro station.",
# #     "location": "We're located in Noida, Uttar Pradesh, India, at the address I just gave you.",
# #     "services": "We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.",
# #     "email": "You can email us at info at wartinlabs dot com.",
# #     "phone": "Our phone number is plus 91 63875 41924.",
# #     "pricing": "Pricing depends on the project scope. I'd be happy to connect you with our team for a custom quote.",
# #     "modi": "Narendra Modi is the Prime Minister of India.",
# #     "prime minister": "Narendra Modi is the Prime Minister of India.",
# # }

# # def get_cached_answer(text: str) -> Optional[str]:
# #     """Return cached answer if user asks a frequent question (fuzzy match)."""
# #     lower = text.lower()
# #     for key, answer in FAQ_CACHE.items():
# #         if key in lower:
# #             logger.info(f"✅ FAQ cache hit: '{key}'")
# #             return answer
# #     return None

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers and structured collection
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # Structured lead collection state machine
# # # ─────────────────────────────────────────────────────────────
# # class LeadState:
# #     def __init__(self):
# #         self.reset()
# #     def reset(self):
# #         self.name = None
# #         self.email = None
# #         self.phone = None
# #         self.project_desc = None
# #         self.budget = None
# #         self.contact_time = None
# #         self.step = 0  # 0=not active, 1=name, 2=email, 3=phone, 4=project, 5=budget, 6=time, 7=complete
# #     def is_active(self):
# #         return self.step > 0 and self.step < 7
# #     def next_question(self) -> str:
# #         if self.step == 1:
# #             return "Could you please tell me your full name?"
# #         elif self.step == 2:
# #             return "And your email address?"
# #         elif self.step == 3:
# #             return "What's your phone number?"
# #         elif self.step == 4:
# #             return "Can you briefly describe your project?"
# #         elif self.step == 5:
# #             return "What's your budget range for this project?"
# #         elif self.step == 6:
# #             return "What time of day is best to contact you?"
# #         else:
# #             return ""
# #     def process_answer(self, text: str, lead_active_flag: bool) -> tuple[str, bool]:
# #         if not lead_active_flag:
# #             if _wants_lead(text):
# #                 self.step = 1
# #                 return (self.next_question(), True)
# #             return (None, False)
# #         if self.step == 1:
# #             self.name = text.strip()
# #             self.step = 2
# #             return (self.next_question(), True)
# #         elif self.step == 2:
# #             if '@' in text and '.' in text:
# #                 self.email = text.strip()
# #                 self.step = 3
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I didn't catch a valid email address. Could you please repeat your email?", True)
# #         elif self.step == 3:
# #             if re.search(r"[\d\s\-+\(\)]{6,}", text):
# #                 self.phone = text.strip()
# #                 self.step = 4
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I need your phone number to connect you with our team. Please tell me your number.", True)
# #         elif self.step == 4:
# #             self.project_desc = text.strip()
# #             self.step = 5
# #             return (self.next_question(), True)
# #         elif self.step == 5:
# #             self.budget = text.strip()
# #             self.step = 6
# #             return (self.next_question(), True)
# #         elif self.step == 6:
# #             self.contact_time = text.strip()
# #             self.step = 7
# #             return ("To confirm, I have all your details. Our team will reach out to you within 24 hours.", False)
# #         else:
# #             return (None, False)

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – works with structured state
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_state = LeadState()
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         # No longer used, but kept for compatibility
# #         pass

# #     def get_state(self):
# #         return self._lead_state

# #     def reset_state(self):
# #         self._lead_state.reset()

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     if self._lead_state.name:
# #                         lead_data["name"] = self._lead_state.name
# #                     if self._lead_state.email:
# #                         lead_data["email"] = self._lead_state.email
# #                     if self._lead_state.phone:
# #                         lead_data["phone"] = self._lead_state.phone
# #                     if self._lead_state.project_desc:
# #                         lead_data["requirements"] = self._lead_state.project_desc
# #                     if self._lead_state.budget:
# #                         lead_data["budget"] = self._lead_state.budget
# #                     if self._lead_state.contact_time:
# #                         lead_data["contact_time"] = self._lead_state.contact_time
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session", "terminate this call",
# #     "end this call", "end the conversation", "that's all", "that is all",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "no more questions", "i'm done", "i am done", "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._triggered = False
# #         self._task = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 1s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(1.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list = e2e_list
# #         self._user_stop = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped at {self._user_stop:.3f}")
# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s (total: {len(self._e2e_list)})")
# #                 self._user_stop = None
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)
# #         self.proc = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]
# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt = getattr(val, "prompt_tokens", getattr(md, "prompt_tokens", 0))
# #                     ct = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)
# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles": {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles": self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters": sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars : {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id": session_id,
# #             "recorded_at": datetime.now().isoformat(),
# #             "ttfb": {k: v for k, v in self.ttfb.items()},
# #             "processing": {k: v for k, v in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary": self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point with aggressive trimming and summarization
# # # ─────────────────────────────────────────────────────────────
# # MAX_CONVERSATION_TURNS = 6   # system + last 5 messages (~2 exchanges)
# # SUMMARY_TRIGGER_LEN = 10     # when conversation exceeds 10 messages, generate summary

# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list = []

# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=150)

# #     # Initial context
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=2)  # reduced RAG chunks
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ─── Summarization helper ────────────────────────────────
# #     async def generate_summary(long_messages: List[Dict]) -> str:
# #         """Generate a concise summary of older conversation turns."""
# #         turns = [m for m in long_messages if m["role"] != "system"][-8:]  # last 8 non-system turns
# #         if not turns:
# #             return "The user is interested in WartinLabs services."
# #         prompt = f"Summarize this conversation in 2 short sentences focusing on user's needs and collected info:\n{json.dumps(turns)}"
# #         try:
# #             # Use the Groq service to generate summary
# #             summary_response = await llm.generate(prompt, max_tokens=80)
# #             return summary_response.strip()
# #         except Exception as e:
# #             logger.warning(f"Summary generation failed: {e}")
# #             return "The user is interested in WartinLabs services."

# #     # ─── Context trimming + summarization ────────────────────
# #     async def trim_and_summarize():
# #         nonlocal messages, context, ctx_agg
# #         if len(messages) <= MAX_CONVERSATION_TURNS:
# #             return

# #         # If conversation is very long, generate summary for older parts
# #         if len(messages) > SUMMARY_TRIGGER_LEN:
# #             # Extract messages before the last MAX_CONVERSATION_TURNS messages
# #             old_msgs = messages[1:-(MAX_CONVERSATION_TURNS - 1)]
# #             if old_msgs:
# #                 summary = await generate_summary(old_msgs)
# #                 # Replace old messages with a single assistant message containing the summary
# #                 summary_msg = {"role": "assistant", "content": f"[Previous conversation summary: {summary}]"}
# #                 # New list: system + summary + last (MAX_CONVERSATION_TURNS - 2) messages
# #                 new_msgs = [messages[0], summary_msg] + messages[-(MAX_CONVERSATION_TURNS - 2):]
# #                 messages[:] = new_msgs
# #                 logger.info(f"📝 Summarized {len(old_msgs)} messages into summary, now total {len(messages)} messages")
# #             else:
# #                 # Simple trim without summary
# #                 new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #                 messages[:] = new_msgs
# #         else:
# #             # Simple trim
# #             new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #             messages[:] = new_msgs

# #         # Replace the entire context and aggregator
# #         # This forces the LLM to use the trimmed history
# #         new_context = OpenAILLMContext(messages)
# #         # Replace the aggregator's internal context
# #         ctx_agg._context = new_context
# #         context.messages = messages
# #         logger.info(f"✂️ Trimmed context to {len(messages)} messages")

# #     # ─── Event handlers ──────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # 1. Goodbye detection
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return

# #         # 2. FAQ cache
# #         cached = get_cached_answer(user_text)
# #         if cached:
# #             logger.info("⚡ Using cached answer")
# #             # Append to conversation history
# #             messages.append({"role": "assistant", "content": cached})
# #             # Update context
# #             context.messages.append({"role": "assistant", "content": cached})
# #             await task.queue_frames([TextFrame(cached)])
# #             return

# #         # 3. Structured lead collection
# #         lead_state = interceptor.get_state()
# #         if lead_state.is_active() or _wants_lead(user_text):
# #             bot_reply, still_active = lead_state.process_answer(user_text, lead_state.is_active())
# #             if bot_reply:
# #                 # Append to conversation history
# #                 messages.append({"role": "assistant", "content": bot_reply})
# #                 context.messages.append({"role": "assistant", "content": bot_reply})
# #                 await task.queue_frames([TextFrame(bot_reply)])
# #                 if not still_active:
# #                     lead_active = True
# #                     interceptor.mark_lead_active()
# #                     # Reset state after completion (email will be sent by interceptor)
# #                     interceptor.reset_state()
# #                 return

# #         # 4. Selective RAG – skip for short/filler messages
# #         filler_words = {"yes", "no", "okay", "ok", "sure", "thanks", "thank you", "hello", "hi", "hey", "yeah", "yep", "got it", "uh huh", "hmm"}
# #         if len(user_text) < 15 or user_text.lower() in filler_words:
# #             rag_context = "General WartinLabs knowledge. The user is asking a short question or acknowledging."
# #             logger.debug("Skipping RAG (short/filler message)")
# #         else:
# #             new_ctx = retrieve(user_text, top_k=2)
# #             rag_context = new_ctx or "General WartinLabs knowledge."

# #         # Update system prompt
# #         new_system = SYSTEM_TEMPLATE.format(context=rag_context)
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Trim and summarize after each user turn
# #         await trim_and_summarize()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Fully Optimized Pipecat Pipeline
# # Fixes:
# # - Proper context trimming (resets aggregator)
# # - Aggressive summarization for long conversations
# # - Selective RAG (skip short/filler messages)
# # - FAQ cache with fuzzy matching
# # - Structured lead collection (state machine)
# # - Reduced MAX_CONVERSATION_TURNS to 6
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable, Optional, List, Dict

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, a warm, friendly, and professional AI voice assistant for WartinLabs.

# # PERSONALITY & TONE:
# # - Be conversational, empathetic, and polite. Use phrases like "Sure thing!", "I'd be happy to help", "Let me check that for you".
# # - Responses MUST be 2-4 short sentences max (voice brevity matters). Avoid bullet points, lists, markdown.
# # - Speak numbers naturally: "ten thousand dollars" not "$10,000". Email: "info at wartinlabs dot com".

# # COMPANY DETAILS:
# # - WartinLabs office: 2217, 2nd Floor, Corenthum Tower, Noida-62, Uttar Pradesh, India. (Near Noida Electronic City Metro station)
# # - Contact: info@wartinlabs.com, phone +91 6387541924.

# # SERVICES:
# # - We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.
# # - We do NOT offer flight booking, travel reservations, or any travel agency services. If asked, politely state: "We don't provide flight booking services. Our expertise is in AI and software development."

# # HANDLING UNCLEAR OR NOISY INPUT:
# # - If you are unsure what the user said, politely ask: "I'm sorry, I didn't catch that. Could you please repeat?"
# # - Do not guess or respond with unrelated answers.

# # SENSITIVE OR HARMFUL REQUESTS:
# # - If the user asks to harm someone, commit violence, or anything illegal, immediately say: "I can't help with that request. Let's change the subject."
# # - Do not offer any advice or partial suggestions.

# # FACTUAL QUESTIONS:
# # - Answer common factual questions directly. For example: "Who is the Prime Minister of India?" → "Narendra Modi is the Prime Minister of India."
# # - Do not refuse to answer non-political factual questions.

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote, book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team.
# #   2. Collect details ONE AT A TIME in this exact order: full name, email, phone, project description, budget, preferred contact time.
# #   3. After collecting ALL six, confirm and say exactly: "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure: "Let me connect you with our team for the exact answer."
# # - NEVER fabricate prices, timelines, or client names.
# # - Keep every response SHORT for voice.
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence) and do NOT continue.
# # - If the user asks to change an email address, clearly say: "Sure, please tell me your corrected email address, and I'll update it."

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 1: FAQ Cache (fuzzy matching)
# # # ─────────────────────────────────────────────────────────────
# # FAQ_CACHE = {
# #     "office": "Our office is at 2217, 2nd Floor, Corenthum Tower, Noida-62, near the Electronic City Metro station.",
# #     "location": "We're located in Noida, Uttar Pradesh, India, at the address I just gave you.",
# #     "services": "We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.",
# #     "email": "You can email us at info at wartinlabs dot com.",
# #     "phone": "Our phone number is plus 91 63875 41924.",
# #     "pricing": "Pricing depends on the project scope. I'd be happy to connect you with our team for a custom quote.",
# #     "modi": "Narendra Modi is the Prime Minister of India.",
# #     "prime minister": "Narendra Modi is the Prime Minister of India.",
# # }

# # def get_cached_answer(text: str) -> Optional[str]:
# #     """Return cached answer if user asks a frequent question (fuzzy match)."""
# #     lower = text.lower()
# #     for key, answer in FAQ_CACHE.items():
# #         if key in lower:
# #             logger.info(f"✅ FAQ cache hit: '{key}'")
# #             return answer
# #     return None

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers and structured collection
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # Structured lead collection state machine
# # # ─────────────────────────────────────────────────────────────
# # class LeadState:
# #     def __init__(self):
# #         self.reset()
# #     def reset(self):
# #         self.name = None
# #         self.email = None
# #         self.phone = None
# #         self.project_desc = None
# #         self.budget = None
# #         self.contact_time = None
# #         self.step = 0  # 0=not active, 1=name, 2=email, 3=phone, 4=project, 5=budget, 6=time, 7=complete
# #     def is_active(self):
# #         return self.step > 0 and self.step < 7
# #     def next_question(self) -> str:
# #         if self.step == 1:
# #             return "Could you please tell me your full name?"
# #         elif self.step == 2:
# #             return "And your email address?"
# #         elif self.step == 3:
# #             return "What's your phone number?"
# #         elif self.step == 4:
# #             return "Can you briefly describe your project?"
# #         elif self.step == 5:
# #             return "What's your budget range for this project?"
# #         elif self.step == 6:
# #             return "What time of day is best to contact you?"
# #         else:
# #             return ""
# #     def process_answer(self, text: str, lead_active_flag: bool) -> tuple[str, bool]:
# #         if not lead_active_flag:
# #             if _wants_lead(text):
# #                 self.step = 1
# #                 return (self.next_question(), True)
# #             return (None, False)
# #         if self.step == 1:
# #             self.name = text.strip()
# #             self.step = 2
# #             return (self.next_question(), True)
# #         elif self.step == 2:
# #             if '@' in text and '.' in text:
# #                 self.email = text.strip()
# #                 self.step = 3
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I didn't catch a valid email address. Could you please repeat your email?", True)
# #         elif self.step == 3:
# #             if re.search(r"[\d\s\-+\(\)]{6,}", text):
# #                 self.phone = text.strip()
# #                 self.step = 4
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I need your phone number to connect you with our team. Please tell me your number.", True)
# #         elif self.step == 4:
# #             self.project_desc = text.strip()
# #             self.step = 5
# #             return (self.next_question(), True)
# #         elif self.step == 5:
# #             self.budget = text.strip()
# #             self.step = 6
# #             return (self.next_question(), True)
# #         elif self.step == 6:
# #             self.contact_time = text.strip()
# #             self.step = 7
# #             return ("To confirm, I have all your details. Our team will reach out to you within 24 hours.", False)
# #         else:
# #             return (None, False)

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – works with structured state
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_state = LeadState()
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         # No longer used, but kept for compatibility
# #         pass

# #     def get_state(self):
# #         return self._lead_state

# #     def reset_state(self):
# #         self._lead_state.reset()

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     if self._lead_state.name:
# #                         lead_data["name"] = self._lead_state.name
# #                     if self._lead_state.email:
# #                         lead_data["email"] = self._lead_state.email
# #                     if self._lead_state.phone:
# #                         lead_data["phone"] = self._lead_state.phone
# #                     if self._lead_state.project_desc:
# #                         lead_data["requirements"] = self._lead_state.project_desc
# #                     if self._lead_state.budget:
# #                         lead_data["budget"] = self._lead_state.budget
# #                     if self._lead_state.contact_time:
# #                         lead_data["contact_time"] = self._lead_state.contact_time
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session", "terminate this call",
# #     "end this call", "end the conversation", "that's all", "that is all",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "no more questions", "i'm done", "i am done", "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._triggered = False
# #         self._task = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 1s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(1.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list = e2e_list
# #         self._user_stop = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped at {self._user_stop:.3f}")
# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s (total: {len(self._e2e_list)})")
# #                 self._user_stop = None
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)
# #         self.proc = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]
# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt = getattr(val, "prompt_tokens", getattr(md, "prompt_tokens", 0))
# #                     ct = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)
# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles": {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles": self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters": sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars ")
# # """
# # WartinLabs Voice Agent – Fully Optimized Pipecat Pipeline
# # Fixes:
# # - Proper context trimming (resets aggregator)
# # - Aggressive summarization for long conversations
# # - Selective RAG (skip short/filler messages)
# # - FAQ cache with fuzzy matching
# # - Structured lead collection (state machine)
# # - Reduced MAX_CONVERSATION_TURNS to 6
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable, Optional, List, Dict

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, a warm, friendly, and professional AI voice assistant for WartinLabs.

# # PERSONALITY & TONE:
# # - Be conversational, empathetic, and polite. Use phrases like "Sure thing!", "I'd be happy to help", "Let me check that for you".
# # - Responses MUST be 2-4 short sentences max (voice brevity matters). Avoid bullet points, lists, markdown.
# # - Speak numbers naturally: "ten thousand dollars" not "$10,000". Email: "info at wartinlabs dot com".

# # COMPANY DETAILS:
# # - WartinLabs office: 2217, 2nd Floor, Corenthum Tower, Noida-62, Uttar Pradesh, India. (Near Noida Electronic City Metro station)
# # - Contact: info@wartinlabs.com, phone +91 6387541924.

# # SERVICES:
# # - We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.
# # - We do NOT offer flight booking, travel reservations, or any travel agency services. If asked, politely state: "We don't provide flight booking services. Our expertise is in AI and software development."

# # HANDLING UNCLEAR OR NOISY INPUT:
# # - If you are unsure what the user said, politely ask: "I'm sorry, I didn't catch that. Could you please repeat?"
# # - Do not guess or respond with unrelated answers.

# # SENSITIVE OR HARMFUL REQUESTS:
# # - If the user asks to harm someone, commit violence, or anything illegal, immediately say: "I can't help with that request. Let's change the subject."
# # - Do not offer any advice or partial suggestions.

# # FACTUAL QUESTIONS:
# # - Answer common factual questions directly. For example: "Who is the Prime Minister of India?" → "Narendra Modi is the Prime Minister of India."
# # - Do not refuse to answer non-political factual questions.

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote, book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team.
# #   2. Collect details ONE AT A TIME in this exact order: full name, email, phone, project description, budget, preferred contact time.
# #   3. After collecting ALL six, confirm and say exactly: "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure: "Let me connect you with our team for the exact answer."
# # - NEVER fabricate prices, timelines, or client names.
# # - Keep every response SHORT for voice.
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence) and do NOT continue.
# # - If the user asks to change an email address, clearly say: "Sure, please tell me your corrected email address, and I'll update it."

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 1: FAQ Cache (fuzzy matching)
# # # ─────────────────────────────────────────────────────────────
# # FAQ_CACHE = {
# #     "office": "Our office is at 2217, 2nd Floor, Corenthum Tower, Noida-62, near the Electronic City Metro station.",
# #     "location": "We're located in Noida, Uttar Pradesh, India, at the address I just gave you.",
# #     "services": "We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.",
# #     "email": "You can email us at info at wartinlabs dot com.",
# #     "phone": "Our phone number is plus 91 63875 41924.",
# #     "pricing": "Pricing depends on the project scope. I'd be happy to connect you with our team for a custom quote.",
# #     "modi": "Narendra Modi is the Prime Minister of India.",
# #     "prime minister": "Narendra Modi is the Prime Minister of India.",
# # }

# # def get_cached_answer(text: str) -> Optional[str]:
# #     """Return cached answer if user asks a frequent question (fuzzy match)."""
# #     lower = text.lower()
# #     for key, answer in FAQ_CACHE.items():
# #         if key in lower:
# #             logger.info(f"✅ FAQ cache hit: '{key}'")
# #             return answer
# #     return None

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers and structured collection
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # Structured lead collection state machine
# # # ─────────────────────────────────────────────────────────────
# # class LeadState:
# #     def __init__(self):
# #         self.reset()
# #     def reset(self):
# #         self.name = None
# #         self.email = None
# #         self.phone = None
# #         self.project_desc = None
# #         self.budget = None
# #         self.contact_time = None
# #         self.step = 0  # 0=not active, 1=name, 2=email, 3=phone, 4=project, 5=budget, 6=time, 7=complete
# #     def is_active(self):
# #         return self.step > 0 and self.step < 7
# #     def next_question(self) -> str:
# #         if self.step == 1:
# #             return "Could you please tell me your full name?"
# #         elif self.step == 2:
# #             return "And your email address?"
# #         elif self.step == 3:
# #             return "What's your phone number?"
# #         elif self.step == 4:
# #             return "Can you briefly describe your project?"
# #         elif self.step == 5:
# #             return "What's your budget range for this project?"
# #         elif self.step == 6:
# #             return "What time of day is best to contact you?"
# #         else:
# #             return ""
# #     def process_answer(self, text: str, lead_active_flag: bool) -> tuple[str, bool]:
# #         if not lead_active_flag:
# #             if _wants_lead(text):
# #                 self.step = 1
# #                 return (self.next_question(), True)
# #             return (None, False)
# #         if self.step == 1:
# #             self.name = text.strip()
# #             self.step = 2
# #             return (self.next_question(), True)
# #         elif self.step == 2:
# #             if '@' in text and '.' in text:
# #                 self.email = text.strip()
# #                 self.step = 3
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I didn't catch a valid email address. Could you please repeat your email?", True)
# #         elif self.step == 3:
# #             if re.search(r"[\d\s\-+\(\)]{6,}", text):
# #                 self.phone = text.strip()
# #                 self.step = 4
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I need your phone number to connect you with our team. Please tell me your number.", True)
# #         elif self.step == 4:
# #             self.project_desc = text.strip()
# #             self.step = 5
# #             return (self.next_question(), True)
# #         elif self.step == 5:
# #             self.budget = text.strip()
# #             self.step = 6
# #             return (self.next_question(), True)
# #         elif self.step == 6:
# #             self.contact_time = text.strip()
# #             self.step = 7
# #             return ("To confirm, I have all your details. Our team will reach out to you within 24 hours.", False)
# #         else:
# #             return (None, False)

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – works with structured state
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_state = LeadState()
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         # No longer used, but kept for compatibility
# #         pass

# #     def get_state(self):
# #         return self._lead_state

# #     def reset_state(self):
# #         self._lead_state.reset()

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     if self._lead_state.name:
# #                         lead_data["name"] = self._lead_state.name
# #                     if self._lead_state.email:
# #                         lead_data["email"] = self._lead_state.email
# #                     if self._lead_state.phone:
# #                         lead_data["phone"] = self._lead_state.phone
# #                     if self._lead_state.project_desc:
# #                         lead_data["requirements"] = self._lead_state.project_desc
# #                     if self._lead_state.budget:
# #                         lead_data["budget"] = self._lead_state.budget
# #                     if self._lead_state.contact_time:
# #                         lead_data["contact_time"] = self._lead_state.contact_time
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session", "terminate this call",
# #     "end this call", "end the conversation", "that's all", "that is all",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "no more questions", "i'm done", "i am done", "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._triggered = False
# #         self._task = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 1s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(1.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list = e2e_list
# #         self._user_stop = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped at {self._user_stop:.3f}")
# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s (total: {len(self._e2e_list)})")
# #                 self._user_stop = None
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)
# #         self.proc = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]
# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt = getattr(val, "prompt_tokens", getattr(md, "prompt_tokens", 0))
# #                     ct = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)
# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles": {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles": self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters": sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars : {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id": session_id,
# #             "recorded_at": datetime.now().isoformat(),
# #             "ttfb": {k: v for k, v in self.ttfb.items()},
# #             "processing": {k: v for k, v in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary": self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point with aggressive trimming and summarization
# # # ─────────────────────────────────────────────────────────────
# # MAX_CONVERSATION_TURNS = 6   # system + last 5 messages (~2 exchanges)
# # SUMMARY_TRIGGER_LEN = 10     # when conversation exceeds 10 messages, generate summary

# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list = []

# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=150)

# #     # Initial context
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=2)  # reduced RAG chunks
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ─── Summarization helper ────────────────────────────────
# #     async def generate_summary(long_messages: List[Dict]) -> str:
# #         """Generate a concise summary of older conversation turns."""
# #         turns = [m for m in long_messages if m["role"] != "system"][-8:]  # last 8 non-system turns
# #         if not turns:
# #             return "The user is interested in WartinLabs services."
# #         prompt = f"Summarize this conversation in 2 short sentences focusing on user's needs and collected info:\n{json.dumps(turns)}"
# #         try:
# #             # Use the Groq service to generate summary
# #             summary_response = await llm.generate(prompt, max_tokens=80)
# #             return summary_response.strip()
# #         except Exception as e:
# #             logger.warning(f"Summary generation failed: {e}")
# #             return "The user is interested in WartinLabs services."

# #     # ─── Context trimming + summarization ────────────────────
# #     async def trim_and_summarize():
# #         nonlocal messages, context, ctx_agg
# #         if len(messages) <= MAX_CONVERSATION_TURNS:
# #             return

# #         # If conversation is very long, generate summary for older parts
# #         if len(messages) > SUMMARY_TRIGGER_LEN:
# #             # Extract messages before the last MAX_CONVERSATION_TURNS messages
# #             old_msgs = messages[1:-(MAX_CONVERSATION_TURNS - 1)]
# #             if old_msgs:
# #                 summary = await generate_summary(old_msgs)
# #                 # Replace old messages with a single assistant message containing the summary
# #                 summary_msg = {"role": "assistant", "content": f"[Previous conversation summary: {summary}]"}
# #                 # New list: system + summary + last (MAX_CONVERSATION_TURNS - 2) messages
# #                 new_msgs = [messages[0], summary_msg] + messages[-(MAX_CONVERSATION_TURNS - 2):]
# #                 messages[:] = new_msgs
# #                 logger.info(f"📝 Summarized {len(old_msgs)} messages into summary, now total {len(messages)} messages")
# #             else:
# #                 # Simple trim without summary
# #                 new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #                 messages[:] = new_msgs
# #         else:
# #             # Simple trim
# #             new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #             messages[:] = new_msgs

# #         # Replace the entire context and aggregator
# #         # This forces the LLM to use the trimmed history
# #         new_context = OpenAILLMContext(messages)
# #         # Replace the aggregator's internal context
# #         ctx_agg._context = new_context
# #         context.messages = messages
# #         logger.info(f"✂️ Trimmed context to {len(messages)} messages")

# #     # ─── Event handlers ──────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # 1. Goodbye detection
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return

# #         # 2. FAQ cache
# #         cached = get_cached_answer(user_text)
# #         if cached:
# #             logger.info("⚡ Using cached answer")
# #             # Append to conversation history
# #             messages.append({"role": "assistant", "content": cached})
# #             # Update context
# #             context.messages.append({"role": "assistant", "content": cached})
# #             await task.queue_frames([TextFrame(cached)])
# #             return

# #         # 3. Structured lead collection
# #         lead_state = interceptor.get_state()
# #         if lead_state.is_active() or _wants_lead(user_text):
# #             bot_reply, still_active = lead_state.process_answer(user_text, lead_state.is_active())
# #             if bot_reply:
# #                 # Append to conversation history
# #                 messages.append({"role": "assistant", "content": bot_reply})
# #                 context.messages.append({"role": "assistant", "content": bot_reply})
# #                 await task.queue_frames([TextFrame(bot_reply)])
# #                 if not still_active:
# #                     lead_active = True
# #                     interceptor.mark_lead_active()
# #                     # Reset state after completion (email will be sent by interceptor)
# #                     interceptor.reset_state()
# #                 return

# #         # 4. Selective RAG – skip for short/filler messages
# #         filler_words = {"yes", "no", "okay", "ok", "sure", "thanks", "thank you", "hello", "hi", "hey", "yeah", "yep", "got it", "uh huh", "hmm"}
# #         if len(user_text) < 15 or user_text.lower() in filler_words:
# #             rag_context = "General WartinLabs knowledge. The user is asking a short question or acknowledging."
# #             logger.debug("Skipping RAG (short/filler message)")
# #         else:
# #             new_ctx = retrieve(user_text, top_k=2)
# #             rag_context = new_ctx or "General WartinLabs knowledge."

# #         # Update system prompt
# #         new_system = SYSTEM_TEMPLATE.format(context=rag_context)
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Trim and summarize after each user turn
# #         await trim_and_summarize()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Optimized Pipecat Pipeline
# # Improvements:
# # - Selective RAG (skip short/filler messages)
# # - FAQ cache (static answers)
# # - Structured lead collection (state machine)
# # - Conversation summarization (sliding window + summary)
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict, deque
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable, Dict, Optional

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt – unchanged
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, a warm, friendly, and professional AI voice assistant for WartinLabs.

# # PERSONALITY & TONE:
# # - Be conversational, empathetic, and polite. Use phrases like "Sure thing!", "I'd be happy to help", "Let me check that for you".
# # - Responses MUST be 2-4 short sentences max (voice brevity matters). Avoid bullet points, lists, markdown.
# # - Speak numbers naturally: "ten thousand dollars" not "$10,000". Email: "info at wartinlabs dot com".

# # COMPANY DETAILS:
# # - WartinLabs office: 2217, 2nd Floor, Corenthum Tower, Noida-62, Uttar Pradesh, India. (Near Noida Electronic City Metro station)
# # - Contact: info@wartinlabs.com, phone +91 6387541924.

# # SERVICES:
# # - We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.
# # - We do NOT offer flight booking, travel reservations, or any travel agency services. If asked, politely state: "We don't provide flight booking services. Our expertise is in AI and software development."

# # HANDLING UNCLEAR OR NOISY INPUT:
# # - If you are unsure what the user said, politely ask: "I'm sorry, I didn't catch that. Could you please repeat?"
# # - Do not guess or respond with unrelated answers.

# # SENSITIVE OR HARMFUL REQUESTS:
# # - If the user asks to harm someone, commit violence, or anything illegal, immediately say: "I can't help with that request. Let's change the subject."
# # - Do not offer any advice or partial suggestions.

# # FACTUAL QUESTIONS:
# # - Answer common factual questions directly. For example: "Who is the Prime Minister of India?" → "Narendra Modi is the Prime Minister of India."
# # - Do not refuse to answer non-political factual questions.

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote, book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team.
# #   2. Collect details ONE AT A TIME in this exact order: full name, email, phone, project description, budget, preferred contact time.
# #   3. After collecting ALL six, confirm and say exactly: "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure: "Let me connect you with our team for the exact answer."
# # - NEVER fabricate prices, timelines, or client names.
# # - Keep every response SHORT for voice.
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence) and do NOT continue.
# # - If the user asks to change an email address, clearly say: "Sure, please tell me your corrected email address, and I'll update it."

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 1: FAQ Cache (hardcoded answers)
# # # ─────────────────────────────────────────────────────────────
# # FAQ_CACHE = {
# #     "office": "Our office is at 2217, 2nd Floor, Corenthum Tower, Noida-62, near the Electronic City Metro station.",
# #     "location": "We're located in Noida, Uttar Pradesh, India, at the address I just gave you.",
# #     "services": "We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.",
# #     "email": "You can email us at info at wartinlabs dot com.",
# #     "phone": "Our phone number is plus 91 63875 41924.",
# #     "pricing": "Pricing depends on the project scope. I'd be happy to connect you with our team for a custom quote.",
# #     "modi": "Narendra Modi is the Prime Minister of India.",
# #     "prime minister": "Narendra Modi is the Prime Minister of India.",
# # }

# # def get_cached_answer(text: str) -> Optional[str]:
# #     """Return cached answer if user asks a frequent question."""
# #     lower = text.lower()
# #     for key, answer in FAQ_CACHE.items():
# #         if key in lower:
# #             logger.info(f"✅ FAQ cache hit: '{key}'")
# #             return answer
# #     return None

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers (modified for structured collection)
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     # Same as before – unchanged
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 3: Structured lead collection (state machine)
# # # ─────────────────────────────────────────────────────────────
# # class LeadState:
# #     def __init__(self):
# #         self.reset()
# #     def reset(self):
# #         self.name = None
# #         self.email = None
# #         self.phone = None
# #         self.project_desc = None
# #         self.budget = None
# #         self.contact_time = None
# #         self.step = 0  # 0=not active, 1=name, 2=email, 3=phone, 4=project, 5=budget, 6=time, 7=complete
# #     def is_active(self):
# #         return self.step > 0 and self.step < 7
# #     def next_question(self) -> str:
# #         if self.step == 1:
# #             return "Could you please tell me your full name?"
# #         elif self.step == 2:
# #             return "And your email address?"
# #         elif self.step == 3:
# #             return "What's your phone number?"
# #         elif self.step == 4:
# #             return "Can you briefly describe your project?"
# #         elif self.step == 5:
# #             return "What's your budget range for this project?"
# #         elif self.step == 6:
# #             return "What time of day is best to contact you?"
# #         else:
# #             return ""
# #     def process_answer(self, text: str, lead_active_flag: bool) -> tuple[str, bool]:
# #         """Returns (bot_response, should_continue_lead)"""
# #         if not lead_active_flag:
# #             if _wants_lead(text):
# #                 self.step = 1
# #                 return (self.next_question(), True)
# #             return (None, False)
# #         # Lead active – collect data
# #         if self.step == 1:
# #             self.name = text.strip()
# #             self.step = 2
# #             return (self.next_question(), True)
# #         elif self.step == 2:
# #             if '@' in text and '.' in text:
# #                 self.email = text.strip()
# #                 self.step = 3
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I didn't catch a valid email address. Could you please repeat your email?", True)
# #         elif self.step == 3:
# #             # simple phone number validation
# #             if re.search(r"[\d\s\-+\(\)]{6,}", text):
# #                 self.phone = text.strip()
# #                 self.step = 4
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I need your phone number to connect you with our team. Please tell me your number.", True)
# #         elif self.step == 4:
# #             self.project_desc = text.strip()
# #             self.step = 5
# #             return (self.next_question(), True)
# #         elif self.step == 5:
# #             self.budget = text.strip()
# #             self.step = 6
# #             return (self.next_question(), True)
# #         elif self.step == 6:
# #             self.contact_time = text.strip()
# #             self.step = 7
# #             # Mark complete; email will be sent by the interceptor
# #             return ("To confirm, I have all your details. Our team will reach out to you within 24 hours.", False)
# #         else:
# #             return (None, False)

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – updated to work with structured state
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_state = LeadState()
# #         self._email_sent = False

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     # Also fill from structured state if missing
# #                     if self._lead_state.name:
# #                         lead_data["name"] = self._lead_state.name
# #                     if self._lead_state.email:
# #                         lead_data["email"] = self._lead_state.email
# #                     if self._lead_state.phone:
# #                         lead_data["phone"] = self._lead_state.phone
# #                     if self._lead_state.project_desc:
# #                         lead_data["requirements"] = self._lead_state.project_desc
# #                     if self._lead_state.budget:
# #                         lead_data["budget"] = self._lead_state.budget
# #                     if self._lead_state.contact_time:
# #                         lead_data["contact_time"] = self._lead_state.contact_time
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     def get_state(self):
# #         return self._lead_state

# #     def reset_state(self):
# #         self._lead_state.reset()

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session", "terminate this call",
# #     "end this call", "end the conversation", "that's all", "that is all",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "no more questions", "i'm done", "i am done", "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._triggered = False
# #         self._task = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 1s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(1.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list = e2e_list
# #         self._user_stop = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped at {self._user_stop:.3f}")
# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s (total: {len(self._e2e_list)})")
# #                 self._user_stop = None
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)
# #         self.proc = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]
# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt = getattr(val, "prompt_tokens", getattr(md, "prompt_tokens", 0))
# #                     ct = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)
# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles": {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles": self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters": sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars : {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id": session_id,
# #             "recorded_at": datetime.now().isoformat(),
# #             "ttfb": {k: v for k, v in self.ttfb.items()},
# #             "processing": {k: v for k, v in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary": self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point with all optimizations
# # # ─────────────────────────────────────────────────────────────
# # MAX_CONVERSATION_TURNS = 8  # keep system + last 7 messages (~3 exchanges)
# # SUMMARY_TRIGGER_LEN = 8     # when conversation exceeds this, generate summary

# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list = []

# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=200)

# #     # Initial context
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ─── OPTIMIZATION 4: Conversation summarization ──────────
# #     async def generate_summary(long_messages: list) -> str:
# #         """Use Groq LLM to summarize conversation history."""
# #         # Take messages excluding system, keep only last 10 turns
# #         turns = [m for m in long_messages if m["role"] != "system"][-10:]
# #         prompt = f"Summarize this conversation in 2-3 short sentences, focusing on user's intent and collected info:\n{json.dumps(turns)}"
# #         try:
# #             summary_response = await llm.generate(prompt, max_tokens=100)
# #             return summary_response.strip()
# #         except Exception as e:
# #             logger.warning(f"Summary generation failed: {e}")
# #             return "The user is interested in WartinLabs services."

# #     # ─── Context trimming + summarization ────────────────────
# #     def trim_and_summarize():
# #         nonlocal messages, context
# #         if len(messages) <= MAX_CONVERSATION_TURNS:
# #             return
# #         # If conversation is very long, generate a summary
# #         if len(messages) > SUMMARY_TRIGGER_LEN:
# #             # Create a summary asynchronously (fire-and-forget)
# #             asyncio.create_task(do_summarization())
# #         # Keep system + last (MAX_CONVERSATION_TURNS - 1) messages
# #         new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #         messages[:] = new_msgs
# #         context.messages.clear()
# #         context.messages.extend(new_msgs)
# #         logger.info(f"✂️ Trimmed context to {len(messages)} messages")

# #     async def do_summarization():
# #         # Generate summary of older messages (before the trimmed part)
# #         # For simplicity, we'll just replace system message with a summary note
# #         # But this is optional; we'll just log
# #         logger.info("📝 Long conversation detected – summary would be generated here (optional)")

# #     # ─── Event handlers ──────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # 1. Goodbye detection
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return

# #         # 2. OPTIMIZATION 2: FAQ cache
# #         cached = get_cached_answer(user_text)
# #         if cached:
# #             # Send cached answer directly without LLM call
# #             logger.info("⚡ Using cached answer")
# #             await task.queue_frames([TextFrame(cached)])
# #             # Also add to conversation history
# #             messages.append({"role": "assistant", "content": cached})
# #             context.messages.append({"role": "assistant", "content": cached})
# #             return

# #         # 3. OPTIMIZATION 3: Structured lead collection
# #         lead_state = interceptor.get_state()
# #         if lead_state.is_active() or _wants_lead(user_text):
# #             bot_reply, still_active = lead_state.process_answer(user_text, lead_state.is_active())
# #             if bot_reply:
# #                 await task.queue_frames([TextFrame(bot_reply)])
# #                 messages.append({"role": "assistant", "content": bot_reply})
# #                 context.messages.append({"role": "assistant", "content": bot_reply})
# #                 if not still_active:
# #                     # Lead collection complete – mark lead_active for email
# #                     lead_active = True
# #                     interceptor.mark_lead_active()
# #                 return

# #         # 4. OPTIMIZATION 1: Selective RAG – skip for short/filler messages
# #         filler_words = {"yes", "no", "okay", "ok", "sure", "thanks", "thank you", "hello", "hi", "hey"}
# #         if len(user_text) < 15 or user_text.lower() in filler_words:
# #             rag_context = "General WartinLabs knowledge."  # no retrieval
# #             logger.debug("Skipping RAG (short/filler message)")
# #         else:
# #             new_ctx = retrieve(user_text, top_k=2)  # reduced from 4 to 2 for speed
# #             rag_context = new_ctx or "General WartinLabs knowledge."

# #         # Update system prompt with RAG context
# #         new_system = SYSTEM_TEMPLATE.format(context=rag_context)
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Lead detection (only if not already in structured lead)
# #         if not lead_active and not lead_state.is_active() and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()
# #             # The structured lead will take over from next turn

# #         # Trim context
# #         trim_and_summarize()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)
# # STT       : Deepgram Nova-2
# # TTS       : Deepgram Aura
# # LLM       : Groq llama-3.1-8b-instant
# # VAD       : Silero (local)
# # RAG       : FAISS + MiniLM (local)
# # Metrics   : E2E + TTFB + Processing + Tokens + TTS chars → JSON file
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
# #     EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt – UPDATED with goodbye rule
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence max), do NOT continue the conversation
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – between LLM and TTS
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages    = messages
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator – ends session when user says goodbye
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "that's all", "that is all", "no more questions", "i'm done", "i am done",
# #     "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._triggered   = False
# #         self._task        = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 4s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(4.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)


# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker – sits at START of pipeline, catches VAD frames
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list   = e2e_list
# #         self._user_stop  = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped speaking at {self._user_stop:.3f}")

# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s  (total recorded: {len(self._e2e_list)})")
# #                 self._user_stop = None

# #         await self.push_frame(frame, direction)


# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector – reads MetricsFrame from pipeline
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb       = defaultdict(list)
# #         self.proc       = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars  = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]

# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt  = getattr(val, "prompt_tokens",     getattr(md, "prompt_tokens",     0))
# #                     ct  = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)

# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles":       {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles":        self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens":      sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens":  sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters":         sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements recorded")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars : {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id":    session_id,
# #             "recorded_at":   datetime.now().isoformat(),
# #             "ttfb":          {k: v for k, v in self.ttfb.items()},
# #             "processing":    {k: v for k, v in self.proc.items()},
# #             "llm_tokens":    self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary":       self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list    = []          # shared list filled by E2ETracker

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),  # more sensitive VAD
# #         ),
# #     )
 
# #     # ── Services ─────────────────────────────────────────────
# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=350)

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Processors ───────────────────────────────────────────
# #     interceptor       = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker       = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator        = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     # ── Pipeline ─────────────────────────────────────────────
# #     # E2ETracker FIRST so it sees UserStoppedSpeakingFrame / BotStartedSpeakingFrame
# #     # MetricsCollector LAST so it sees MetricsFrames from all services
# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,            # ← catches VAD frames for E2E timing
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,            # ← email trigger
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,      # ← catches MetricsFrames
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ── Event handlers ────────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # ── Goodbye detection FIRST, before anything else ────
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase detected: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return   # ← IMPORTANT: do NOT process further (no RAG refresh, no lead detection, no LLM response)

# #         # ── RAG context refresh ──────────────────────────────
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # ── Lead detection ───────────────────────────────────
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline with Full Metrics (including E2E)
# # Metrics: Built-in + FrameProcessor that reads frame.data + custom E2E via transport events
# # Session metrics saved to JSON file.
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData, LLMUsageMetricsData, TTSUsageMetricsData

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}
# #     # Name
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break
# #     # Email
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]
# #     # Phone
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break
# #     # Requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(kw in msg["content"].lower() for kw in ["e-commerce", "platform", "system", "application", "features", "looking to", "you want", "module"]):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break
# #     # Budget
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break
# #     # Contact time
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break
# #     # Defaults
# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_active = False
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # Enhanced Metrics Collector (fixed for Pipecat 0.0.77)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self, e2e_list: list = None):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)      # processor -> list of TTFB values
# #         self.proc = defaultdict(list)      # processor -> list of processing times
# #         self.llm_tokens = []               # list of (prompt, completion)
# #         self.tts_chars = []                # list of character counts
# #         self.e2e = e2e_list if e2e_list is not None else []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, MetricsFrame):
# #             metrics_list = []
# #             if hasattr(frame, 'data'):
# #                 data = frame.data
# #                 if isinstance(data, list):
# #                     metrics_list = data
# #                 elif data:
# #                     metrics_list = [data]
# #             elif hasattr(frame, 'metrics_data'):
# #                 metrics_list = [frame.metrics_data] if frame.metrics_data else []
# #             else:
# #                 logger.warning(f"MetricsFrame has no data or metrics_data: {dir(frame)}")
# #                 await self.push_frame(frame, direction)
# #                 return

# #             for md in metrics_list:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                     logger.info(f"📈 TTFB [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                     logger.info(f"⏱️ Processing [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     # Pipecat 0.0.77 stores token usage inside md.value
# #                     if hasattr(md, 'value') and hasattr(md.value, 'prompt_tokens'):
# #                         prompt_tokens = md.value.prompt_tokens
# #                         completion_tokens = md.value.completion_tokens
# #                     else:
# #                         # fallback for other versions
# #                         prompt_tokens = getattr(md, 'prompt_tokens', 0)
# #                         completion_tokens = getattr(md, 'completion_tokens', 0)
# #                     self.llm_tokens.append((prompt_tokens, completion_tokens))
# #                     logger.info(f"🎯 LLM tokens: prompt={prompt_tokens}, completion={completion_tokens}")
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     # TTS usage: md.value is the character count
# #                     self.tts_chars.append(md.value)
# #                     logger.info(f"🔊 TTS chars: {md.value}")
# #                 else:
# #                     logger.debug(f"Unknown metrics data type: {type(md)}")

# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _get_percentiles(values, percentiles=[50, 90, 95, 99]):
# #         if not values:
# #             return {}
# #         sorted_vals = sorted(values)
# #         return {p: sorted_vals[int(len(sorted_vals) * p / 100)] for p in percentiles}

# #     def report_summary(self):
# #         logger.info("=" * 60)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info("=" * 60)
# #         for proc in sorted(self.ttfb.keys()):
# #             p = self._get_percentiles(self.ttfb[proc])
# #             logger.info(f"TTFB [{proc}]: n={len(self.ttfb[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         for proc in sorted(self.proc.keys()):
# #             p = self._get_percentiles(self.proc[proc])
# #             logger.info(f"Processing [{proc}]: n={len(self.proc[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         if self.llm_tokens:
# #             total_prompt = sum(t[0] for t in self.llm_tokens)
# #             total_completion = sum(t[1] for t in self.llm_tokens)
# #             logger.info(f"LLM total tokens: prompt={total_prompt}, completion={total_completion}, total={total_prompt+total_completion}")
# #         if self.tts_chars:
# #             logger.info(f"TTS total characters: {sum(self.tts_chars)}")
# #         if self.e2e:
# #             p = self._get_percentiles(self.e2e)
# #             logger.info(f"E2E Latency (user stop → bot start): n={len(self.e2e)}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         logger.info("=" * 60)

# #     def save_to_file(self, session_id: str, output_dir: str = "metrics"):
# #         Path(output_dir).mkdir(exist_ok=True)
# #         file_path = Path(output_dir) / f"metrics_{session_id}.json"

# #         data = {
# #             "session_id": session_id,
# #             "ttfb": {proc: vals for proc, vals in self.ttfb.items()},
# #             "processing": {proc: vals for proc, vals in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": self.e2e,
# #             "summary": {
# #                 "ttfb_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.ttfb.items()},
# #                 "processing_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.proc.items()},
# #                 "e2e_percentiles": self._get_percentiles(self.e2e) if self.e2e else {},
# #                 "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #                 "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #                 "total_tts_characters": sum(self.tts_chars),
# #             }
# #         }
# #         with open(file_path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved to {file_path}")

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()
# #     lead_active = False

# #     # ----- E2E latency tracking -----
# #     e2e_times = []
# #     last_user_stop = None

# #     # Metrics collector (will be passed e2e list)
# #     metrics_collector = MetricsCollector(e2e_list=e2e_times)

# #     # Build pipeline components
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)

# #     # Pipeline: metrics_collector at the END to capture all metrics
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #         enable_tracing=False,
# #         conversation_id=session_id,
# #     )

# #     # ----- E2E event handlers (correct signatures) -----
# #     @transport.event_handler("on_user_stopped_speaking")
# #     async def on_user_stopped(participant_id: str, timestamp_ms: int):
# #         nonlocal last_user_stop
# #         last_user_stop = asyncio.get_event_loop().time()
# #         logger.info(f"👤 User stopped speaking at {last_user_stop} (participant {participant_id})")

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started():
# #         nonlocal last_user_stop, e2e_times
# #         if last_user_stop is not None:
# #             e2e = asyncio.get_event_loop().time() - last_user_stop
# #             e2e_times.append(e2e)
# #             logger.info(f"🎯 E2E latency: {e2e:.3f}s")
# #             last_user_stop = None
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped():
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ----- Other event handlers -----
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         new_ctx = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary()
# #         metrics_collector.save_to_file(session_id)
# # ----working and saved matrix file ---
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline with Full Metrics (including E2E)
# # Metrics: Built-in + FrameProcessor that reads frame.data + custom E2E via transport events
# # Session metrics saved to JSON file.
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData, LLMUsageMetricsData, TTSUsageMetricsData

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}
# #     # Name
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break
# #     # Email
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]
# #     # Phone
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break
# #     # Requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(kw in msg["content"].lower() for kw in ["e-commerce", "platform", "system", "application", "features", "looking to", "you want", "module"]):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break
# #     # Budget
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break
# #     # Contact time
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break
# #     # Defaults
# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_active = False
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # Enhanced Metrics Collector – now with E2E support & file save
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self, e2e_list: list = None):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)      # processor -> list of TTFB values
# #         self.proc = defaultdict(list)      # processor -> list of processing times
# #         self.llm_tokens = []               # list of (prompt, completion)
# #         self.tts_chars = []                # list of character counts
# #         self.e2e = e2e_list if e2e_list is not None else []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)   # handle StartFrame etc.

# #         if isinstance(frame, MetricsFrame):
# #             # In this Pipecat version, metrics data is in frame.data (a list)
# #             metrics_list = []
# #             if hasattr(frame, 'data'):
# #                 data = frame.data
# #                 if isinstance(data, list):
# #                     metrics_list = data
# #                 elif data:
# #                     metrics_list = [data]
# #             elif hasattr(frame, 'metrics_data'):
# #                 # fallback for newer versions
# #                 metrics_list = [frame.metrics_data] if frame.metrics_data else []
# #             else:
# #                 logger.warning(f"MetricsFrame has no data or metrics_data: {dir(frame)}")
# #                 await self.push_frame(frame, direction)
# #                 return

# #             for md in metrics_list:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                     logger.info(f"📈 TTFB [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                     logger.info(f"⏱️ Processing [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     self.llm_tokens.append((md.prompt_tokens, md.completion_tokens))
# #                     logger.info(f"🎯 LLM tokens: prompt={md.prompt_tokens}, completion={md.completion_tokens}")
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     self.tts_chars.append(md.characters)
# #                     logger.info(f"🔊 TTS chars: {md.characters}")
# #                 else:
# #                     logger.debug(f"Unknown metrics data type: {type(md)}")

# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _get_percentiles(values, percentiles=[50, 90, 95, 99]):
# #         if not values:
# #             return {}
# #         sorted_vals = sorted(values)
# #         return {p: sorted_vals[int(len(sorted_vals) * p / 100)] for p in percentiles}

# #     def report_summary(self):
# #         logger.info("=" * 60)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info("=" * 60)
# #         for proc in sorted(self.ttfb.keys()):
# #             p = self._get_percentiles(self.ttfb[proc])
# #             logger.info(f"TTFB [{proc}]: n={len(self.ttfb[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         for proc in sorted(self.proc.keys()):
# #             p = self._get_percentiles(self.proc[proc])
# #             logger.info(f"Processing [{proc}]: n={len(self.proc[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         if self.llm_tokens:
# #             total_prompt = sum(t[0] for t in self.llm_tokens)
# #             total_completion = sum(t[1] for t in self.llm_tokens)
# #             logger.info(f"LLM total tokens: prompt={total_prompt}, completion={total_completion}, total={total_prompt+total_completion}")
# #         if self.tts_chars:
# #             logger.info(f"TTS total characters: {sum(self.tts_chars)}")
# #         if self.e2e:
# #             p = self._get_percentiles(self.e2e)
# #             logger.info(f"E2E Latency (user stop → bot start): n={len(self.e2e)}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         logger.info("=" * 60)

# #     def save_to_file(self, session_id: str, output_dir: str = "metrics"):
# #         Path(output_dir).mkdir(exist_ok=True)
# #         file_path = Path(output_dir) / f"metrics_{session_id}.json"

# #         # Build serialisable dictionary
# #         data = {
# #             "session_id": session_id,
# #             "ttfb": {proc: vals for proc, vals in self.ttfb.items()},
# #             "processing": {proc: vals for proc, vals in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": self.e2e,
# #             "summary": {
# #                 "ttfb_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.ttfb.items()},
# #                 "processing_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.proc.items()},
# #                 "e2e_percentiles": self._get_percentiles(self.e2e) if self.e2e else {},
# #                 "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #                 "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #                 "total_tts_characters": sum(self.tts_chars),
# #             }
# #         }
# #         with open(file_path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved to {file_path}")

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()
# #     lead_active = False

# #     # ----- E2E latency tracking -----
# #     e2e_times = []          # list of deltas (user stop → bot start)
# #     last_user_stop = None   # timestamp of last user_stopped_speaking

# #     # 1. Create metrics collector (will be passed e2e list)
# #     metrics_collector = MetricsCollector(e2e_list=e2e_times)

# #     # 2. Build pipeline components
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         metrics_collector,   # <-- captures all metrics frames
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     # 3. PipelineTask – metrics enabled, tracing disabled
# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #         enable_tracing=False,          # disable OpenTelemetry (not needed)
# #         conversation_id=session_id,
# #     )

# #     # ----- E2E event handlers -----
# #     @transport.event_handler("on_user_stopped_speaking")
# #     async def on_user_stopped(transport, participant, timestamp_ms):
# #         nonlocal last_user_stop
# #         last_user_stop = asyncio.get_event_loop().time()
# #         logger.debug(f"User stopped speaking at {last_user_stop}")

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         nonlocal last_user_stop, e2e_times
# #         if last_user_stop is not None:
# #             e2e = asyncio.get_event_loop().time() - last_user_stop
# #             e2e_times.append(e2e)
# #             logger.info(f"🎯 E2E latency: {e2e:.3f}s")
# #             last_user_stop = None
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ----- Existing event handlers -----
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         new_ctx = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary()
# #         metrics_collector.save_to_file(session_id)   # persist metrics to JSON
# #---working version ----
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline with Working Metrics
# # Metrics: Built-in + FrameProcessor that reads frame.data
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData, LLMUsageMetricsData, TTSUsageMetricsData

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}
# #     # Name
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break
# #     # Email
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]
# #     # Phone
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break
# #     # Requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(kw in msg["content"].lower() for kw in ["e-commerce", "platform", "system", "application", "features", "looking to", "you want", "module"]):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break
# #     # Budget
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break
# #     # Contact time
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break
# #     # Defaults
# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_active = False
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # Fixed Metrics Collector – reads frame.data instead of metrics_data
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)      # processor -> list of TTFB values
# #         self.proc = defaultdict(list)      # processor -> list of processing times
# #         self.llm_tokens = []               # list of (prompt, completion)
# #         self.tts_chars = []                # list of character counts

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)   # handle StartFrame etc.

# #         if isinstance(frame, MetricsFrame):
# #             # In this Pipecat version, metrics data is in frame.data (a list)
# #             metrics_list = []
# #             if hasattr(frame, 'data'):
# #                 data = frame.data
# #                 if isinstance(data, list):
# #                     metrics_list = data
# #                 elif data:
# #                     metrics_list = [data]
# #             elif hasattr(frame, 'metrics_data'):
# #                 # fallback for newer versions
# #                 metrics_list = [frame.metrics_data] if frame.metrics_data else []
# #             else:
# #                 logger.warning(f"MetricsFrame has no data or metrics_data: {dir(frame)}")
# #                 await self.push_frame(frame, direction)
# #                 return

# #             for md in metrics_list:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                     logger.info(f"📈 TTFB [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                     logger.info(f"⏱️ Processing [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     self.llm_tokens.append((md.prompt_tokens, md.completion_tokens))
# #                     logger.info(f"🎯 LLM tokens: prompt={md.prompt_tokens}, completion={md.completion_tokens}")
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     self.tts_chars.append(md.characters)
# #                     logger.info(f"🔊 TTS chars: {md.characters}")
# #                 else:
# #                     logger.debug(f"Unknown metrics data type: {type(md)}")

# #         await self.push_frame(frame, direction)

# #     def get_percentiles(self, values, percentiles=[50, 90, 95, 99]):
# #         if not values:
# #             return {}
# #         sorted_vals = sorted(values)
# #         return {p: sorted_vals[int(len(sorted_vals) * p / 100)] for p in percentiles}

# #     def report_summary(self):
# #         logger.info("=" * 60)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info("=" * 60)
# #         for proc in sorted(self.ttfb.keys()):
# #             p = self.get_percentiles(self.ttfb[proc])
# #             logger.info(f"TTFB [{proc}]: n={len(self.ttfb[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         for proc in sorted(self.proc.keys()):
# #             p = self.get_percentiles(self.proc[proc])
# #             logger.info(f"Processing [{proc}]: n={len(self.proc[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         if self.llm_tokens:
# #             total_prompt = sum(t[0] for t in self.llm_tokens)
# #             total_completion = sum(t[1] for t in self.llm_tokens)
# #             logger.info(f"LLM total tokens: prompt={total_prompt}, completion={total_completion}, total={total_prompt+total_completion}")
# #         if self.tts_chars:
# #             logger.info(f"TTS total characters: {sum(self.tts_chars)}")
# #         logger.info("=" * 60)

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()
# #     lead_active = False

# #     # 1. Create metrics collector processor
# #     metrics_collector = MetricsCollector()

# #     # 2. Build pipeline
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         metrics_collector,   # <-- captures all metrics frames
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     # 3. PipelineTask – metrics enabled, no observers/tracing
# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #         enable_tracing=False,          # disable OpenTelemetry (not needed)
# #         conversation_id=session_id,
# #     )

# #     # 4. Event handlers
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         new_ctx = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary()
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # # Every phrase the LLM uses to close a lead
# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     """Extract lead fields by scanning the full conversation."""
# #     full = " ".join(
# #         m["content"] for m in messages
# #         if m["role"] in ("user", "assistant")
# #     )
# #     lead: dict = {}

# #     # ── Name ──────────────────────────────────────────────────
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break

# #     # Specific fallback: user said "My name is X"
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(
# #                     r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #                     msg["content"], re.IGNORECASE
# #                 )
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     # ── Email ─────────────────────────────────────────────────
# #     # Collect from user messages only (more reliable than bot summaries)
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     # Fallback: scan everything
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     # ── Phone ─────────────────────────────────────────────────
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     # ── Requirements ─────────────────────────────────────────
# #     # Look for the bot's summary of project requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             # Take the first 200 chars of that assistant message
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     # ── Budget ────────────────────────────────────────────────
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     # Fallback: bot confirmed budget
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     # ── Contact Time ─────────────────────────────────────────
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     # ── Defaults ─────────────────────────────────────────────
# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – sits between LLM and TTS in pipeline
# # # Accumulates text per LLM response (resets on LLMFullResponseEndFrame)
# # # Fires email when completion phrase is detected
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):

# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages    = messages   # live reference to context message list
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""         # accumulates text for current LLM response
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if direction == FrameDirection.DOWNSTREAM:

# #             if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
# #                 self._buffer += frame.text or ""
# #                 # logger.debug(f"Buffer now: {self._buffer!r}")
# #                 # logger.debug(f"_lead_active={self._lead_active}, _email_sent={self._email_sent}")
# #                 # logger.debug(f"_is_completion={_is_completion(self._buffer)}")

# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))

# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 # LLM finished this response – log and reset buffer for next turn
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""

# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Lead Interceptor ─────────────────────────────────────
# #     interceptor = LeadInterceptor(
# #         messages=messages,
# #         ws_callback=ws_callback,
# #         session_id=session_id,
# #     )

# #     # ── Pipeline ─────────────────────────────────────────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,        # ← between LLM and TTS, reads every text frame
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Activate lead collection once triggered
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "will get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "get in touch within 24 hours",
# #     "reach out within 24 hours",
# #     "team will reach out",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     """Extract lead data by scanning the full conversation history."""
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     # Name – look for bot confirming the name
# #     for pat in [
# #         r"(?:your name is|hi|hello|thanks?,?|great,?|noted,?|so,?)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #         r"(?:lovely|nice) to (?:meet|have) you[,\s]+([A-Z][a-zA-Z]+)",
# #         r"my name is ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             # Filter out common words that aren't names
# #             if candidate.lower() not in ("you", "there", "welcome", "sure", "great", "alex"):
# #                 lead["name"] = candidate
# #                 break

# #     # Also try user messages directly
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     # Email – most reliable: just find @
# #     emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #     if emails:
# #         lead["email"] = emails[-1]

# #     # Phone – find in user messages first (more reliable)
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\+?[\d][\d\s\-().]{6,18}\d)", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     # Requirements – look for project description phrases
# #     for msg in messages:
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["looking to build", "project is", "you want to", "you're interested in", "features you're looking"]
# #         ):
# #             # Take the assistant's summary of requirements
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     # Budget – find dollar amounts or "k" values in conversation
# #     budgets = re.findall(
# #         r"(?:budget|cost|around|about)?\s*(?:\$[\d,]+(?:k|K)?|[\d]+(?:,[\d]+)?\s*(?:k|K|thousand|USD|dollars?))",
# #         full, re.IGNORECASE
# #     )
# #     if budgets:
# #         lead["budget"] = budgets[-1].strip()

# #     # Contact time
# #     for pat in [
# #         r"\b(morning|afternoon|evening|night|weekday|weekend|anytime|asap|today|tomorrow)\b",
# #         r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             lead["contact_time"] = m.group(1).strip()
# #             break

# #     # Defaults
# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LLM Text Interceptor
# # # Sits BETWEEN LLM and TTS, reads every text frame as it streams
# # # Detects completion phrase and fires email IMMEDIATELY
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     """
# #     Intercepts text frames from the LLM before they reach TTS.
# #     Accumulates text per response, detects completion phrase,
# #     fires the lead email with real data extracted from conversation.
# #     """

# #     def __init__(self, context: OpenAILLMContext, messages: list,
# #                  ws_callback, session_id: str):
# #         super().__init__()
# #         self._context     = context
# #         self._messages    = messages     # same list object as context messages
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""           # accumulates current LLM response
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
# #             text = frame.text or ""
# #             self._buffer += text

# #             # Check for completion phrase in the streaming buffer
# #             if (
# #                 self._lead_active
# #                 and not self._email_sent
# #                 and _is_completion(self._buffer)
# #             ):
# #                 self._email_sent = True
# #                 logger.info("✅ Completion phrase detected in stream – sending lead email now")

# #                 # Use current messages list for extraction
# #                 lead_data = _extract_from_conversation(self._messages)
# #                 logger.info(f"Extracted lead: {lead_data}")

# #                 # Fire email immediately (don't await, don't block TTS)
# #                 asyncio.ensure_future(self._fire_email(lead_data))

# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# #     def reset_buffer(self):
# #         """Call at the start of each new LLM response."""
# #         self._buffer = ""


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Lead interceptor (sits between LLM and TTS) ──────────
# #     interceptor = LeadInterceptor(
# #         context=context,
# #         messages=messages,
# #         ws_callback=ws_callback,
# #         session_id=session_id,
# #     )

# #     # ── Pipeline  (interceptor between LLM and TTS) ──────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,       # ← reads every text frame from LLM
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Activate lead collection
# #             logger.info("Lead collection started")
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()
# #             logger.info("Lead collection started")

# #         # Reset interceptor buffer for new response
# #         interceptor.reset_buffer()

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "will get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "get in touch within 24 hours",
# #     "reach out within 24 hours",
# #     "team will reach out",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     """Extract lead data by scanning the full conversation history."""
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     # Name – look for bot confirming the name
# #     for pat in [
# #         r"(?:your name is|hi|hello|thanks?,?|great,?|noted,?|so,?)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #         r"(?:lovely|nice) to (?:meet|have) you[,\s]+([A-Z][a-zA-Z]+)",
# #         r"my name is ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             # Filter out common words that aren't names
# #             if candidate.lower() not in ("you", "there", "welcome", "sure", "great", "alex"):
# #                 lead["name"] = candidate
# #                 break

# #     # Also try user messages directly
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     # Email – most reliable: just find @
# #     emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #     if emails:
# #         lead["email"] = emails[-1]

# #     # Phone – find in user messages first (more reliable)
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\+?[\d][\d\s\-().]{6,18}\d)", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     # Requirements – look for project description phrases
# #     for msg in messages:
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["looking to build", "project is", "you want to", "you're interested in", "features you're looking"]
# #         ):
# #             # Take the assistant's summary of requirements
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     # Budget – find dollar amounts or "k" values in conversation
# #     budgets = re.findall(
# #         r"(?:budget|cost|around|about)?\s*(?:\$[\d,]+(?:k|K)?|[\d]+(?:,[\d]+)?\s*(?:k|K|thousand|USD|dollars?))",
# #         full, re.IGNORECASE
# #     )
# #     if budgets:
# #         lead["budget"] = budgets[-1].strip()

# #     # Contact time
# #     for pat in [
# #         r"\b(morning|afternoon|evening|night|weekday|weekend|anytime|asap|today|tomorrow)\b",
# #         r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             lead["contact_time"] = m.group(1).strip()
# #             break

# #     # Defaults
# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LLM Text Interceptor
# # # Sits BETWEEN LLM and TTS, reads every text frame as it streams
# # # Detects completion phrase and fires email IMMEDIATELY
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     """
# #     Intercepts text frames from the LLM before they reach TTS.
# #     Accumulates text per response, detects completion phrase,
# #     fires the lead email with real data extracted from conversation.
# #     """

# #     def __init__(self, context: OpenAILLMContext, messages: list,
# #                  ws_callback, session_id: str):
# #         super().__init__()
# #         self._context     = context
# #         self._messages    = messages     # same list object as context messages
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""           # accumulates current LLM response
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
# #             text = frame.text or ""
# #             self._buffer += text

# #             # Check for completion phrase in the streaming buffer
# #             if (
# #                 self._lead_active
# #                 and not self._email_sent
# #                 and _is_completion(self._buffer)
# #             ):
# #                 self._email_sent = True
# #                 logger.info("✅ Completion phrase detected in stream – sending lead email now")

# #                 # Use current messages list for extraction
# #                 lead_data = _extract_from_conversation(self._messages)
# #                 logger.info(f"Extracted lead: {lead_data}")

# #                 # Fire email immediately (don't await, don't block TTS)
# #                 asyncio.ensure_future(self._fire_email(lead_data))

# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# #     def reset_buffer(self):
# #         """Call at the start of each new LLM response."""
# #         self._buffer = ""


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Lead interceptor (sits between LLM and TTS) ──────────
# #     interceptor = LeadInterceptor(
# #         context=context,
# #         messages=messages,
# #         ws_callback=ws_callback,
# #         session_id=session_id,
# #     )

# #     # ── Pipeline  (interceptor between LLM and TTS) ──────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,       # ← reads every text frame from LLM
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Activate lead collection
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()
# #             logger.info("Lead collection started")

# #         # Reset interceptor buffer for new response
# #         interceptor.reset_buffer()

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import TextFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY:
# #      "Our team will reach out to you within 24 hours."
# #   4. Then on the very next line write this hidden tag (not spoken aloud):
# #      [LEAD_COMPLETE: name=<name> | email=<email> | phone=<phone> | requirements=<requirements> | budget=<budget> | contact_time=<contact_time>]

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead detection helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TAG = re.compile(
# #     r"\[LEAD_COMPLETE:\s*"
# #     r"name=(?P<name>[^|]+)\|?\s*"
# #     r"email=(?P<email>[^|]+)\|?\s*"
# #     r"phone=(?P<phone>[^|]+)\|?\s*"
# #     r"requirements=(?P<requirements>[^|]+)\|?\s*"
# #     r"budget=(?P<budget>[^|]+)\|?\s*"
# #     r"contact_time=(?P<contact_time>[^\]]+)"
# #     r"\]",
# #     re.IGNORECASE | re.DOTALL,
# # )

# # _COMPLETION_SIGNAL = re.compile(
# #     r"(reach out|get back|contact you).{0,30}24 hours",
# #     re.IGNORECASE,
# # )

# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     t = text.lower()
# #     return any(kw in t for kw in _LEAD_TRIGGERS)


# # def _parse_lead_tag(text: str) -> dict | None:
# #     """Extract structured lead data from the [LEAD_COMPLETE:...] tag."""
# #     m = _LEAD_TAG.search(text)
# #     if not m:
# #         return None
# #     return {k: v.strip() for k, v in m.groupdict().items() if v}


# # def _scrape_lead_from_history(messages: list) -> dict:
# #     """
# #     Fallback: scan full conversation history and extract
# #     name / email / phone / requirements using regex.
# #     """
# #     full_text = " ".join(
# #         m["content"] for m in messages if m["role"] in ("user", "assistant")
# #     )
# #     lead: dict = {}

# #     # Name
# #     for pat in [
# #         r"(?:your name is|name:?)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #         r"(?:lovely|great|nice) to (?:have you|meet you)[,\s]+([A-Z][a-zA-Z]+)",
# #         r"(?:got it|noted)[,\s]+([A-Z][a-zA-Z]+)",
# #     ]:
# #         m = re.search(pat, full_text)
# #         if m:
# #             lead["name"] = m.group(1).strip()
# #             break

# #     # Email
# #     m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", full_text)
# #     if m:
# #         lead["email"] = m.group(0)

# #     # Phone – 7+ digit sequence
# #     m = re.search(r"\b[\d\s\-+()]{7,20}\b", full_text)
# #     if m:
# #         digits = re.sub(r"\D", "", m.group(0))
# #         if len(digits) >= 7:
# #             lead["phone"] = digits

# #     # Requirements
# #     for pat in [
# #         r"(?:looking to build|want to build|build a?)\s+(.{10,120}?)(?:\.|,|\n|$)",
# #         r"(?:multi.?gateway|payment|saas|mobile app|web app|e.?commerce).{0,80}",
# #     ]:
# #         m = re.search(pat, full_text, re.IGNORECASE)
# #         if m:
# #             lead.setdefault("requirements", m.group(0).strip()[:200])
# #             break

# #     # Defaults for anything not found
# #     lead.setdefault("name",         "Unknown")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "As soon as possible")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     # shared mutable state (nonlocal inside handlers)
# #     lead_active = False
# #     email_sent  = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Pipeline ─────────────────────────────────────────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── Lead completion checker ───────────────────────────────
# #     async def _check_for_lead_completion():
# #         nonlocal email_sent
# #         if email_sent:
# #             return

# #         msgs = context.get_messages()
# #         # Scan ALL assistant messages for the tag, not just the last one
# #         all_assistant_text = " ".join(
# #             m["content"] for m in msgs if m["role"] == "assistant"
# #         )

# #         # Method 1 – structured tag from LLM
# #         lead_data = _parse_lead_tag(all_assistant_text)
# #         if lead_data:
# #             logger.info(f"Lead tag found: {lead_data}")
# #             email_sent = True
# #             sent = await send_lead_email(lead_data)
# #             if ws_callback:
# #                 await ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": session_id
# #                 })
# #             return

# #         # Method 2 – completion phrase detected, scrape history as fallback
# #         if _COMPLETION_SIGNAL.search(all_assistant_text) and lead_active:
# #             logger.info("Completion signal detected – scraping lead from history")
# #             lead_data = _scrape_lead_from_history(msgs)
# #             logger.info(f"Scraped lead: {lead_data}")
# #             email_sent = True
# #             sent = await send_lead_email(lead_data)
# #             if ws_callback:
# #                 await ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": session_id
# #                 })
# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")

# #         transport.capture_participant_audio(pid)

# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})

# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context in system prompt
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Mark lead as active once user shows intent
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             logger.info("Lead collection started")

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})
# #         # Wait for context aggregator to save assistant message, then check
# #         if lead_active and not email_sent:
# #             await asyncio.sleep(1.5)
# #             await _check_for_lead_completion()

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task): {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id": session_id,
# #             "recorded_at": datetime.now().isoformat(),
# #             "ttfb": {k: v for k, v in self.ttfb.items()},
# #             "processing": {k: v for k, v in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary": self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point with aggressive trimming and summarization
# # # ─────────────────────────────────────────────────────────────
# # MAX_CONVERSATION_TURNS = 6   # system + last 5 messages (~2 exchanges)
# # SUMMARY_TRIGGER_LEN = 10     # when conversation exceeds 10 messages, generate summary

# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list = []

# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=150)

# #     # Initial context
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=2)  # reduced RAG chunks
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ─── Summarization helper ────────────────────────────────
# #     async def generate_summary(long_messages: List[Dict]) -> str:
# #         """Generate a concise summary of older conversation turns."""
# #         turns = [m for m in long_messages if m["role"] != "system"][-8:]  # last 8 non-system turns
# #         if not turns:
# #             return "The user is interested in WartinLabs services."
# #         prompt = f"Summarize this conversation in 2 short sentences focusing on user's needs and collected info:\n{json.dumps(turns)}"
# #         try:
# #             # Use the Groq service to generate summary
# #             summary_response = await llm.generate(prompt, max_tokens=80)
# #             return summary_response.strip()
# #         except Exception as e:
# #             logger.warning(f"Summary generation failed: {e}")
# #             return "The user is interested in WartinLabs services."

# #     # ─── Context trimming + summarization ────────────────────
# #     async def trim_and_summarize():
# #         nonlocal messages, context, ctx_agg
# #         if len(messages) <= MAX_CONVERSATION_TURNS:
# #             return

# #         # If conversation is very long, generate summary for older parts
# #         if len(messages) > SUMMARY_TRIGGER_LEN:
# #             # Extract messages before the last MAX_CONVERSATION_TURNS messages
# #             old_msgs = messages[1:-(MAX_CONVERSATION_TURNS - 1)]
# #             if old_msgs:
# #                 summary = await generate_summary(old_msgs)
# #                 # Replace old messages with a single assistant message containing the summary
# #                 summary_msg = {"role": "assistant", "content": f"[Previous conversation summary: {summary}]"}
# #                 # New list: system + summary + last (MAX_CONVERSATION_TURNS - 2) messages
# #                 new_msgs = [messages[0], summary_msg] + messages[-(MAX_CONVERSATION_TURNS - 2):]
# #                 messages[:] = new_msgs
# #                 logger.info(f"📝 Summarized {len(old_msgs)} messages into summary, now total {len(messages)} messages")
# #             else:
# #                 # Simple trim without summary
# #                 new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #                 messages[:] = new_msgs
# #         else:
# #             # Simple trim
# #             new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #             messages[:] = new_msgs

# #         # Replace the entire context and aggregator
# #         # This forces the LLM to use the trimmed history
# #         new_context = OpenAILLMContext(messages)
# #         # Replace the aggregator's internal context
# #         ctx_agg._context = new_context
# #         context.messages = messages
# #         logger.info(f"✂️ Trimmed context to {len(messages)} messages")

# #     # ─── Event handlers ──────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # 1. Goodbye detection
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return

# #         # 2. FAQ cache
# #         cached = get_cached_answer(user_text)
# #         if cached:
# #             logger.info("⚡ Using cached answer")
# #             # Append to conversation history
# #             messages.append({"role": "assistant", "content": cached})
# #             # Update context
# #             context.messages.append({"role": "assistant", "content": cached})
# #             await task.queue_frames([TextFrame(cached)])
# #             return

# #         # 3. Structured lead collection
# #         lead_state = interceptor.get_state()
# #         if lead_state.is_active() or _wants_lead(user_text):
# #             bot_reply, still_active = lead_state.process_answer(user_text, lead_state.is_active())
# #             if bot_reply:
# #                 # Append to conversation history
# #                 messages.append({"role": "assistant", "content": bot_reply})
# #                 context.messages.append({"role": "assistant", "content": bot_reply})
# #                 await task.queue_frames([TextFrame(bot_reply)])
# #                 if not still_active:
# #                     lead_active = True
# #                     interceptor.mark_lead_active()
# #                     # Reset state after completion (email will be sent by interceptor)
# #                     interceptor.reset_state()
# #                 return

# #         # 4. Selective RAG – skip for short/filler messages
# #         filler_words = {"yes", "no", "okay", "ok", "sure", "thanks", "thank you", "hello", "hi", "hey", "yeah", "yep", "got it", "uh huh", "hmm"}
# #         if len(user_text) < 15 or user_text.lower() in filler_words:
# #             rag_context = "General WartinLabs knowledge. The user is asking a short question or acknowledging."
# #             logger.debug("Skipping RAG (short/filler message)")
# #         else:
# #             new_ctx = retrieve(user_text, top_k=2)
# #             rag_context = new_ctx or "General WartinLabs knowledge."

# #         # Update system prompt
# #         new_system = SYSTEM_TEMPLATE.format(context=rag_context)
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Trim and summarize after each user turn
# #         await trim_and_summarize()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Optimized Pipecat Pipeline
# # Improvements:
# # - Selective RAG (skip short/filler messages)
# # - FAQ cache (static answers)
# # - Structured lead collection (state machine)
# # - Conversation summarization (sliding window + summary)
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict, deque
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable, Dict, Optional

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt – unchanged
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, a warm, friendly, and professional AI voice assistant for WartinLabs.

# # PERSONALITY & TONE:
# # - Be conversational, empathetic, and polite. Use phrases like "Sure thing!", "I'd be happy to help", "Let me check that for you".
# # - Responses MUST be 2-4 short sentences max (voice brevity matters). Avoid bullet points, lists, markdown.
# # - Speak numbers naturally: "ten thousand dollars" not "$10,000". Email: "info at wartinlabs dot com".

# # COMPANY DETAILS:
# # - WartinLabs office: 2217, 2nd Floor, Corenthum Tower, Noida-62, Uttar Pradesh, India. (Near Noida Electronic City Metro station)
# # - Contact: info@wartinlabs.com, phone +91 6387541924.

# # SERVICES:
# # - We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.
# # - We do NOT offer flight booking, travel reservations, or any travel agency services. If asked, politely state: "We don't provide flight booking services. Our expertise is in AI and software development."

# # HANDLING UNCLEAR OR NOISY INPUT:
# # - If you are unsure what the user said, politely ask: "I'm sorry, I didn't catch that. Could you please repeat?"
# # - Do not guess or respond with unrelated answers.

# # SENSITIVE OR HARMFUL REQUESTS:
# # - If the user asks to harm someone, commit violence, or anything illegal, immediately say: "I can't help with that request. Let's change the subject."
# # - Do not offer any advice or partial suggestions.

# # FACTUAL QUESTIONS:
# # - Answer common factual questions directly. For example: "Who is the Prime Minister of India?" → "Narendra Modi is the Prime Minister of India."
# # - Do not refuse to answer non-political factual questions.

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote, book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team.
# #   2. Collect details ONE AT A TIME in this exact order: full name, email, phone, project description, budget, preferred contact time.
# #   3. After collecting ALL six, confirm and say exactly: "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure: "Let me connect you with our team for the exact answer."
# # - NEVER fabricate prices, timelines, or client names.
# # - Keep every response SHORT for voice.
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence) and do NOT continue.
# # - If the user asks to change an email address, clearly say: "Sure, please tell me your corrected email address, and I'll update it."

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 1: FAQ Cache (hardcoded answers)
# # # ─────────────────────────────────────────────────────────────
# # FAQ_CACHE = {
# #     "office": "Our office is at 2217, 2nd Floor, Corenthum Tower, Noida-62, near the Electronic City Metro station.",
# #     "location": "We're located in Noida, Uttar Pradesh, India, at the address I just gave you.",
# #     "services": "We specialize in AI solutions, custom software development, automation, digital transformation, voice agents, and SaaS platforms.",
# #     "email": "You can email us at info at wartinlabs dot com.",
# #     "phone": "Our phone number is plus 91 63875 41924.",
# #     "pricing": "Pricing depends on the project scope. I'd be happy to connect you with our team for a custom quote.",
# #     "modi": "Narendra Modi is the Prime Minister of India.",
# #     "prime minister": "Narendra Modi is the Prime Minister of India.",
# # }

# # def get_cached_answer(text: str) -> Optional[str]:
# #     """Return cached answer if user asks a frequent question."""
# #     lower = text.lower()
# #     for key, answer in FAQ_CACHE.items():
# #         if key in lower:
# #             logger.info(f"✅ FAQ cache hit: '{key}'")
# #             return answer
# #     return None

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers (modified for structured collection)
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     # Same as before – unchanged
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # OPTIMIZATION 3: Structured lead collection (state machine)
# # # ─────────────────────────────────────────────────────────────
# # class LeadState:
# #     def __init__(self):
# #         self.reset()
# #     def reset(self):
# #         self.name = None
# #         self.email = None
# #         self.phone = None
# #         self.project_desc = None
# #         self.budget = None
# #         self.contact_time = None
# #         self.step = 0  # 0=not active, 1=name, 2=email, 3=phone, 4=project, 5=budget, 6=time, 7=complete
# #     def is_active(self):
# #         return self.step > 0 and self.step < 7
# #     def next_question(self) -> str:
# #         if self.step == 1:
# #             return "Could you please tell me your full name?"
# #         elif self.step == 2:
# #             return "And your email address?"
# #         elif self.step == 3:
# #             return "What's your phone number?"
# #         elif self.step == 4:
# #             return "Can you briefly describe your project?"
# #         elif self.step == 5:
# #             return "What's your budget range for this project?"
# #         elif self.step == 6:
# #             return "What time of day is best to contact you?"
# #         else:
# #             return ""
# #     def process_answer(self, text: str, lead_active_flag: bool) -> tuple[str, bool]:
# #         """Returns (bot_response, should_continue_lead)"""
# #         if not lead_active_flag:
# #             if _wants_lead(text):
# #                 self.step = 1
# #                 return (self.next_question(), True)
# #             return (None, False)
# #         # Lead active – collect data
# #         if self.step == 1:
# #             self.name = text.strip()
# #             self.step = 2
# #             return (self.next_question(), True)
# #         elif self.step == 2:
# #             if '@' in text and '.' in text:
# #                 self.email = text.strip()
# #                 self.step = 3
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I didn't catch a valid email address. Could you please repeat your email?", True)
# #         elif self.step == 3:
# #             # simple phone number validation
# #             if re.search(r"[\d\s\-+\(\)]{6,}", text):
# #                 self.phone = text.strip()
# #                 self.step = 4
# #                 return (self.next_question(), True)
# #             else:
# #                 return ("I need your phone number to connect you with our team. Please tell me your number.", True)
# #         elif self.step == 4:
# #             self.project_desc = text.strip()
# #             self.step = 5
# #             return (self.next_question(), True)
# #         elif self.step == 5:
# #             self.budget = text.strip()
# #             self.step = 6
# #             return (self.next_question(), True)
# #         elif self.step == 6:
# #             self.contact_time = text.strip()
# #             self.step = 7
# #             # Mark complete; email will be sent by the interceptor
# #             return ("To confirm, I have all your details. Our team will reach out to you within 24 hours.", False)
# #         else:
# #             return (None, False)

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – updated to work with structured state
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_state = LeadState()
# #         self._email_sent = False

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     # Also fill from structured state if missing
# #                     if self._lead_state.name:
# #                         lead_data["name"] = self._lead_state.name
# #                     if self._lead_state.email:
# #                         lead_data["email"] = self._lead_state.email
# #                     if self._lead_state.phone:
# #                         lead_data["phone"] = self._lead_state.phone
# #                     if self._lead_state.project_desc:
# #                         lead_data["requirements"] = self._lead_state.project_desc
# #                     if self._lead_state.budget:
# #                         lead_data["budget"] = self._lead_state.budget
# #                     if self._lead_state.contact_time:
# #                         lead_data["contact_time"] = self._lead_state.contact_time
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     def get_state(self):
# #         return self._lead_state

# #     def reset_state(self):
# #         self._lead_state.reset()

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session", "terminate this call",
# #     "end this call", "end the conversation", "that's all", "that is all",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "no more questions", "i'm done", "i am done", "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._triggered = False
# #         self._task = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 1s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(1.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list = e2e_list
# #         self._user_stop = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped at {self._user_stop:.3f}")
# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s (total: {len(self._e2e_list)})")
# #                 self._user_stop = None
# #         await self.push_frame(frame, direction)

# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)
# #         self.proc = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]
# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt = getattr(val, "prompt_tokens", getattr(md, "prompt_tokens", 0))
# #                     ct = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)
# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles": {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles": self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters": sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars : {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id": session_id,
# #             "recorded_at": datetime.now().isoformat(),
# #             "ttfb": {k: v for k, v in self.ttfb.items()},
# #             "processing": {k: v for k, v in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary": self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point with all optimizations
# # # ─────────────────────────────────────────────────────────────
# # MAX_CONVERSATION_TURNS = 8  # keep system + last 7 messages (~3 exchanges)
# # SUMMARY_TRIGGER_LEN = 8     # when conversation exceeds this, generate summary

# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list = []

# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=200)

# #     # Initial context
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ─── OPTIMIZATION 4: Conversation summarization ──────────
# #     async def generate_summary(long_messages: list) -> str:
# #         """Use Groq LLM to summarize conversation history."""
# #         # Take messages excluding system, keep only last 10 turns
# #         turns = [m for m in long_messages if m["role"] != "system"][-10:]
# #         prompt = f"Summarize this conversation in 2-3 short sentences, focusing on user's intent and collected info:\n{json.dumps(turns)}"
# #         try:
# #             summary_response = await llm.generate(prompt, max_tokens=100)
# #             return summary_response.strip()
# #         except Exception as e:
# #             logger.warning(f"Summary generation failed: {e}")
# #             return "The user is interested in WartinLabs services."

# #     # ─── Context trimming + summarization ────────────────────
# #     def trim_and_summarize():
# #         nonlocal messages, context
# #         if len(messages) <= MAX_CONVERSATION_TURNS:
# #             return
# #         # If conversation is very long, generate a summary
# #         if len(messages) > SUMMARY_TRIGGER_LEN:
# #             # Create a summary asynchronously (fire-and-forget)
# #             asyncio.create_task(do_summarization())
# #         # Keep system + last (MAX_CONVERSATION_TURNS - 1) messages
# #         new_msgs = [messages[0]] + messages[-(MAX_CONVERSATION_TURNS - 1):]
# #         messages[:] = new_msgs
# #         context.messages.clear()
# #         context.messages.extend(new_msgs)
# #         logger.info(f"✂️ Trimmed context to {len(messages)} messages")

# #     async def do_summarization():
# #         # Generate summary of older messages (before the trimmed part)
# #         # For simplicity, we'll just replace system message with a summary note
# #         # But this is optional; we'll just log
# #         logger.info("📝 Long conversation detected – summary would be generated here (optional)")

# #     # ─── Event handlers ──────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # 1. Goodbye detection
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return

# #         # 2. OPTIMIZATION 2: FAQ cache
# #         cached = get_cached_answer(user_text)
# #         if cached:
# #             # Send cached answer directly without LLM call
# #             logger.info("⚡ Using cached answer")
# #             await task.queue_frames([TextFrame(cached)])
# #             # Also add to conversation history
# #             messages.append({"role": "assistant", "content": cached})
# #             context.messages.append({"role": "assistant", "content": cached})
# #             return

# #         # 3. OPTIMIZATION 3: Structured lead collection
# #         lead_state = interceptor.get_state()
# #         if lead_state.is_active() or _wants_lead(user_text):
# #             bot_reply, still_active = lead_state.process_answer(user_text, lead_state.is_active())
# #             if bot_reply:
# #                 await task.queue_frames([TextFrame(bot_reply)])
# #                 messages.append({"role": "assistant", "content": bot_reply})
# #                 context.messages.append({"role": "assistant", "content": bot_reply})
# #                 if not still_active:
# #                     # Lead collection complete – mark lead_active for email
# #                     lead_active = True
# #                     interceptor.mark_lead_active()
# #                 return

# #         # 4. OPTIMIZATION 1: Selective RAG – skip for short/filler messages
# #         filler_words = {"yes", "no", "okay", "ok", "sure", "thanks", "thank you", "hello", "hi", "hey"}
# #         if len(user_text) < 15 or user_text.lower() in filler_words:
# #             rag_context = "General WartinLabs knowledge."  # no retrieval
# #             logger.debug("Skipping RAG (short/filler message)")
# #         else:
# #             new_ctx = retrieve(user_text, top_k=2)  # reduced from 4 to 2 for speed
# #             rag_context = new_ctx or "General WartinLabs knowledge."

# #         # Update system prompt with RAG context
# #         new_system = SYSTEM_TEMPLATE.format(context=rag_context)
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Lead detection (only if not already in structured lead)
# #         if not lead_active and not lead_state.is_active() and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()
# #             # The structured lead will take over from next turn

# #         # Trim context
# #         trim_and_summarize()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)
# # STT       : Deepgram Nova-2
# # TTS       : Deepgram Aura
# # LLM       : Groq llama-3.1-8b-instant
# # VAD       : Silero (local)
# # RAG       : FAISS + MiniLM (local)
# # Metrics   : E2E + TTFB + Processing + Tokens + TTS chars → JSON file
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from datetime import datetime
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import (
# #     Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame,
# #     UserStoppedSpeakingFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
# #     EndFrame,
# # )
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import (
# #     TTFBMetricsData, ProcessingMetricsData,
# #     LLMUsageMetricsData, TTSUsageMetricsData,
# # )

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt – UPDATED with goodbye rule
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # - If the user says goodbye, end the call, or disconnect: say a short farewell ONLY (1 sentence max), do NOT continue the conversation
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     return any(phrase in text.lower() for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             if candidate.lower() not in {"you", "there", "sure", "great", "welcome", "aria"}:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – between LLM and TTS
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages    = messages
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor buffer end: {repr(self._buffer[:120])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # SessionTerminator – ends session when user says goodbye
# # # ─────────────────────────────────────────────────────────────
# # _BYE_PHRASES = [
# #     "goodbye", "good bye", "bye bye", "bye for now",
# #     "end session", "end the session", "terminate", "disconnect",
# #     "end call", "end the call", "just end", "hang up",
# #     "stop the call", "close the session",
# #     "have a nice day", "have a good day", "talk later", "talk to you later",
# #     "that's all", "that is all", "no more questions", "i'm done", "i am done",
# #     "exit", "quit",
# # ]

# # class SessionTerminator(FrameProcessor):
# #     def __init__(self, ws_callback, session_id: str):
# #         super().__init__()
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._triggered   = False
# #         self._task        = None

# #     def set_task(self, task):
# #         self._task = task

# #     def is_goodbye(self, text: str) -> bool:
# #         t = text.lower().strip()
# #         return any(phrase in t for phrase in _BYE_PHRASES)

# #     async def trigger_end(self):
# #         if self._triggered or self._task is None:
# #             return
# #         self._triggered = True
# #         logger.info("👋 Goodbye detected – ending session in 4s")
# #         if self._ws_callback:
# #             await self._ws_callback("session_ended", {"session_id": self._session_id})
# #         await asyncio.sleep(4.0)
# #         logger.info("👋 Sending EndFrame to shut down pipeline")
# #         await self._task.queue_frame(EndFrame())

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         await self.push_frame(frame, direction)


# # # ─────────────────────────────────────────────────────────────
# # # E2E Tracker – sits at START of pipeline, catches VAD frames
# # # ─────────────────────────────────────────────────────────────
# # class E2ETracker(FrameProcessor):
# #     def __init__(self, e2e_list: list):
# #         super().__init__()
# #         self._e2e_list   = e2e_list
# #         self._user_stop  = None

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, UserStoppedSpeakingFrame):
# #             self._user_stop = asyncio.get_event_loop().time()
# #             logger.debug(f"E2ETracker: user stopped speaking at {self._user_stop:.3f}")

# #         elif isinstance(frame, BotStartedSpeakingFrame):
# #             if self._user_stop is not None:
# #                 e2e = asyncio.get_event_loop().time() - self._user_stop
# #                 self._e2e_list.append(round(e2e, 4))
# #                 logger.info(f"🎯 E2E latency: {e2e:.3f}s  (total recorded: {len(self._e2e_list)})")
# #                 self._user_stop = None

# #         await self.push_frame(frame, direction)


# # # ─────────────────────────────────────────────────────────────
# # # MetricsCollector – reads MetricsFrame from pipeline
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb       = defaultdict(list)
# #         self.proc       = defaultdict(list)
# #         self.llm_tokens = []
# #         self.tts_chars  = []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, MetricsFrame):
# #             raw = getattr(frame, "data", None) or getattr(frame, "metrics_data", None)
# #             if raw is None:
# #                 await self.push_frame(frame, direction)
# #                 return
# #             items = raw if isinstance(raw, list) else [raw]

# #             for md in items:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     val = getattr(md, "value", md)
# #                     pt  = getattr(val, "prompt_tokens",     getattr(md, "prompt_tokens",     0))
# #                     ct  = getattr(val, "completion_tokens", getattr(md, "completion_tokens", 0))
# #                     self.llm_tokens.append((pt, ct))
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     chars = getattr(md, "value", getattr(md, "characters", 0))
# #                     self.tts_chars.append(chars)

# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _pct(values, pcts=(50, 90, 95, 99)):
# #         if not values:
# #             return {}
# #         sv = sorted(values)
# #         return {p: round(sv[int(len(sv) * p / 100)], 4) for p in pcts}

# #     def build_summary(self, e2e_list: list) -> dict:
# #         return {
# #             "ttfb_percentiles":       {k: self._pct(v) for k, v in self.ttfb.items()},
# #             "processing_percentiles": {k: self._pct(v) for k, v in self.proc.items()},
# #             "e2e_percentiles":        self._pct(e2e_list) if e2e_list else {},
# #             "total_llm_prompt_tokens":      sum(t[0] for t in self.llm_tokens),
# #             "total_llm_completion_tokens":  sum(t[1] for t in self.llm_tokens),
# #             "total_tts_characters":         sum(self.tts_chars),
# #         }

# #     def report_summary(self, e2e_list: list):
# #         s = self.build_summary(e2e_list)
# #         sep = "=" * 62
# #         logger.info(sep)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info(sep)
# #         for proc, p in s["ttfb_percentiles"].items():
# #             logger.info(f"TTFB [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         for proc, p in s["processing_percentiles"].items():
# #             logger.info(f"Proc [{proc}]: p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         if e2e_list:
# #             p = self._pct(e2e_list)
# #             logger.info(f"E2E  [user→bot]: n={len(e2e_list)}  p50={p.get(50,0):.3f}s  p90={p.get(90,0):.3f}s  p95={p.get(95,0):.3f}s  p99={p.get(99,0):.3f}s")
# #         else:
# #             logger.warning("E2E: no measurements recorded")
# #         logger.info(f"LLM tokens: prompt={s['total_llm_prompt_tokens']}  completion={s['total_llm_completion_tokens']}")
# #         logger.info(f"TTS chars : {s['total_tts_characters']}")
# #         logger.info(sep)

# #     def save_to_file(self, session_id: str, e2e_list: list, output_dir: str = "metrics"):
# #         out = Path(__file__).resolve().parent / output_dir
# #         out.mkdir(exist_ok=True)
# #         ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
# #         path = out / f"metrics_{session_id}_{ts}.json"
# #         data = {
# #             "session_id":    session_id,
# #             "recorded_at":   datetime.now().isoformat(),
# #             "ttfb":          {k: v for k, v in self.ttfb.items()},
# #             "processing":    {k: v for k, v in self.proc.items()},
# #             "llm_tokens":    self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": e2e_list,
# #             "summary":       self.build_summary(e2e_list),
# #         }
# #         with open(path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved → {path}")
# #         return path


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False
# #     e2e_list    = []          # shared list filled by E2ETracker

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),  # more sensitive VAD
# #         ),
# #     )
 
# #     # ── Services ─────────────────────────────────────────────
# #     stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], language="en-US", model="nova-2")
# #     tts = DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], voice="aura-asteria-en")
# #     llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], model="llama-3.1-8b-instant", temperature=0.65, max_tokens=350)

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Processors ───────────────────────────────────────────
# #     interceptor       = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)
# #     e2e_tracker       = E2ETracker(e2e_list=e2e_list)
# #     metrics_collector = MetricsCollector()
# #     terminator        = SessionTerminator(ws_callback=ws_callback, session_id=session_id)

# #     # ── Pipeline ─────────────────────────────────────────────
# #     # E2ETracker FIRST so it sees UserStoppedSpeakingFrame / BotStartedSpeakingFrame
# #     # MetricsCollector LAST so it sees MetricsFrames from all services
# #     pipeline = Pipeline([
# #         transport.input(),
# #         e2e_tracker,            # ← catches VAD frames for E2E timing
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,            # ← email trigger
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,      # ← catches MetricsFrames
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #     )
# #     terminator.set_task(task)

# #     # ── Event handlers ────────────────────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # ── Goodbye detection FIRST, before anything else ────
# #         if terminator.is_goodbye(user_text):
# #             logger.info(f"👋 Goodbye phrase detected: '{user_text}'")
# #             asyncio.ensure_future(terminator.trigger_end())
# #             return   # ← IMPORTANT: do NOT process further (no RAG refresh, no lead detection, no LLM response)

# #         # ── RAG context refresh ──────────────────────────────
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # ── Lead detection ───────────────────────────────────
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary(e2e_list)
# #         metrics_collector.save_to_file(session_id, e2e_list)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline with Full Metrics (including E2E)
# # Metrics: Built-in + FrameProcessor that reads frame.data + custom E2E via transport events
# # Session metrics saved to JSON file.
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData, LLMUsageMetricsData, TTSUsageMetricsData

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}
# #     # Name
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break
# #     # Email
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]
# #     # Phone
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break
# #     # Requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(kw in msg["content"].lower() for kw in ["e-commerce", "platform", "system", "application", "features", "looking to", "you want", "module"]):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break
# #     # Budget
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break
# #     # Contact time
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break
# #     # Defaults
# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_active = False
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # Enhanced Metrics Collector (fixed for Pipecat 0.0.77)
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self, e2e_list: list = None):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)      # processor -> list of TTFB values
# #         self.proc = defaultdict(list)      # processor -> list of processing times
# #         self.llm_tokens = []               # list of (prompt, completion)
# #         self.tts_chars = []                # list of character counts
# #         self.e2e = e2e_list if e2e_list is not None else []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, MetricsFrame):
# #             metrics_list = []
# #             if hasattr(frame, 'data'):
# #                 data = frame.data
# #                 if isinstance(data, list):
# #                     metrics_list = data
# #                 elif data:
# #                     metrics_list = [data]
# #             elif hasattr(frame, 'metrics_data'):
# #                 metrics_list = [frame.metrics_data] if frame.metrics_data else []
# #             else:
# #                 logger.warning(f"MetricsFrame has no data or metrics_data: {dir(frame)}")
# #                 await self.push_frame(frame, direction)
# #                 return

# #             for md in metrics_list:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                     logger.info(f"📈 TTFB [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                     logger.info(f"⏱️ Processing [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     # Pipecat 0.0.77 stores token usage inside md.value
# #                     if hasattr(md, 'value') and hasattr(md.value, 'prompt_tokens'):
# #                         prompt_tokens = md.value.prompt_tokens
# #                         completion_tokens = md.value.completion_tokens
# #                     else:
# #                         # fallback for other versions
# #                         prompt_tokens = getattr(md, 'prompt_tokens', 0)
# #                         completion_tokens = getattr(md, 'completion_tokens', 0)
# #                     self.llm_tokens.append((prompt_tokens, completion_tokens))
# #                     logger.info(f"🎯 LLM tokens: prompt={prompt_tokens}, completion={completion_tokens}")
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     # TTS usage: md.value is the character count
# #                     self.tts_chars.append(md.value)
# #                     logger.info(f"🔊 TTS chars: {md.value}")
# #                 else:
# #                     logger.debug(f"Unknown metrics data type: {type(md)}")

# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _get_percentiles(values, percentiles=[50, 90, 95, 99]):
# #         if not values:
# #             return {}
# #         sorted_vals = sorted(values)
# #         return {p: sorted_vals[int(len(sorted_vals) * p / 100)] for p in percentiles}

# #     def report_summary(self):
# #         logger.info("=" * 60)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info("=" * 60)
# #         for proc in sorted(self.ttfb.keys()):
# #             p = self._get_percentiles(self.ttfb[proc])
# #             logger.info(f"TTFB [{proc}]: n={len(self.ttfb[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         for proc in sorted(self.proc.keys()):
# #             p = self._get_percentiles(self.proc[proc])
# #             logger.info(f"Processing [{proc}]: n={len(self.proc[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         if self.llm_tokens:
# #             total_prompt = sum(t[0] for t in self.llm_tokens)
# #             total_completion = sum(t[1] for t in self.llm_tokens)
# #             logger.info(f"LLM total tokens: prompt={total_prompt}, completion={total_completion}, total={total_prompt+total_completion}")
# #         if self.tts_chars:
# #             logger.info(f"TTS total characters: {sum(self.tts_chars)}")
# #         if self.e2e:
# #             p = self._get_percentiles(self.e2e)
# #             logger.info(f"E2E Latency (user stop → bot start): n={len(self.e2e)}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         logger.info("=" * 60)

# #     def save_to_file(self, session_id: str, output_dir: str = "metrics"):
# #         Path(output_dir).mkdir(exist_ok=True)
# #         file_path = Path(output_dir) / f"metrics_{session_id}.json"

# #         data = {
# #             "session_id": session_id,
# #             "ttfb": {proc: vals for proc, vals in self.ttfb.items()},
# #             "processing": {proc: vals for proc, vals in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": self.e2e,
# #             "summary": {
# #                 "ttfb_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.ttfb.items()},
# #                 "processing_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.proc.items()},
# #                 "e2e_percentiles": self._get_percentiles(self.e2e) if self.e2e else {},
# #                 "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #                 "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #                 "total_tts_characters": sum(self.tts_chars),
# #             }
# #         }
# #         with open(file_path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved to {file_path}")

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()
# #     lead_active = False

# #     # ----- E2E latency tracking -----
# #     e2e_times = []
# #     last_user_stop = None

# #     # Metrics collector (will be passed e2e list)
# #     metrics_collector = MetricsCollector(e2e_list=e2e_times)

# #     # Build pipeline components
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)

# #     # Pipeline: metrics_collector at the END to capture all metrics
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #         metrics_collector,
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #         enable_tracing=False,
# #         conversation_id=session_id,
# #     )

# #     # ----- E2E event handlers (correct signatures) -----
# #     @transport.event_handler("on_user_stopped_speaking")
# #     async def on_user_stopped(participant_id: str, timestamp_ms: int):
# #         nonlocal last_user_stop
# #         last_user_stop = asyncio.get_event_loop().time()
# #         logger.info(f"👤 User stopped speaking at {last_user_stop} (participant {participant_id})")

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started():
# #         nonlocal last_user_stop, e2e_times
# #         if last_user_stop is not None:
# #             e2e = asyncio.get_event_loop().time() - last_user_stop
# #             e2e_times.append(e2e)
# #             logger.info(f"🎯 E2E latency: {e2e:.3f}s")
# #             last_user_stop = None
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped():
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ----- Other event handlers -----
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         new_ctx = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary()
# #         metrics_collector.save_to_file(session_id)
# # ----working and saved matrix file ---
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline with Full Metrics (including E2E)
# # Metrics: Built-in + FrameProcessor that reads frame.data + custom E2E via transport events
# # Session metrics saved to JSON file.
# # """

# # from __future__ import annotations

# # import asyncio
# # import json
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData, LLMUsageMetricsData, TTSUsageMetricsData

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}
# #     # Name
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break
# #     # Email
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]
# #     # Phone
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break
# #     # Requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(kw in msg["content"].lower() for kw in ["e-commerce", "platform", "system", "application", "features", "looking to", "you want", "module"]):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break
# #     # Budget
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break
# #     # Contact time
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break
# #     # Defaults
# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_active = False
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # Enhanced Metrics Collector – now with E2E support & file save
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self, e2e_list: list = None):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)      # processor -> list of TTFB values
# #         self.proc = defaultdict(list)      # processor -> list of processing times
# #         self.llm_tokens = []               # list of (prompt, completion)
# #         self.tts_chars = []                # list of character counts
# #         self.e2e = e2e_list if e2e_list is not None else []

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)   # handle StartFrame etc.

# #         if isinstance(frame, MetricsFrame):
# #             # In this Pipecat version, metrics data is in frame.data (a list)
# #             metrics_list = []
# #             if hasattr(frame, 'data'):
# #                 data = frame.data
# #                 if isinstance(data, list):
# #                     metrics_list = data
# #                 elif data:
# #                     metrics_list = [data]
# #             elif hasattr(frame, 'metrics_data'):
# #                 # fallback for newer versions
# #                 metrics_list = [frame.metrics_data] if frame.metrics_data else []
# #             else:
# #                 logger.warning(f"MetricsFrame has no data or metrics_data: {dir(frame)}")
# #                 await self.push_frame(frame, direction)
# #                 return

# #             for md in metrics_list:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                     logger.info(f"📈 TTFB [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                     logger.info(f"⏱️ Processing [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     self.llm_tokens.append((md.prompt_tokens, md.completion_tokens))
# #                     logger.info(f"🎯 LLM tokens: prompt={md.prompt_tokens}, completion={md.completion_tokens}")
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     self.tts_chars.append(md.characters)
# #                     logger.info(f"🔊 TTS chars: {md.characters}")
# #                 else:
# #                     logger.debug(f"Unknown metrics data type: {type(md)}")

# #         await self.push_frame(frame, direction)

# #     @staticmethod
# #     def _get_percentiles(values, percentiles=[50, 90, 95, 99]):
# #         if not values:
# #             return {}
# #         sorted_vals = sorted(values)
# #         return {p: sorted_vals[int(len(sorted_vals) * p / 100)] for p in percentiles}

# #     def report_summary(self):
# #         logger.info("=" * 60)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info("=" * 60)
# #         for proc in sorted(self.ttfb.keys()):
# #             p = self._get_percentiles(self.ttfb[proc])
# #             logger.info(f"TTFB [{proc}]: n={len(self.ttfb[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         for proc in sorted(self.proc.keys()):
# #             p = self._get_percentiles(self.proc[proc])
# #             logger.info(f"Processing [{proc}]: n={len(self.proc[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         if self.llm_tokens:
# #             total_prompt = sum(t[0] for t in self.llm_tokens)
# #             total_completion = sum(t[1] for t in self.llm_tokens)
# #             logger.info(f"LLM total tokens: prompt={total_prompt}, completion={total_completion}, total={total_prompt+total_completion}")
# #         if self.tts_chars:
# #             logger.info(f"TTS total characters: {sum(self.tts_chars)}")
# #         if self.e2e:
# #             p = self._get_percentiles(self.e2e)
# #             logger.info(f"E2E Latency (user stop → bot start): n={len(self.e2e)}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         logger.info("=" * 60)

# #     def save_to_file(self, session_id: str, output_dir: str = "metrics"):
# #         Path(output_dir).mkdir(exist_ok=True)
# #         file_path = Path(output_dir) / f"metrics_{session_id}.json"

# #         # Build serialisable dictionary
# #         data = {
# #             "session_id": session_id,
# #             "ttfb": {proc: vals for proc, vals in self.ttfb.items()},
# #             "processing": {proc: vals for proc, vals in self.proc.items()},
# #             "llm_tokens": self.llm_tokens,
# #             "tts_characters": self.tts_chars,
# #             "e2e_latencies": self.e2e,
# #             "summary": {
# #                 "ttfb_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.ttfb.items()},
# #                 "processing_percentiles": {proc: self._get_percentiles(vals) for proc, vals in self.proc.items()},
# #                 "e2e_percentiles": self._get_percentiles(self.e2e) if self.e2e else {},
# #                 "total_llm_prompt_tokens": sum(t[0] for t in self.llm_tokens),
# #                 "total_llm_completion_tokens": sum(t[1] for t in self.llm_tokens),
# #                 "total_tts_characters": sum(self.tts_chars),
# #             }
# #         }
# #         with open(file_path, "w") as f:
# #             json.dump(data, f, indent=2)
# #         logger.info(f"📁 Metrics saved to {file_path}")

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()
# #     lead_active = False

# #     # ----- E2E latency tracking -----
# #     e2e_times = []          # list of deltas (user stop → bot start)
# #     last_user_stop = None   # timestamp of last user_stopped_speaking

# #     # 1. Create metrics collector (will be passed e2e list)
# #     metrics_collector = MetricsCollector(e2e_list=e2e_times)

# #     # 2. Build pipeline components
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         metrics_collector,   # <-- captures all metrics frames
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     # 3. PipelineTask – metrics enabled, tracing disabled
# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #         enable_tracing=False,          # disable OpenTelemetry (not needed)
# #         conversation_id=session_id,
# #     )

# #     # ----- E2E event handlers -----
# #     @transport.event_handler("on_user_stopped_speaking")
# #     async def on_user_stopped(transport, participant, timestamp_ms):
# #         nonlocal last_user_stop
# #         last_user_stop = asyncio.get_event_loop().time()
# #         logger.debug(f"User stopped speaking at {last_user_stop}")

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         nonlocal last_user_stop, e2e_times
# #         if last_user_stop is not None:
# #             e2e = asyncio.get_event_loop().time() - last_user_stop
# #             e2e_times.append(e2e)
# #             logger.info(f"🎯 E2E latency: {e2e:.3f}s")
# #             last_user_stop = None
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ----- Existing event handlers -----
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         new_ctx = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary()
# #         metrics_collector.save_to_file(session_id)   # persist metrics to JSON
# #---working version ----
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline with Working Metrics
# # Metrics: Built-in + FrameProcessor that reads frame.data
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from collections import defaultdict
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame, MetricsFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport
# # from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData, LLMUsageMetricsData, TTSUsageMetricsData

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]

# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)

# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)

# # def _extract_from_conversation(messages: list) -> dict:
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}
# #     # Name
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break
# #     # Email
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]
# #     # Phone
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break
# #     # Requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(kw in msg["content"].lower() for kw in ["e-commerce", "platform", "system", "application", "features", "looking to", "you want", "module"]):
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break
# #     # Budget
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break
# #     # Contact time
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", msg["content"], re.IGNORECASE)
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break
# #     # Defaults
# #     lead.setdefault("name", "Not provided")
# #     lead.setdefault("email", "Not provided")
# #     lead.setdefault("phone", "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget", "Not specified")
# #     lead.setdefault("contact_time", "ASAP")
# #     return lead

# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor (unchanged)
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages = messages
# #         self._ws_callback = ws_callback
# #         self._session_id = session_id
# #         self._buffer = ""
# #         self._lead_active = False
# #         self._email_sent = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)
# #         if direction == FrameDirection.DOWNSTREAM:
# #             if isinstance(frame, TextFrame):
# #                 self._buffer += frame.text or ""
# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))
# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""
# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# # # ─────────────────────────────────────────────────────────────
# # # Fixed Metrics Collector – reads frame.data instead of metrics_data
# # # ─────────────────────────────────────────────────────────────
# # class MetricsCollector(FrameProcessor):
# #     def __init__(self):
# #         super().__init__()
# #         self.ttfb = defaultdict(list)      # processor -> list of TTFB values
# #         self.proc = defaultdict(list)      # processor -> list of processing times
# #         self.llm_tokens = []               # list of (prompt, completion)
# #         self.tts_chars = []                # list of character counts

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)   # handle StartFrame etc.

# #         if isinstance(frame, MetricsFrame):
# #             # In this Pipecat version, metrics data is in frame.data (a list)
# #             metrics_list = []
# #             if hasattr(frame, 'data'):
# #                 data = frame.data
# #                 if isinstance(data, list):
# #                     metrics_list = data
# #                 elif data:
# #                     metrics_list = [data]
# #             elif hasattr(frame, 'metrics_data'):
# #                 # fallback for newer versions
# #                 metrics_list = [frame.metrics_data] if frame.metrics_data else []
# #             else:
# #                 logger.warning(f"MetricsFrame has no data or metrics_data: {dir(frame)}")
# #                 await self.push_frame(frame, direction)
# #                 return

# #             for md in metrics_list:
# #                 if isinstance(md, TTFBMetricsData):
# #                     self.ttfb[md.processor].append(md.value)
# #                     logger.info(f"📈 TTFB [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, ProcessingMetricsData):
# #                     self.proc[md.processor].append(md.value)
# #                     logger.info(f"⏱️ Processing [{md.processor}]: {md.value:.3f}s")
# #                 elif isinstance(md, LLMUsageMetricsData):
# #                     self.llm_tokens.append((md.prompt_tokens, md.completion_tokens))
# #                     logger.info(f"🎯 LLM tokens: prompt={md.prompt_tokens}, completion={md.completion_tokens}")
# #                 elif isinstance(md, TTSUsageMetricsData):
# #                     self.tts_chars.append(md.characters)
# #                     logger.info(f"🔊 TTS chars: {md.characters}")
# #                 else:
# #                     logger.debug(f"Unknown metrics data type: {type(md)}")

# #         await self.push_frame(frame, direction)

# #     def get_percentiles(self, values, percentiles=[50, 90, 95, 99]):
# #         if not values:
# #             return {}
# #         sorted_vals = sorted(values)
# #         return {p: sorted_vals[int(len(sorted_vals) * p / 100)] for p in percentiles}

# #     def report_summary(self):
# #         logger.info("=" * 60)
# #         logger.info("📊 METRICS SUMMARY")
# #         logger.info("=" * 60)
# #         for proc in sorted(self.ttfb.keys()):
# #             p = self.get_percentiles(self.ttfb[proc])
# #             logger.info(f"TTFB [{proc}]: n={len(self.ttfb[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         for proc in sorted(self.proc.keys()):
# #             p = self.get_percentiles(self.proc[proc])
# #             logger.info(f"Processing [{proc}]: n={len(self.proc[proc])}, p50={p.get(50,0):.3f}s, p90={p.get(90,0):.3f}s, p95={p.get(95,0):.3f}s, p99={p.get(99,0):.3f}s")
# #         if self.llm_tokens:
# #             total_prompt = sum(t[0] for t in self.llm_tokens)
# #             total_completion = sum(t[1] for t in self.llm_tokens)
# #             logger.info(f"LLM total tokens: prompt={total_prompt}, completion={total_completion}, total={total_prompt+total_completion}")
# #         if self.tts_chars:
# #             logger.info(f"TTS total characters: {sum(self.tts_chars)}")
# #         logger.info("=" * 60)

# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()
# #     lead_active = False

# #     # 1. Create metrics collector processor
# #     metrics_collector = MetricsCollector()

# #     # 2. Build pipeline
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg = SYSTEM_TEMPLATE.format(context=initial_ctx or "General WartinLabs knowledge.")
# #     messages = [
# #         {"role": "system", "content": system_msg},
# #         {"role": "user", "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     interceptor = LeadInterceptor(messages=messages, ws_callback=ws_callback, session_id=session_id)

# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         metrics_collector,   # <-- captures all metrics frames
# #         llm,
# #         interceptor,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     # 3. PipelineTask – metrics enabled, no observers/tracing
# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(
# #             allow_interruptions=True,
# #             enable_metrics=True,
# #             enable_usage_metrics=True,
# #         ),
# #         enable_tracing=False,          # disable OpenTelemetry (not needed)
# #         conversation_id=session_id,
# #     )

# #     # 4. Event handlers
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active
# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")
# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         new_ctx = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(context=new_ctx or "Use general WartinLabs knowledge.")
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     try:
# #         await runner.run(task)
# #     finally:
# #         metrics_collector.report_summary()
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame, LLMFullResponseEndFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # # Every phrase the LLM uses to close a lead
# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "reach out within 24 hours",
# #     "get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "team will reach out",
# #     "will reach out to you",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     """Extract lead fields by scanning the full conversation."""
# #     full = " ".join(
# #         m["content"] for m in messages
# #         if m["role"] in ("user", "assistant")
# #     )
# #     lead: dict = {}

# #     # ── Name ──────────────────────────────────────────────────
# #     for pat in [
# #         r"my name is ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:nice to meet you|hi|hello|thanks?)[,\s]+([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #         r"(?:full name is|name is) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             skip = {"you", "there", "sure", "great", "welcome", "shivam", "aria"}
# #             if candidate.lower() not in skip:
# #                 lead["name"] = candidate
# #                 break

# #     # Specific fallback: user said "My name is X"
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(
# #                     r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)",
# #                     msg["content"], re.IGNORECASE
# #                 )
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     # ── Email ─────────────────────────────────────────────────
# #     # Collect from user messages only (more reliable than bot summaries)
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", msg["content"])
# #             if emails:
# #                 lead["email"] = emails[-1]
# #                 break
# #     # Fallback: scan everything
# #     if "email" not in lead:
# #         emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #         if emails:
# #             lead["email"] = emails[-1]

# #     # ── Phone ─────────────────────────────────────────────────
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"[\+\(]?[\d][\d\s\-().]{6,18}\d", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     # ── Requirements ─────────────────────────────────────────
# #     # Look for the bot's summary of project requirements
# #     for msg in reversed(messages):
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["e-commerce", "platform", "system", "application",
# #                        "features", "looking to", "you want", "module"]
# #         ):
# #             # Take the first 200 chars of that assistant message
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     # ── Budget ────────────────────────────────────────────────
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"(?:\$[\d,]+(?:k)?|[\d]+(?:,[\d]+)?\s*(?:k|thousand|USD|dollars?))",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["budget"] = m.group(0).strip()
# #                 break
# #     # Fallback: bot confirmed budget
# #     if "budget" not in lead:
# #         for msg in reversed(messages):
# #             if msg["role"] == "assistant" and "budget" in msg["content"].lower():
# #                 m = re.search(r"(?:budget|around|approximately) ([^.]+)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["budget"] = m.group(1).strip()[:50]
# #                     break

# #     # ── Contact Time ─────────────────────────────────────────
# #     for msg in reversed(messages):
# #         if msg["role"] == "user":
# #             m = re.search(
# #                 r"\b(morning|afternoon|evening|night|tomorrow|today|weekend|weekday|anytime|asap|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #                 msg["content"], re.IGNORECASE
# #             )
# #             if m:
# #                 lead["contact_time"] = m.group(1).strip()
# #                 break

# #     # ── Defaults ─────────────────────────────────────────────
# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LeadInterceptor – sits between LLM and TTS in pipeline
# # # Accumulates text per LLM response (resets on LLMFullResponseEndFrame)
# # # Fires email when completion phrase is detected
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):

# #     def __init__(self, messages: list, ws_callback, session_id: str):
# #         super().__init__()
# #         self._messages    = messages   # live reference to context message list
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""         # accumulates text for current LLM response
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True
# #         logger.info("LeadInterceptor: lead collection active")

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if direction == FrameDirection.DOWNSTREAM:

# #             if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
# #                 self._buffer += frame.text or ""
# #                 # logger.debug(f"Buffer now: {self._buffer!r}")
# #                 # logger.debug(f"_lead_active={self._lead_active}, _email_sent={self._email_sent}")
# #                 # logger.debug(f"_is_completion={_is_completion(self._buffer)}")

# #                 if not self._email_sent and _is_completion(self._buffer):
# #                     self._email_sent = True
# #                     logger.info("✅ Completion phrase detected – sending email")
# #                     lead_data = _extract_from_conversation(self._messages)
# #                     logger.info(f"Extracted lead: {lead_data}")
# #                     asyncio.ensure_future(self._fire_email(lead_data))

# #             elif isinstance(frame, LLMFullResponseEndFrame):
# #                 # LLM finished this response – log and reset buffer for next turn
# #                 if self._buffer:
# #                     logger.debug(f"LeadInterceptor: response ended, full buffer was: {repr(self._buffer[:200])}")
# #                 self._buffer = ""

# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Lead Interceptor ─────────────────────────────────────
# #     interceptor = LeadInterceptor(
# #         messages=messages,
# #         ws_callback=ws_callback,
# #         session_id=session_id,
# #     )

# #     # ── Pipeline ─────────────────────────────────────────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,        # ← between LLM and TTS, reads every text frame
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Activate lead collection once triggered
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "will get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "get in touch within 24 hours",
# #     "reach out within 24 hours",
# #     "team will reach out",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     """Extract lead data by scanning the full conversation history."""
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     # Name – look for bot confirming the name
# #     for pat in [
# #         r"(?:your name is|hi|hello|thanks?,?|great,?|noted,?|so,?)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #         r"(?:lovely|nice) to (?:meet|have) you[,\s]+([A-Z][a-zA-Z]+)",
# #         r"my name is ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             # Filter out common words that aren't names
# #             if candidate.lower() not in ("you", "there", "welcome", "sure", "great", "alex"):
# #                 lead["name"] = candidate
# #                 break

# #     # Also try user messages directly
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     # Email – most reliable: just find @
# #     emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #     if emails:
# #         lead["email"] = emails[-1]

# #     # Phone – find in user messages first (more reliable)
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\+?[\d][\d\s\-().]{6,18}\d)", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     # Requirements – look for project description phrases
# #     for msg in messages:
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["looking to build", "project is", "you want to", "you're interested in", "features you're looking"]
# #         ):
# #             # Take the assistant's summary of requirements
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     # Budget – find dollar amounts or "k" values in conversation
# #     budgets = re.findall(
# #         r"(?:budget|cost|around|about)?\s*(?:\$[\d,]+(?:k|K)?|[\d]+(?:,[\d]+)?\s*(?:k|K|thousand|USD|dollars?))",
# #         full, re.IGNORECASE
# #     )
# #     if budgets:
# #         lead["budget"] = budgets[-1].strip()

# #     # Contact time
# #     for pat in [
# #         r"\b(morning|afternoon|evening|night|weekday|weekend|anytime|asap|today|tomorrow)\b",
# #         r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             lead["contact_time"] = m.group(1).strip()
# #             break

# #     # Defaults
# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LLM Text Interceptor
# # # Sits BETWEEN LLM and TTS, reads every text frame as it streams
# # # Detects completion phrase and fires email IMMEDIATELY
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     """
# #     Intercepts text frames from the LLM before they reach TTS.
# #     Accumulates text per response, detects completion phrase,
# #     fires the lead email with real data extracted from conversation.
# #     """

# #     def __init__(self, context: OpenAILLMContext, messages: list,
# #                  ws_callback, session_id: str):
# #         super().__init__()
# #         self._context     = context
# #         self._messages    = messages     # same list object as context messages
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""           # accumulates current LLM response
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
# #             text = frame.text or ""
# #             self._buffer += text

# #             # Check for completion phrase in the streaming buffer
# #             if (
# #                 self._lead_active
# #                 and not self._email_sent
# #                 and _is_completion(self._buffer)
# #             ):
# #                 self._email_sent = True
# #                 logger.info("✅ Completion phrase detected in stream – sending lead email now")

# #                 # Use current messages list for extraction
# #                 lead_data = _extract_from_conversation(self._messages)
# #                 logger.info(f"Extracted lead: {lead_data}")

# #                 # Fire email immediately (don't await, don't block TTS)
# #                 asyncio.ensure_future(self._fire_email(lead_data))

# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# #     def reset_buffer(self):
# #         """Call at the start of each new LLM response."""
# #         self._buffer = ""


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Lead interceptor (sits between LLM and TTS) ──────────
# #     interceptor = LeadInterceptor(
# #         context=context,
# #         messages=messages,
# #         ws_callback=ws_callback,
# #         session_id=session_id,
# #     )

# #     # ── Pipeline  (interceptor between LLM and TTS) ──────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,       # ← reads every text frame from LLM
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Activate lead collection
# #             logger.info("Lead collection started")
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()
# #             logger.info("Lead collection started")

# #         # Reset interceptor buffer for new response
# #         interceptor.reset_buffer()

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import Frame, TextFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY this phrase:
# #      "Our team will reach out to you within 24 hours."

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs. I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services. I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]

# # _COMPLETION_PHRASES = [
# #     "reach out to you within 24 hours",
# #     "will get back to you within 24 hours",
# #     "contact you within 24 hours",
# #     "get in touch within 24 hours",
# #     "reach out within 24 hours",
# #     "team will reach out",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     return any(kw in text.lower() for kw in _LEAD_TRIGGERS)


# # def _is_completion(text: str) -> bool:
# #     t = text.lower()
# #     return any(phrase in t for phrase in _COMPLETION_PHRASES)


# # def _extract_from_conversation(messages: list) -> dict:
# #     """Extract lead data by scanning the full conversation history."""
# #     full = " ".join(m["content"] for m in messages if m["role"] in ("user", "assistant"))
# #     lead: dict = {}

# #     # Name – look for bot confirming the name
# #     for pat in [
# #         r"(?:your name is|hi|hello|thanks?,?|great,?|noted,?|so,?)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #         r"(?:lovely|nice) to (?:meet|have) you[,\s]+([A-Z][a-zA-Z]+)",
# #         r"my name is ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             candidate = m.group(1).strip()
# #             # Filter out common words that aren't names
# #             if candidate.lower() not in ("you", "there", "welcome", "sure", "great", "alex"):
# #                 lead["name"] = candidate
# #                 break

# #     # Also try user messages directly
# #     if "name" not in lead:
# #         for msg in messages:
# #             if msg["role"] == "user":
# #                 m = re.search(r"(?:my name is|i(?:'m| am)) ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", msg["content"], re.IGNORECASE)
# #                 if m:
# #                     lead["name"] = m.group(1).strip()
# #                     break

# #     # Email – most reliable: just find @
# #     emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", full)
# #     if emails:
# #         lead["email"] = emails[-1]

# #     # Phone – find in user messages first (more reliable)
# #     for msg in messages:
# #         if msg["role"] == "user":
# #             m = re.search(r"(?:\+?[\d][\d\s\-().]{6,18}\d)", msg["content"])
# #             if m:
# #                 lead["phone"] = m.group(0).strip()
# #                 break

# #     # Requirements – look for project description phrases
# #     for msg in messages:
# #         if msg["role"] == "assistant" and any(
# #             kw in msg["content"].lower()
# #             for kw in ["looking to build", "project is", "you want to", "you're interested in", "features you're looking"]
# #         ):
# #             # Take the assistant's summary of requirements
# #             lead.setdefault("requirements", msg["content"][:200])
# #             break

# #     # Budget – find dollar amounts or "k" values in conversation
# #     budgets = re.findall(
# #         r"(?:budget|cost|around|about)?\s*(?:\$[\d,]+(?:k|K)?|[\d]+(?:,[\d]+)?\s*(?:k|K|thousand|USD|dollars?))",
# #         full, re.IGNORECASE
# #     )
# #     if budgets:
# #         lead["budget"] = budgets[-1].strip()

# #     # Contact time
# #     for pat in [
# #         r"\b(morning|afternoon|evening|night|weekday|weekend|anytime|asap|today|tomorrow)\b",
# #         r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
# #     ]:
# #         m = re.search(pat, full, re.IGNORECASE)
# #         if m:
# #             lead["contact_time"] = m.group(1).strip()
# #             break

# #     # Defaults
# #     lead.setdefault("name",         "Not provided")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "ASAP")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # LLM Text Interceptor
# # # Sits BETWEEN LLM and TTS, reads every text frame as it streams
# # # Detects completion phrase and fires email IMMEDIATELY
# # # ─────────────────────────────────────────────────────────────
# # class LeadInterceptor(FrameProcessor):
# #     """
# #     Intercepts text frames from the LLM before they reach TTS.
# #     Accumulates text per response, detects completion phrase,
# #     fires the lead email with real data extracted from conversation.
# #     """

# #     def __init__(self, context: OpenAILLMContext, messages: list,
# #                  ws_callback, session_id: str):
# #         super().__init__()
# #         self._context     = context
# #         self._messages    = messages     # same list object as context messages
# #         self._ws_callback = ws_callback
# #         self._session_id  = session_id
# #         self._buffer      = ""           # accumulates current LLM response
# #         self._lead_active = False
# #         self._email_sent  = False

# #     def mark_lead_active(self):
# #         self._lead_active = True

# #     async def process_frame(self, frame: Frame, direction: FrameDirection):
# #         await super().process_frame(frame, direction)

# #         if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
# #             text = frame.text or ""
# #             self._buffer += text

# #             # Check for completion phrase in the streaming buffer
# #             if (
# #                 self._lead_active
# #                 and not self._email_sent
# #                 and _is_completion(self._buffer)
# #             ):
# #                 self._email_sent = True
# #                 logger.info("✅ Completion phrase detected in stream – sending lead email now")

# #                 # Use current messages list for extraction
# #                 lead_data = _extract_from_conversation(self._messages)
# #                 logger.info(f"Extracted lead: {lead_data}")

# #                 # Fire email immediately (don't await, don't block TTS)
# #                 asyncio.ensure_future(self._fire_email(lead_data))

# #         await self.push_frame(frame, direction)

# #     async def _fire_email(self, lead_data: dict):
# #         try:
# #             sent = await send_lead_email(lead_data)
# #             if self._ws_callback:
# #                 await self._ws_callback("lead_captured", {
# #                     "data": lead_data,
# #                     "email_sent": sent,
# #                     "session_id": self._session_id,
# #                 })
# #         except Exception as e:
# #             logger.error(f"Lead email error: {e}")

# #     def reset_buffer(self):
# #         """Call at the start of each new LLM response."""
# #         self._buffer = ""


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     lead_active = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Lead interceptor (sits between LLM and TTS) ──────────
# #     interceptor = LeadInterceptor(
# #         context=context,
# #         messages=messages,
# #         ws_callback=ws_callback,
# #         session_id=session_id,
# #     )

# #     # ── Pipeline  (interceptor between LLM and TTS) ──────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         interceptor,       # ← reads every text frame from LLM
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")
# #         transport.capture_participant_audio(pid)
# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})
# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Activate lead collection
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             interceptor.mark_lead_active()
# #             logger.info("Lead collection started")

# #         # Reset interceptor buffer for new response
# #         interceptor.reset_buffer()

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)
# # """
# # WartinLabs Voice Agent – Pipecat Pipeline
# # ─────────────────────────────────────────
# # Transport : Daily (WebRTC)       – free 10k min/month
# # STT       : Deepgram Nova-2      – free 12k min/year
# # TTS       : Deepgram Aura        – free (same key)
# # LLM       : Groq llama-3.1-8b   – free tier
# # VAD       : Silero               – fully local
# # RAG       : FAISS + MiniLM       – fully local
# # """

# # from __future__ import annotations

# # import asyncio
# # import os
# # import re
# # import sys
# # from pathlib import Path
# # from typing import Callable, Awaitable

# # from dotenv import load_dotenv
# # from loguru import logger

# # sys.path.insert(0, str(Path(__file__).resolve().parent))
# # load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# # from pipecat.audio.vad.silero import SileroVADAnalyzer
# # from pipecat.frames.frames import TextFrame
# # from pipecat.pipeline.pipeline import Pipeline
# # from pipecat.pipeline.runner import PipelineRunner
# # from pipecat.pipeline.task import PipelineParams, PipelineTask
# # from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
# # from pipecat.services.deepgram.stt import DeepgramSTTService
# # from pipecat.services.deepgram.tts import DeepgramTTSService
# # from pipecat.services.groq.llm import GroqLLMService
# # from pipecat.transports.services.daily import DailyParams, DailyTransport

# # from rag_engine import retrieve, ensure_index
# # from lead_capture import send_lead_email

# # # ─────────────────────────────────────────────────────────────
# # # System prompt
# # # ─────────────────────────────────────────────────────────────
# # SYSTEM_TEMPLATE = """\
# # You are Aria, the warm and professional AI voice assistant for WartinLabs – \
# # a leading AI and software development company.

# # PERSONALITY:
# # - Conversational, friendly, enthusiastic about WartinLabs work
# # - Responses MUST be 2-4 short sentences max (this is voice – brevity matters)
# # - Never use bullet points, asterisks, markdown, or lists in your responses
# # - Use natural spoken transitions: "Sure!", "Great question!", "Absolutely!"
# # - Speak numbers and prices naturally: "ten thousand dollars" not "$10,000"
# # - For emails say: "info at wartinlabs dot com"

# # RELEVANT KNOWLEDGE FOR THIS TURN:
# # {context}

# # LEAD CAPTURE RULES:
# # When the user wants to contact WartinLabs, start a project, get a quote,
# # book a consultation, discuss requirements, or hire us:
# #   1. Warmly say you will connect them with the team
# #   2. Collect these details ONE AT A TIME in this exact order:
# #      - Full name
# #      - Email address
# #      - Phone number
# #      - Project description
# #      - Budget range
# #      - Preferred contact time
# #   3. After collecting ALL six details, confirm them back and end with EXACTLY:
# #      "Our team will reach out to you within 24 hours."
# #   4. Then on the very next line write this hidden tag (not spoken aloud):
# #      [LEAD_COMPLETE: name=<name> | email=<email> | phone=<phone> | requirements=<requirements> | budget=<budget> | contact_time=<contact_time>]

# # RESPONSE RULES:
# # - If unsure of exact details: "Let me connect you with our team for the exact answer"
# # - NEVER fabricate prices, timelines, or client names
# # - Keep every response SHORT for voice
# # """

# # OPENING = (
# #     "Welcome to WartinLabs I'm Aria, your AI assistant. "
# #     "We specialize in AI solutions, custom software development, automation, "
# #     "and digital transformation services I'd be happy to answer your questions "
# #     "or connect you with our team. How can I assist you today?"
# # )

# # # ─────────────────────────────────────────────────────────────
# # # Lead detection helpers
# # # ─────────────────────────────────────────────────────────────
# # _LEAD_TAG = re.compile(
# #     r"\[LEAD_COMPLETE:\s*"
# #     r"name=(?P<name>[^|]+)\|?\s*"
# #     r"email=(?P<email>[^|]+)\|?\s*"
# #     r"phone=(?P<phone>[^|]+)\|?\s*"
# #     r"requirements=(?P<requirements>[^|]+)\|?\s*"
# #     r"budget=(?P<budget>[^|]+)\|?\s*"
# #     r"contact_time=(?P<contact_time>[^\]]+)"
# #     r"\]",
# #     re.IGNORECASE | re.DOTALL,
# # )

# # _COMPLETION_SIGNAL = re.compile(
# #     r"(reach out|get back|contact you).{0,30}24 hours",
# #     re.IGNORECASE,
# # )

# # _LEAD_TRIGGERS = [
# #     "contact", "reach out", "get in touch", "book", "consultation",
# #     "quote", "proposal", "start a project", "discuss", "hire",
# #     "want to work", "interested in", "enquiry", "inquiry",
# #     "call me", "connect me", "build", "develop", "create",
# # ]


# # def _wants_lead(text: str) -> bool:
# #     t = text.lower()
# #     return any(kw in t for kw in _LEAD_TRIGGERS)


# # def _parse_lead_tag(text: str) -> dict | None:
# #     """Extract structured lead data from the [LEAD_COMPLETE:...] tag."""
# #     m = _LEAD_TAG.search(text)
# #     if not m:
# #         return None
# #     return {k: v.strip() for k, v in m.groupdict().items() if v}


# # def _scrape_lead_from_history(messages: list) -> dict:
# #     """
# #     Fallback: scan full conversation history and extract
# #     name / email / phone / requirements using regex.
# #     """
# #     full_text = " ".join(
# #         m["content"] for m in messages if m["role"] in ("user", "assistant")
# #     )
# #     lead: dict = {}

# #     # Name
# #     for pat in [
# #         r"(?:your name is|name:?)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
# #         r"(?:lovely|great|nice) to (?:have you|meet you)[,\s]+([A-Z][a-zA-Z]+)",
# #         r"(?:got it|noted)[,\s]+([A-Z][a-zA-Z]+)",
# #     ]:
# #         m = re.search(pat, full_text)
# #         if m:
# #             lead["name"] = m.group(1).strip()
# #             break

# #     # Email
# #     m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", full_text)
# #     if m:
# #         lead["email"] = m.group(0)

# #     # Phone – 7+ digit sequence
# #     m = re.search(r"\b[\d\s\-+()]{7,20}\b", full_text)
# #     if m:
# #         digits = re.sub(r"\D", "", m.group(0))
# #         if len(digits) >= 7:
# #             lead["phone"] = digits

# #     # Requirements
# #     for pat in [
# #         r"(?:looking to build|want to build|build a?)\s+(.{10,120}?)(?:\.|,|\n|$)",
# #         r"(?:multi.?gateway|payment|saas|mobile app|web app|e.?commerce).{0,80}",
# #     ]:
# #         m = re.search(pat, full_text, re.IGNORECASE)
# #         if m:
# #             lead.setdefault("requirements", m.group(0).strip()[:200])
# #             break

# #     # Defaults for anything not found
# #     lead.setdefault("name",         "Unknown")
# #     lead.setdefault("email",        "Not provided")
# #     lead.setdefault("phone",        "Not provided")
# #     lead.setdefault("requirements", "Discussed in conversation")
# #     lead.setdefault("budget",       "Not specified")
# #     lead.setdefault("contact_time", "As soon as possible")

# #     return lead


# # # ─────────────────────────────────────────────────────────────
# # # Bot entry point
# # # ─────────────────────────────────────────────────────────────
# # async def run_bot(
# #     room_url: str,
# #     token: str,
# #     session_id: str,
# #     ws_callback: Callable[[str, dict], Awaitable[None]] | None = None,
# # ):
# #     ensure_index()

# #     # shared mutable state (nonlocal inside handlers)
# #     lead_active = False
# #     email_sent  = False

# #     # ── Transport ────────────────────────────────────────────
# #     transport = DailyTransport(
# #         room_url, token,
# #         "Aria – WartinLabs AI",
# #         DailyParams(
# #             audio_in_enabled=True,
# #             audio_out_enabled=True,
# #             vad_analyzer=SileroVADAnalyzer(),
# #         ),
# #     )

# #     # ── STT ──────────────────────────────────────────────────
# #     stt = DeepgramSTTService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         language="en-US",
# #         model="nova-2",
# #     )

# #     # ── TTS ──────────────────────────────────────────────────
# #     tts = DeepgramTTSService(
# #         api_key=os.environ["DEEPGRAM_API_KEY"],
# #         voice="aura-asteria-en",
# #     )

# #     # ── LLM ──────────────────────────────────────────────────
# #     llm = GroqLLMService(
# #         api_key=os.environ["GROQ_API_KEY"],
# #         model="llama-3.1-8b-instant",
# #         temperature=0.65,
# #         max_tokens=350,
# #     )

# #     # ── Context ──────────────────────────────────────────────
# #     initial_ctx = retrieve("WartinLabs services overview capabilities", top_k=4)
# #     system_msg  = SYSTEM_TEMPLATE.format(
# #         context=initial_ctx or "General WartinLabs knowledge."
# #     )
# #     messages = [
# #         {"role": "system",    "content": system_msg},
# #         {"role": "user",      "content": "Hello"},
# #         {"role": "assistant", "content": OPENING},
# #     ]
# #     context = OpenAILLMContext(messages)
# #     ctx_agg = llm.create_context_aggregator(context)

# #     # ── Pipeline ─────────────────────────────────────────────
# #     pipeline = Pipeline([
# #         transport.input(),
# #         stt,
# #         ctx_agg.user(),
# #         llm,
# #         tts,
# #         transport.output(),
# #         ctx_agg.assistant(),
# #     ])

# #     task = PipelineTask(
# #         pipeline,
# #         params=PipelineParams(allow_interruptions=True, enable_metrics=True),
# #     )

# #     # ── Lead completion checker ───────────────────────────────
# #     async def _check_for_lead_completion():
# #         nonlocal email_sent
# #         if email_sent:
# #             return

# #         msgs = context.get_messages()
# #         # Scan ALL assistant messages for the tag, not just the last one
# #         all_assistant_text = " ".join(
# #             m["content"] for m in msgs if m["role"] == "assistant"
# #         )

# #         # Method 1 – structured tag from LLM
# #         lead_data = _parse_lead_tag(all_assistant_text)
# #         if lead_data:
# #             logger.info(f"Lead tag found: {lead_data}")
# #             email_sent = True
# #             sent = await send_lead_email(lead_data)
# #             if ws_callback:
# #                 await ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": session_id
# #                 })
# #             return

# #         # Method 2 – completion phrase detected, scrape history as fallback
# #         if _COMPLETION_SIGNAL.search(all_assistant_text) and lead_active:
# #             logger.info("Completion signal detected – scraping lead from history")
# #             lead_data = _scrape_lead_from_history(msgs)
# #             logger.info(f"Scraped lead: {lead_data}")
# #             email_sent = True
# #             sent = await send_lead_email(lead_data)
# #             if ws_callback:
# #                 await ws_callback("lead_captured", {
# #                     "data": lead_data, "email_sent": sent, "session_id": session_id
# #                 })
# #     # ── on_first_participant_joined ───────────────────────────
# #     @transport.event_handler("on_first_participant_joined")
# #     async def on_first_participant_joined(transport, participant):
# #         pid = participant["id"]
# #         logger.info(f"Participant joined: {pid}")

# #         transport.capture_participant_audio(pid)

# #         if ws_callback:
# #             await ws_callback("bot_text", {"text": OPENING, "session_id": session_id})

# #         await task.queue_frames([TextFrame(OPENING)])

# #     # ── on_transcription_message ──────────────────────────────
# #     @transport.event_handler("on_transcription_message")
# #     async def on_transcript(transport, message):
# #         nonlocal lead_active

# #         user_text = message.get("text", "").strip()
# #         if not user_text:
# #             return
# #         logger.info(f"USER [{session_id}]: {user_text}")

# #         if ws_callback:
# #             await ws_callback("user_text", {"text": user_text, "session_id": session_id})

# #         # Refresh RAG context in system prompt
# #         new_ctx    = retrieve(user_text, top_k=4)
# #         new_system = SYSTEM_TEMPLATE.format(
# #             context=new_ctx or "Use general WartinLabs knowledge."
# #         )
# #         for m in context.get_messages():
# #             if m["role"] == "system":
# #                 m["content"] = new_system
# #                 break

# #         # Mark lead as active once user shows intent
# #         if not lead_active and _wants_lead(user_text):
# #             lead_active = True
# #             logger.info("Lead collection started")

# #     # ── bot speaking events ───────────────────────────────────
# #     @transport.event_handler("on_bot_started_speaking")
# #     async def on_bot_started(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_start", {"session_id": session_id})

# #     @transport.event_handler("on_bot_stopped_speaking")
# #     async def on_bot_stopped(transport):
# #         if ws_callback:
# #             await ws_callback("bot_speaking_end", {"session_id": session_id})
# #         # Wait for context aggregator to save assistant message, then check
# #         if lead_active and not email_sent:
# #             await asyncio.sleep(1.5)
# #             await _check_for_lead_completion()

# #     # ── Run ──────────────────────────────────────────────────
# #     logger.info(f"Starting pipeline for session {session_id}")
# #     runner = PipelineRunner()
# #     await runner.run(task)