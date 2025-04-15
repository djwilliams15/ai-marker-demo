import os
import base64
from dotenv import load_dotenv
from azure.communication.email import EmailClient

# Load environment variables from .env file
load_dotenv()

# Read environment variables
connection_string = os.getenv("ACS_EMAIL_CONNECTION_STRING")
sender_email = os.getenv("SMTP_SENDER_EMAIL")
# Replace with an email address you control for testing
to_email = "djwilliams15@gmail.com"

# Create EmailClient from your ACS connection string
client = EmailClient.from_connection_string(connection_string)

# Build the email payload as a dictionary
email_payload = {
    "sender": sender_email,
    "content": {
        "subject": "Test Email from ACS Email",
        "plainText": "This is a test email from your ACS Email integration."
    },
    "recipients": {
        "to": [
            {"email": to_email, "displayName": "Test Recipient"}
        ]
    }
}

# Optionally attach a file for testing (if sample.pdf exists in the project root)
try:
    with open("sample.pdf", "rb") as f:
        file_data = f.read()
    encoded_content = base64.b64encode(file_data).decode("utf-8")
    attachment = {
        "name": "sample.pdf",
        "contentBytes": encoded_content,
        "contentType": "application/pdf"
    }
    email_payload["attachments"] = [attachment]
except Exception as e:
    print("No attachment added. Error reading sample.pdf:", e)

# Send the email using the begin_send operation
try:
    poller = client.begin_send(email_payload)
    result = poller.result()
    print("✅ Test email sent successfully via ACS. Result:", result)
except Exception as e:
    print("❌ Failed to send test email via ACS:", e)
