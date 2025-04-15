import os
import fitz  # PyMuPDF for PDF processing
import pytesseract
from PIL import Image
from flask import Flask, render_template, request
from dotenv import load_dotenv
from document_ocr import extract_text_with_document_intelligence
import openai
import re
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import base64
from azure.communication.email import EmailClient
from werkzeug.utils import secure_filename

# Load environment variables from .env file
load_dotenv()

# 🔍 DEBUG: Print key environment variables to verify they’re loading (remove in production)
print("🔍 OPENAI_API_KEY:", os.getenv("OPENAI_API_KEY"))
print("🔍 ACS_EMAIL_CONNECTION_STRING:", os.getenv("ACS_EMAIL_CONNECTION_STRING"))
print("🔍 SMTP_SENDER_EMAIL:", os.getenv("SMTP_SENDER_EMAIL"))

# Set up OpenAI using your key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Configure Tesseract (update the path if necessary)
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Initialize Flask app and set the upload folder
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

###############################################
# Email Sending via Azure Communication Services Email (Dictionary Payload)
###############################################
def send_email_with_attachments_acs(to_email, subject, body, attachments):
    connection_string = os.getenv("ACS_EMAIL_CONNECTION_STRING")
    if not connection_string:
        print("ACS_EMAIL_CONNECTION_STRING not set in environment")
        return

    client = EmailClient.from_connection_string(connection_string)

    email_payload = {
        "senderAddress": os.getenv("SMTP_SENDER_EMAIL"),
        "content": {
            "subject": subject,
            "plainText": body
        },
        "recipients": {
            "to": [
                {"address": to_email}
            ]
        }
    }

    if attachments:
        attachment_list = []
        for filepath in attachments:
            try:
                with open(filepath, "rb") as f:
                    file_data = f.read()
                encoded_content = base64.b64encode(file_data).decode("utf-8")
                attachment_list.append({
                    "name": os.path.basename(filepath),
                    "contentInBase64": encoded_content,
                    "contentType": "application/pdf"
                })
            except Exception as e:
                print(f"Failed to attach file {filepath}: {e}")
        if attachment_list:
            email_payload["attachments"] = attachment_list

    try:
        poller = client.begin_send(email_payload)
        result = poller.result()
        print("✅ Email sent successfully via ACS. Result:", result)
    except Exception as e:
        print("❌ Failed to send email via ACS:", e)

###############################################
# PDF Generation Function for Individual Feedback using ReportLab
###############################################
def save_feedback_pdf(filename, student_name, feedback):
    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    c = canvas.Canvas(pdf_path, pagesize=A4)
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
    return pdf_path

###############################################
# PDF Generation Function for Class Summary using ReportLab
###############################################
def save_class_summary_pdf(filename, class_feedback, class_average):
    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    c = canvas.Canvas(pdf_path, pagesize=A4)
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
    return pdf_path

###############################################
# OCR Function with Fallback Strategy
###############################################
def extract_text(pdf_path):
    try:
        print(f"Using Azure Document Intelligence on: {pdf_path}")
        return extract_text_with_document_intelligence(pdf_path)
    except Exception as e:
        print("Document Intelligence failed, falling back to local OCR.")
        print("Error:", e)
        text = ""
        with fitz.open(pdf_path) as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=400)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text += pytesseract.image_to_string(img)
        return text.strip()

###############################################
# Flask Routes
###############################################
@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'markscheme_file' not in request.files or 'student_files' not in request.files:
        return 'Mark scheme and at least one student file are required.'

    subject = request.form.get('subject')
    teacher_email = request.form.get('teacher_email')
    exam_board = request.form.get('exam_board')
    additional_info = request.form.get('additional_info')
    markscheme_file = request.files['markscheme_file']
    student_files = request.files.getlist('student_files')

    if markscheme_file.filename == '' or not student_files:
        return 'No file(s) selected.'

    markscheme_filename = secure_filename(markscheme_file.filename)
    markscheme_path = os.path.join(app.config['UPLOAD_FOLDER'], markscheme_filename)
    markscheme_file.save(markscheme_path)
    markscheme_text = extract_text(markscheme_path)

    results = []
    marks = []
    all_feedback = []

    for student_file in student_files:
        student_filename = secure_filename(student_file.filename)
        student_path = os.path.join(app.config['UPLOAD_FOLDER'], student_filename)
        student_file.save(student_path)
        student_text = extract_text(student_path)

        try:
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {
                        "role": "system",
                        "content": f"You are an experienced {exam_board} {subject} examiner. Only use the mark scheme to award marks. Be precise, fair, and consistent."
                    },
                    {
                        "role": "user",
                        "content": (
                            "Mark the student's work below using only the mark scheme.\n\n"
                            f"Mark Scheme:\n{markscheme_text}\n\n"
                            f"Additional Instructions:\n{additional_info}\n\n"
                            f"Student Response:\n{student_text}\n\n"
                            "Instructions:\n"
                            "- First, state the total mark awarded (e.g. 'Mark: 5/6').\n"
                            "- Then provide feedback under the following headings:\n"
                            "  • What went well\n"
                            "  • Targets for improvement\n"
                            "  • Misconceptions or incorrect answers\n\n"
                            "Format your response like this:\n"
                            "Mark: __/__ \n\n"
                            "What went well:\n- ...\n\n"
                            "Targets for improvement:\n- ...\n\n"
                            "Misconceptions or incorrect answers:\n- ...\n\n"
                            "Be concise and use bullet points. Only use mark scheme content to justify marks."
                        )
                    }
                ],
                temperature=0.0
            )

            feedback = response.choices[0].message.content
            all_feedback.append(feedback)
            pdf_filename = f"{os.path.splitext(student_file.filename)[0]}_feedback.pdf"
            pdf_path = save_feedback_pdf(pdf_filename, student_file.filename, feedback)

            mark_match = re.search(r"Mark:\s*(\d+)\s*/\s*(\d+)", feedback)
            if mark_match:
                score = int(mark_match.group(1))
                out_of = int(mark_match.group(2))
                percent = round((score / out_of) * 100)
                marks.append(percent)
            else:
                marks.append(None)

        except Exception as e:
            feedback = f"Error: {str(e)}"
            marks.append(None)

        results.append({
            'filename': student_file.filename,
            'feedback': feedback,
            'student_text': student_text
        })

    class_feedback = "No class summary generated."
    if all_feedback:
        try:
            summary_response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a teaching assistant. Summarise trends in student feedback."},
                    {"role": "user", "content": f"Here are all the student feedbacks:\n\n{chr(10).join(all_feedback)}\n\nSummarise:\n- Common misconceptions\n- General strengths\n- Targets for improvement\nKeep it short, clear, and bullet-pointed."}
                ]
            )
            class_feedback = summary_response.choices[0].message.content
        except Exception as e:
            class_feedback = f"Error generating class summary: {str(e)}"

    valid_marks = [m for m in marks if m is not None]
    class_average = round(sum(valid_marks) / len(valid_marks), 1) if valid_marks else "N/A"

    class_summary_pdf = save_class_summary_pdf("class_summary.pdf", class_feedback, class_average)

    feedback_pdfs = [os.path.join(UPLOAD_FOLDER, f"{os.path.splitext(r['filename'])[0]}_feedback.pdf") for r in results]
    feedback_pdfs.append(class_summary_pdf)

    send_email_with_attachments_acs(
        teacher_email,
        "Student Feedback PDFs",
        "Please find attached the feedback PDFs for the recent submissions, including the class summary.",
        feedback_pdfs
    )

    return render_template(
        'results.html',
        results=results,
        markscheme_text=markscheme_text,
        subject=subject,
        exam_board=exam_board,
        class_average=class_average,
        class_feedback=class_feedback
    )

###############################################
# Run the Flask Application
###############################################
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    print(f"Flask is starting on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
