import asyncio
from backend.lead_capture import send_lead_email
lead = {
    "name": "Test User",
    "email": "patel at theGmail dot com",
    "phone": "(707) 839-6011",
    "requirements": "AI Voice Agent",
    "budget": "Around ten thousand to fifteen thousand dollars",
    "contact_time": "Tomorrow morning",
}

asyncio.run(send_lead_email(lead))