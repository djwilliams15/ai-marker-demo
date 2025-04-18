# app.py
import os
import re
import base64
import fitz                         # PyMuPDF, if needed
from PIL import Image
from flask import Flask, render_template, request, send_from_directory
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from openai import OpenAI
from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.communication.email import EmailClient

# ─── Configuration ─────────────────────────────────────────────────────────────
AZURE_OCR_ENDPOINT = os.getenv("AZURE_OCR_ENDPOINT", "").rstrip("/")
AZURE_OCR_KEY      = os.getenv("AZURE_OCR_KEY")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
ACS_EMAIL_CONNECTION_STRING = os.getenv("ACS_EMAIL_CONNECTION_STRING")
SMTP_SENDER_EMAIL           = os.getenv("SMTP_SENDER_EMAIL")

if not AZURE_OCR_ENDPOINT or not AZURE_OCR_KEY:
    raise RuntimeError("AZURE_OCR_ENDPOINT and AZURE_OCR_KEY must both be set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY must be set")
if not ACS_EMAIL_CONNECTION_STRING or not SMTP_SENDER_EMAIL:
    print("⚠️  ACS_EMAIL_CONNECTION_STRING or SMTP_SENDER_EMAIL not set; email will be disabled.")

print(f"[startup] OCR Endpoint: '{AZURE_OCR_ENDPOINT}'")
print(f"[startup] OCR Key present: {bool(AZURE_OCR_KEY)}")
print(f"[startup] OpenAI Key present: {bool(OPENAI_API_KEY)}")

# ─── Client Initialization ────────────────────────────────────────────────────
fr_credential = AzureKeyCredential(AZURE_OCR_KEY)
doc_client   = DocumentAnalysisClient(AZURE_OCR_ENDPOINT, fr_credential)
ai_client    = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─── Utility Functions ────────────────────────────────────────────────────────
def extract_text(pdf_path: str) -> str:
    with open(pdf_path, "rb") as f:
        poller = doc_client.begin_analyze_document("prebuilt-document", f)
        result = poller.result()
    lines = [line.content for page in result.pages for line in page.lines]
    return "\n".join(lines)

def save_feedback_pdf(filename: str, student_name: str, feedback: str) -> str:
    path = os.path.join(UPLOAD_FOLDER, filename)
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, height - 50, f"Feedback for: {student_name}")
    c.setFont("Helvetica", 11)
    y = height - 80
    for line in feedback.splitlines():
        if y < 50:
            c.showPage()
            y = height - 50
        c.drawString(50, y, line)
        y -= 15
    c.save()
    return path

def save_class_summary_pdf(filename: str, summary: str, average: float) -> str:
    path = os.path.join(UPLOAD_FOLDER, filename)
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Class Summary")
    c.setFont("Helvetica", 12)
    c.drawString(50, height - 80, f"Class Average: {average}%")
    y = height - 120
    for line in summary.splitlines():
        if y < 50:
            c.showPage()
            y = height - 50
        c.drawString(50, y, line)
        y -= 15
    c.save()
    return path

def send_email_with_attachments_acs(to_email: str, subject: str, body: str, attachments: list[str]):
    if not ACS_EMAIL_CONNECTION_STRING or not SMTP_SENDER_EMAIL:
        print("❌ Email not sent: ACS settings missing")
        return
    client = EmailClient.from_connection_string(ACS_EMAIL_CONNECTION_STRING)
    payload = {
        "senderAddress": SMTP_SENDER_EMAIL,
        "content": {"subject": subject, "plainText": body},
        "recipients": {"to": [{"address": to_email}]}
    }
    attach_list = []
    for fp in attachments:
        with open(fp, "rb") as f:
            data = f.read()
        attach_list.append({
            "name": os.path.basename(fp),
            "contentInBase64": base64.b64encode(data).decode(),
            "contentType": "application/pdf"
        })
    if attach_list:
        payload["attachments"] = attach_list
    poller = client.begin_send(payload)
    result = poller.result()
    print("✅ Email sent:", result)

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("upload.html")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/upload", methods=["POST"])
def upload_file():
    if "markscheme_file" not in request.files or "student_files" not in request.files:
        return "Mark scheme and at least one student file are required.", 400

    # Read form data
    level         = request.form.get("level", "").strip()
    subject       = request.form.get("subject", "").strip()
    exam_board    = request.form.get("exam_board", "").strip()
    teacher_email = request.form.get("teacher_email", "").strip()
    additional    = request.form.get("additional_info", "").strip()
    opts          = request.form.getlist("delivery_option")
    view_on_site  = "website" in opts
    send_email    = "email" in opts

    # ─── Validate selections ───────────────────────────────────────────────
    allowed_levels   = ["KS3", "GCSE", "A level"]
    allowed_subjects = ["Physics","Maths","English","History","Geography",
                        "Biology","Chemistry","Computer Science",
                        "Design and Technology","Art and Design"]
    allowed_boards   = ["AQA","Edexcel","OCR","WJEC"]

    if level not in allowed_levels:
        return f"Invalid level '{level}' selected.", 400
    if subject not in allowed_subjects:
        return f"Invalid subject '{subject}' selected.", 400
    if exam_board not in allowed_boards:
        return f"Invalid exam board '{exam_board}' selected.", 400

    # OCR the mark scheme
    ms = request.files["markscheme_file"]
    ms_name = secure_filename(ms.filename)
    ms_path = os.path.join(UPLOAD_FOLDER, ms_name)
    ms.save(ms_path)
    markscheme_text = extract_text(ms_path)

    results = []
    marks   = []
    all_feedback = []

    # Process each student file
    for f in request.files.getlist("student_files"):
        name = secure_filename(f.filename)
        path = os.path.join(UPLOAD_FOLDER, name)
        f.save(path)
        student_text = extract_text(path)

        # Marking prompt includes level
        try:
            resp = ai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content":
                        f"You are an experienced examiner for {level} ({exam_board} {subject}). "
                        "Use ONLY the mark scheme and teacher instructions to award marks."},
                    {"role": "user", "content":
                        f"Mark Scheme:\n{markscheme_text}\n\n"
                        f"Additional Instructions:\n{additional}\n\n"
                        f"Student Response:\n{student_text}\n\n"
                        "Instructions:\n"
                        "- First state 'Mark: X/Y'.\n"
                        "- Then feedback under 'What went well', 'Targets for improvement', and 'Misconceptions'."}
                ],
                temperature=0.0
            )
            fb = resp.choices[0].message.content
        except Exception as e:
            fb = f"Error generating feedback: {e}"

        all_feedback.append(fb)
        pdf_name = f"{os.path.splitext(name)[0]}_feedback.pdf"
        save_feedback_pdf(pdf_name, name, fb)

        m = re.search(r"Mark:\s*(\d+)\s*/\s*(\d+)", fb)
        marks.append(round(int(m.group(1)) / int(m.group(2)) * 100, 1) if m else None)

        results.append({
            "filename": name,
            "student_text": student_text,
            "feedback": fb
        })

    # Class summary
    valid = [m for m in marks if m is not None]
    class_avg = round(sum(valid) / max(len(valid), 1), 1)
    try:
        summ = ai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role":"system","content":
                    f"You are a teaching assistant summarising feedback trends "
                    f"for {level}-level {subject} papers from {exam_board}."},
                {"role":"user","content":
                    "\n\n".join(all_feedback) +
                    "\n\nPlease summarise the key trends in student performance."}
            ],
            temperature=0.0
        )
        class_feedback = summ.choices[0].message.content
    except Exception as e:
        class_feedback = f"Class summary error: {e}"

    save_class_summary_pdf("class_summary.pdf", class_feedback, class_avg)

    if send_email:
        attachments = [
            os.path.join(UPLOAD_FOLDER, f"{os.path.splitext(r['filename'])[0]}_feedback.pdf")
            for r in results
        ] + [os.path.join(UPLOAD_FOLDER, "class_summary.pdf")]
        send_email_with_attachments_acs(
            teacher_email,
            "Your AI Marking Results",
            "Please find your feedback attached.",
            attachments
        )

    if view_on_site:
        return render_template(
            "results.html",
            results=results,
            markscheme_text=markscheme_text,
            subject=subject,
            exam_board=exam_board,
            level=level,
            class_average=class_avg,
            class_feedback=class_feedback
        )
    else:
        return """
            <h2>Feedback is being emailed to you.</h2>
            <p>Thank you! You’ll receive your results shortly.</p>
            <a href="/">Mark another set of papers</a>
        """

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
