import os
import fitz  # PyMuPDF for PDF processing
from PIL import Image
from flask import Flask, render_template, request, send_from_directory
import openai import openAI
import re
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import base64
from azure.communication.email import EmailClient
from werkzeug.utils import secure_filename

# Form Recognizer SDK
from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient

# Load environment variables
acs_email_connection_string = os.getenv("ACS_EMAIL_CONNECTION_STRING")
smtp_sender_email           = os.getenv("SMTP_SENDER_EMAIL")
azure_ocr_endpoint          = os.getenv("AZURE_OCR_ENDPOINT", "").rstrip("/")
azure_ocr_key               = os.getenv("AZURE_OCR_KEY")
client                      = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Startup logging
print(f"[startup] OCR Endpoint: '{azure_ocr_endpoint}'")
print(f"[startup] OCR Key present: {bool(azure_ocr_key)}")
print(f"[startup] OpenAI Key present: {bool(openai.api_key)}")

# Init clients
doc_client = DocumentAnalysisClient(azure_ocr_endpoint, AzureKeyCredential(azure_ocr_key))

# Flask setup
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

def extract_text(pdf_path):
    """Use Form Recognizer to extract all lines of text from a PDF."""
    try:
        with open(pdf_path, "rb") as f:
            poller = doc_client.begin_analyze_document("prebuilt-document", f)
            result = poller.result()
        lines = [line.content for page in result.pages for line in page.lines]
        return "\n".join(lines)
    except Exception as e:
        print("OCR failed:", e)
        raise

def save_feedback_pdf(filename, student_name, feedback):
    """Render a single‑student feedback PDF."""
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

def save_class_summary_pdf(filename, class_feedback, class_average):
    """Render the class‑summary PDF."""
    path = os.path.join(UPLOAD_FOLDER, filename)
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Class Summary")
    c.setFont("Helvetica", 12)
    y = height - 80
    c.drawString(50, y, f"Class Average: {class_average}%")
    y -= 30
    for line in class_feedback.splitlines():
        if y < 50:
            c.showPage()
            y = height - 50
        c.drawString(50, y, line)
        y -= 15
    c.save()
    return path

def send_email_with_attachments_acs(to_email, subject, body, attachments):
    """Send PDFs via Azure Communication Services Email."""
    if not acs_email_connection_string:
        print("❌ ACS_EMAIL_CONNECTION_STRING not set")
        return
    client = EmailClient.from_connection_string(acs_email_connection_string)
    payload = {
        "senderAddress": smtp_sender_email,
        "content": {"subject": subject, "plainText": body},
        "recipients": {"to": [{"address": to_email}]}
    }
    if attachments:
        attach_list = []
        for fp in attachments:
            try:
                with open(fp, "rb") as f:
                    data = f.read()
                b64 = base64.b64encode(data).decode()
                attach_list.append({
                    "name": os.path.basename(fp),
                    "contentInBase64": b64,
                    "contentType": "application/pdf"
                })
            except Exception as e:
                print(f"Failed to attach {fp}: {e}")
        if attach_list:
            payload["attachments"] = attach_list
    try:
        poller = client.begin_send(payload)
        res = poller.result()
        print("✅ Email sent:", res)
    except Exception as e:
        print("❌ Email failed:", e)

@app.route('/upload', methods=['POST'])
def upload_file():
    # Validate inputs
    if 'markscheme_file' not in request.files or 'student_files' not in request.files:
        return 'Mark scheme and at least one student file are required.', 400
    # Form fields
    subject         = request.form.get('subject')
    exam_board      = request.form.get('exam_board')
    teacher_email   = request.form.get('teacher_email')
    additional_info = request.form.get('additional_info', '')
    delivery_opts   = request.form.getlist('delivery_option')
    view_on_site    = 'website' in delivery_opts
    send_email_flag = 'email'   in delivery_opts
    if not (view_on_site or send_email_flag):
        return 'Please select at least one delivery option.', 400

    # OCR the mark scheme
    ms_file   = request.files['markscheme_file']
    ms_name   = secure_filename(ms_file.filename)
    ms_path   = os.path.join(UPLOAD_FOLDER, ms_name)
    ms_file.save(ms_path)
    markscheme_text = extract_text(ms_path)

    results      = []
    marks        = []
    all_feedback = []

    # Loop through each student PDF
    for f in request.files.getlist('student_files'):
        name = secure_filename(f.filename)
        path = os.path.join(UPLOAD_FOLDER, name)
        f.save(path)
        student_text = extract_text(path)

        # Call OpenAI to mark & give feedback
        try:
            resp = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role":"system", "content":
                        f"You are an experienced {exam_board} {subject} examiner. Use only the mark scheme and teacher instructions to award marks."},
                    {"role":"user", "content":
                        f"Mark Scheme:\n{markscheme_text}\n\n"
                        f"Additional Instructions:\n{additional_info}\n\n"
                        f"Student Response:\n{student_text}\n\n"
                        "Instructions:\n"
                        "- First state 'Mark: X/Y'.\n"
                        "- Then feedback under 'What went well', 'Targets for improvement', 'Misconceptions'."
                    }
                ],
                temperature=0.0
            )
            fb = resp.choices[0].message.content
        except Exception as e:
            fb = f"Error generating feedback: {e}"

        all_feedback.append(fb)
        # Save individual feedback PDF
        pdf_name = f"{os.path.splitext(name)[0]}_feedback.pdf"
        save_feedback_pdf(pdf_name, name, fb)

        # Parse mark percentage
        m = re.search(r"Mark:\s*(\d+)\s*/\s*(\d+)", fb)
        marks.append(round(int(m.group(1))/int(m.group(2))*100) if m else None)

        results.append({
            'filename': name,
            'student_text': student_text,
            'feedback': fb
        })

    # Class summary
    valid_marks = [m for m in marks if m is not None]
    class_average = round(sum(valid_marks)/max(len(valid_marks),1), 1)
    try:
        summ = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role":"system","content":"You are a teaching assistant. Summarise trends in feedback."},
                {"role":"user","content": "\n\n".join(all_feedback) + "\n\nSummarise key points."}
            ]
        )
        class_feedback = client.chat.completions.create(
    except Exception as e:
        class_feedback = f"Class summary error: {e}"

    save_class_summary_pdf("class_summary.pdf", class_feedback, class_average)

    # Send email if requested
    if send_email_flag:
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

    # Render results on site if requested
    if view_on_site:
        return render_template(
            'results.html',
            results=results,
            markscheme_text=markscheme_text,
            subject=subject,
            exam_board=exam_board,
            class_average=class_average,
            class_feedback=class_feedback
        )
    else:
        return """
            <h2>Feedback is being emailed to you.</h2>
            <p>Thank you! You’ll receive your results shortly.</p>
            <a href="/">Mark another set of papers</a>
        """

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
