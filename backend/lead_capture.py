# """
# WartinLabs Lead Capture & Email Notification
# Uses Resend free tier (3,000 emails / month).
# """

# from __future__ import annotations

# import os
# from datetime import datetime
# from loguru import logger


# async def send_lead_email(lead: dict) -> bool:
#     """Send a rich HTML lead notification to the WartinLabs team."""
#     try:
#         import resend
#         resend.api_key = os.getenv("RESEND_API_KEY", "")
#         if not resend.api_key:
#             logger.warning("RESEND_API_KEY not set – lead email skipped")
#             return False

#         to_addr   = os.getenv("WARTIN_LABS_EMAIL", "contact@wartinlabs.com")
#         from_addr = os.getenv("FROM_EMAIL", "onboarding@resend.dev")
#         ts        = datetime.now().strftime("%d %B %Y, %I:%M %p")

#         def row(label: str, key: str) -> str:
#             val = lead.get(key, "").strip()
#             if not val:
#                 return ""
#             return f"""
#             <tr>
#               <td style="padding:10px 16px;color:#6b7280;font-size:12px;
#                          font-weight:600;text-transform:uppercase;
#                          letter-spacing:.5px;white-space:nowrap;
#                          border-bottom:1px solid #f3f4f6;">{label}</td>
#               <td style="padding:10px 16px;color:#111827;font-size:14px;
#                          border-bottom:1px solid #f3f4f6;">{val}</td>
#             </tr>"""

#         html = f"""<!DOCTYPE html>
# <html lang="en">
# <head><meta charset="utf-8">
# <style>
#   body{{margin:0;padding:24px;background:#f9fafb;
#        font-family:'Segoe UI',Arial,sans-serif}}
#   .card{{max-width:580px;margin:0 auto;background:#fff;
#          border-radius:16px;overflow:hidden;
#          box-shadow:0 4px 24px rgba(0,0,0,.08)}}
#   .hdr{{background:linear-gradient(135deg,#1e1b4b,#312e81,#1d4ed8);
#         padding:32px;text-align:center;color:#fff}}
#   .hdr h1{{margin:0 0 6px;font-size:22px;letter-spacing:.5px}}
#   .hdr p{{margin:0;font-size:13px;opacity:.8}}
#   .badge{{display:inline-block;margin-top:14px;padding:4px 14px;
#           background:#f97316;color:#fff;border-radius:20px;
#           font-size:11px;font-weight:700;letter-spacing:.5px}}
#   table{{width:100%;border-collapse:collapse}}
#   .footer{{background:#f9fafb;padding:16px 24px;text-align:center;
#            font-size:11px;color:#9ca3af}}
# </style>
# </head>
# <body>
# <div class="card">
#   <div class="hdr">
#     <h1>🎯 New Lead – WartinLabs Voice Agent</h1>
#     <p>Captured on {ts}</p>
#     <span class="badge">HOT LEAD</span>
#   </div>
#   <div style="padding:0">
#     <table>
#       {row("Name",           "name")}
#       {row("Company",        "company")}
#       {row("Email",          "email")}
#       {row("Phone",          "phone")}
#       {row("Requirements",   "requirements")}
#       {row("Budget",         "budget")}
#       {row("Contact Time",   "contact_time")}
#     </table>
#   </div>
#   <div class="footer">
#     Captured automatically by the <strong>WartinLabs AI Voice Agent (Aria)</strong>.<br>
#     Please follow up within 24 hours for best conversion. 🚀
#   </div>
# </div>
# </body>
# </html>"""

#         params = resend.Emails.SendParams(
#             from_=f"WartinLabs Voice Agent <{from_addr}>",
#             to=[to_addr],
#             subject=f"🎯 New Lead: {lead.get('name','Unknown')} – Voice Agent",
#             html=html,
#         )
#         result = resend.Emails.send(params)
#         logger.info(f"Lead email sent – id={result.get('id','?')}")
#         return True

#     except ImportError:
#         logger.error("resend not installed: pip install resend")
#     except Exception as exc:
#         logger.error(f"Lead email failed: {exc}")
#     return False
"""
WartinLabs Lead Capture & Email Notification
Uses Gmail SMTP with App Password (free, no third-party service needed).

Setup:
  1. Go to https://myaccount.google.com/security
  2. Enable 2-Step Verification
  3. Go to https://myaccount.google.com/apppasswords
  4. Create app password → name it "WartinLabs Bot" → copy the 16-char password
  5. Add to .env:
       GMAIL_USER=your@gmail.com
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""

from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from loguru import logger

# ── Fixed recipient ───────────────────────────────────────────
TO_NAME  = "Shivam Sharma"
TO_EMAIL = "shivam@wartinlabs.com"


def _build_html(lead: dict, ts: str) -> str:
    def row(label: str, key: str) -> str:
        val = (lead.get(key) or "").strip()
        if not val:
            return ""
        return f"""
        <tr>
          <td style="padding:11px 18px;color:#6b7280;font-size:12px;font-weight:600;
                     text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;
                     border-bottom:1px solid #f3f4f6;background:#fafafa;">{label}</td>
          <td style="padding:11px 18px;color:#111827;font-size:14px;
                     border-bottom:1px solid #f3f4f6;">{val}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8">
<style>
  body{{margin:0;padding:24px;background:#f0f2f5;font-family:'Segoe UI',Arial,sans-serif}}
  .wrap{{max-width:600px;margin:0 auto}}
  .card{{background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10)}}
  .hdr{{background:linear-gradient(135deg,#1e1b4b 0%,#3730a3 50%,#1d4ed8 100%);
        padding:36px 32px;text-align:center;color:#fff}}
  .hdr h1{{margin:0 0 8px;font-size:24px;font-weight:700;letter-spacing:.5px}}
  .hdr p{{margin:0;font-size:13px;opacity:.8}}
  .badge{{display:inline-block;margin-top:14px;padding:5px 16px;background:#f97316;
          color:#fff;border-radius:20px;font-size:11px;font-weight:700;
          letter-spacing:.8px;text-transform:uppercase}}
  .greeting{{padding:24px 28px 8px;font-size:15px;color:#374151;line-height:1.6}}
  table{{width:100%;border-collapse:collapse}}
  .footer{{background:#f8fafc;padding:20px 28px;text-align:center;
           font-size:12px;color:#9ca3af;line-height:1.7}}
  .footer strong{{color:#6b7280}}
</style>
</head>
<body>
<div class="wrap"><div class="card">
  <div class="hdr">
    <h1>🎯 New Lead Captured</h1>
    <p>WartinLabs AI Voice Agent — Aria</p>
    <span class="badge">🔥 Hot Lead</span>
  </div>
  <div class="greeting">
    Hi <strong>{TO_NAME}</strong>,<br><br>
    A new lead was just captured by the WartinLabs voice agent. Here are the details:
  </div>
  <div style="padding:8px 0 16px">
    <table>
      {row("Full Name",       "name")}
      {row("Company",         "company")}
      {row("Email Address",   "email")}
      {row("Phone Number",    "phone")}
      {row("Project Details", "requirements")}
      {row("Budget Range",    "budget")}
      {row("Contact Time",    "contact_time")}
      <tr>
        <td style="padding:11px 18px;color:#6b7280;font-size:12px;font-weight:600;
                   text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;
                   border-bottom:1px solid #f3f4f6;background:#fafafa;">Captured At</td>
        <td style="padding:11px 18px;color:#111827;font-size:14px;
                   border-bottom:1px solid #f3f4f6;">{ts}</td>
      </tr>
      <tr>
        <td style="padding:11px 18px;color:#6b7280;font-size:12px;font-weight:600;
                   text-transform:uppercase;letter-spacing:.5px;background:#fafafa;">Source</td>
        <td style="padding:11px 18px;font-size:13px;">
          <span style="background:#ede9fe;color:#5b21b6;padding:3px 10px;
                       border-radius:20px;font-weight:600;">🎙 Voice Agent</span>
        </td>
      </tr>
    </table>
  </div>
  <div class="footer">
    Captured automatically by <strong>Aria – WartinLabs Voice Agent</strong>.<br>
    Please follow up within <strong>24 hours</strong> for best conversion. 🚀<br><br>
    <span style="font-size:11px">© WartinLabs · contact@wartinlabs.com</span>
  </div>
</div></div>
</body>
</html>"""


async def send_lead_email(lead: dict) -> bool:
    """Send lead notification to Shivam Sharma via Gmail SMTP App Password."""

    gmail_user     = os.getenv("GMAIL_USER", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    if not gmail_user or not gmail_password:
        logger.warning("GMAIL_USER or GMAIL_APP_PASSWORD not set in .env – email skipped")
        return False

    ts      = datetime.now().strftime("%d %B %Y at %I:%M %p")
    subject = f"🎯 New Lead: {lead.get('name', 'Unknown')} — WartinLabs Voice Agent"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"WartinLabs Voice Agent <{gmail_user}>"
    msg["To"]      = f"{TO_NAME} <{TO_EMAIL}>"
    msg.attach(MIMEText(_build_html(lead, ts), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, TO_EMAIL, msg.as_string())

        # ── Yellow terminal confirmation ──────────────────────
        Y = "\033[1;33m"
        R = "\033[0m"
        print(f"\n{Y}{'━'*55}")
        print(f"  📧  EMAIL SENT SUCCESSFULLY")
        print(f"{'━'*55}")
        print(f"  To      : {TO_NAME} <{TO_EMAIL}>")
        print(f"  Lead    : {lead.get('name', 'Unknown')}")
        print(f"  Project : {lead.get('requirements', 'N/A')[:60]}")
        print(f"  Time    : {ts}")
        print(f"{'━'*55}{R}\n")

        logger.info(f"Lead email sent to {TO_EMAIL}")
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error("Gmail auth failed – check GMAIL_USER and GMAIL_APP_PASSWORD in .env")
    except Exception as exc:
        logger.error(f"Lead email failed: {exc}")
    return False