import os
import fitz  # PyMuPDF for PDF processing
from PIL import Image
from flask import Flask, render_template, request, send_from_directory
from document_ocr import extract_text_with_document_intelligence
import openai
import re
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import base64
from azure.communication.email import EmailClient
from werkzeug.utils import secure_filename

# Load environment variables from Azure App Settings
acs_email_connection_string = os.getenv("ACS_EMAIL_CONNECTION_STRING")
smtp_sender_email = os.getenv("SMTP_SENDER_EMAIL")
azure_ocr_endpoint = os.getenv("AZURE_OCR_ENDPOINT")
azure_ocr_key = os.getenv("AZURE_OCR_KEY")
openai_api_key = os.getenv("OPENAI_API_KEY")

# at the top of your app.py, after loading env vars
print("OCR Endpoint:", azure_ocr_endpoint)
print("OCR Key present:", bool(azure_ocr_key))

# Configure OpenAI
openai.api_key = openai_api_key

# Flask app setup
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload folder exists
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Route to serve uploaded PDFs
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Email function
def send_email_with_attachments_acs(to_email, subject, body, attachments):
    if not acs_email_connection_string:
        print("ACS_EMAIL_CONNECTION_STRING not set")
        return

    client = EmailClient.from_connection_string(acs_email_connection_string)
    email_payload = {
        "senderAddress": smtp_sender_email,
        "content": {"subject": subject, "plainText": body},
        "recipients": {"to": [{"address": to_email}]}
    }

    if attachments:
        attachment_list = []
        for filepath in attachments:
            try:
                with open(filepath, "rb") as f:
                    file_data = f.read()
                encoded = base64.b64encode(file_data).decode("utf-8")
                attachment_list.append({
                    "name": os.path.basename(filepath),
                    "contentInBase64": encoded,
                    "contentType": "application/pdf"
                })
            except Exception as e:
                print(f"Failed to attach {filepath}: {e}")
        if attachment_list:
            email_payload["attachments"] = attachment_list

    try:
        poller = client.begin_send(email_payload)
        result = poller.result()
        print("✅ Email sent successfully via ACS:", result)
    except Exception as e:
        print("❌ Email send failed:", e)

# PDF Generation for feedback and class summary
def save_feedback_pdf(filename, student_name, feedback):
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

# OCR text extraction using Azure Document Intelligence
def extract_text(pdf_path):
    try:
        return extract_text_with_document_intelligence(pdf_path, azure_ocr_endpoint, azure_ocr_key)
    except Exception as e:
        print("OCR failed:", e)
        raise

# Routes
@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    # Validate files
    if 'markscheme_file' not in request.files or 'student_files' not in request.files:
        return 'Mark scheme and at least one student file are required.', 400

    subject = request.form.get('subject')
    exam_board = request.form.get('exam_board')
    teacher_email = request.form.get('teacher_email')
    additional_info = request.form.get('additional_info')

    # New: delivery options
    delivery_options = request.form.getlist('delivery_option')
    view_on_site = 'website' in delivery_options
    send_email_flag = 'email' in delivery_options
    if not (view_on_site or send_email_flag):
        return 'Please select at least one delivery option.', 400

    # Save mark scheme
    markscheme = request.files['markscheme_file']
    ms_filename = secure_filename(markscheme.filename)
    ms_path = os.path.join(UPLOAD_FOLDER, ms_filename)
    markscheme.save(ms_path)
    markscheme_text = extract_text(ms_path)

    results = []
    marks = []
    all_feedback = []

    for f in request.files.getlist('student_files'):
        name = secure_filename(f.filename)
        path = os.path.join(UPLOAD_FOLDER, name)
        f.save(path)
        student_text = extract_text(path)

        try:
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": f"You are an experienced {exam_board} {subject} examiner. Use only the mark scheme and teacher instructions to award marks."},
                    {"role": "user", "content": (
                        f"Mark Scheme:\n{markscheme_text}\n\n"
                        f"Additional Instructions:\n{additional_info}\n\n"
                        f"Student Response:\n{student_text}\n\n"
                        "Instructions:\n- First state 'Mark: X/Y'.\n"
                        "- Then feedback under 'What went well', 'Targets for improvement', 'Misconceptions'.\n"
                    )}
                ],
                temperature=0.0
            )
            fb = response.choices[0].message.content
            all_feedback.append(fb)
            pdf_name = f"{os.path.splitext(name)[0]}_feedback.pdf"
            save_feedback_pdf(pdf_name, name, fb)

            m = re.search(r"Mark:\s*(\d+)\s*/\s*(\d+)", fb)
            marks.append(round(int(m.group(1)) / int(m.group(2)) * 100) if m else None)
        except Exception as e:
            fb = f"Error generating feedback: {e}"
            marks.append(None)

        results.append({'filename': name, 'student_text': student_text, 'feedback': fb})

    # Class summary
    class_average = round(sum([m for m in marks if m is not None]) / max(len([m for m in marks if m is not None]),1),1)
    try:
        summ = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a teaching assistant. Summarise trends in feedback."},
                {"role": "user", "content": f"{chr(10).join(all_feedback)}\n\nSummarise key points."}
            ]
        )
        class_feedback = summ.choices[0].message.content
    except Exception as e:
        class_feedback = f"Class summary error: {e}"

    save_class_summary_pdf("class_summary.pdf", class_feedback, class_average)

    # Optionally send email
    if send_email_flag:
        attachments = [os.path.join(UPLOAD_FOLDER, f"{os.path.splitext(r['filename'])[0]}_feedback.pdf") for r in results]
        attachments.append(os.path.join(UPLOAD_FOLDER, "class_summary.pdf"))
        send_email_with_attachments_acs(
            teacher_email,
            "Your AI Marking Results",
            "Please find your feedback attached.",
            attachments
        )

    # Optionally render on-site
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
        # Email–only confirmation (handled via AJAX on the front end)
        return """
            <h2>Feedback is being emailed to you.</h2>
            <p>Thank you! You’ll receive your results shortly.</p>
            <a href="/">Mark another set of papers</a>
        """

if __name__ == '__main__':
    p = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=p)
