import os
import base64
import json
from flask import Flask, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import fitz  # PyMuPDF (optional OCR fallback)
from PIL import Image

from fpdf import FPDF  # fpdf2 for PDF generation
from openai import OpenAI
from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient, AnalysisFeature
from azure.communication.email import EmailClient
import traceback
import markdown
from dotenv import load_dotenv

# Flask setup
debug = os.getenv('FLASK_ENV') == 'development'
app = Flask(__name__, static_folder='static')
app.jinja_env.filters['markdown'] = markdown.markdown
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Load environment
load_dotenv('.env.development')
AZURE_OCR_ENDPOINT = os.getenv('AZURE_OCR_ENDPOINT', '').rstrip('/')
AZURE_OCR_KEY = os.getenv('AZURE_OCR_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ACS_EMAIL_CONNECTION_STRING = os.getenv('ACS_EMAIL_CONNECTION_STRING')
SMTP_SENDER_EMAIL = os.getenv('SMTP_SENDER_EMAIL')
if not AZURE_OCR_ENDPOINT or not AZURE_OCR_KEY:
    raise RuntimeError('OCR endpoint/key not set')
if not OPENAI_API_KEY:
    raise RuntimeError('OpenAI key not set')

# Clients
fr_credential = AzureKeyCredential(AZURE_OCR_KEY)
doc_client = DocumentAnalysisClient(AZURE_OCR_ENDPOINT, fr_credential)
ai_client = OpenAI(api_key=OPENAI_API_KEY)
email_client = None
if ACS_EMAIL_CONNECTION_STRING and SMTP_SENDER_EMAIL:
    email_client = EmailClient.from_connection_string(ACS_EMAIL_CONNECTION_STRING)

# Utilities
def chunk_text(text: str, max_chars: int = 20000) -> str:
    return text if len(text) <= max_chars else text[-max_chars:]

@app.errorhandler(Exception)
def handle_exception(e):
    traceback.print_exc()
    return '<pre>' + traceback.format_exc() + '</pre>', 500

@app.route('/favicon.ico')
def favicon():
    path = os.path.join(app.static_folder, 'favicon.ico')
    if os.path.exists(path):
        return send_from_directory(app.static_folder, 'favicon.ico', mimetype='image/vnd.microsoft.icon')
    return '', 204

# OCR extraction
def extract_text(pdf_path: str) -> str:
    with open(pdf_path, 'rb') as f:
        poller = doc_client.begin_analyze_document(
            'prebuilt-layout', document=f, features=[AnalysisFeature.STYLE_FONT]
        )
        result = poller.result()
    text = result.content or ''
    chars = list(text)
    for style in result.styles or []:
        tag_open = '**' if style.font_weight == 'bold' else ''
        tag_close = '**' if style.font_weight == 'bold' else ''
        for span in sorted(style.spans, key=lambda s: s.offset, reverse=True):
            start, end = span.offset, span.offset + span.length
            chars[start] = f"{tag_open}{chars[start]}"
            chars[end-1] = f"{chars[end-1]}{tag_close}"
    return ''.join(chars)

# PDF generation
def save_feedback_pdf_structured(filename: str, student_name: str, parts: list[dict]) -> None:
    out_path = os.path.join(UPLOAD_FOLDER, filename)
    pdf = FPDF(format='A4', unit='mm')
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)
    width = pdf.w - pdf.l_margin - pdf.r_margin
    # Header
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(width, 10, f'Feedback for: {student_name}')
    pdf.ln(5)
    # Per-part feedback
    for part in parts:
        pdf.set_x(pdf.l_margin)
        header = part.get('question', 'Unknown')
        if part.get('awarded') is not None and part.get('total') is not None:
            header += f" - {part['awarded']}/{part['total']}"
        pdf.set_font('Helvetica', 'B', 12)
        pdf.multi_cell(width, 8, header)
        pdf.set_x(pdf.l_margin)
        pdf.set_font('Helvetica', '', 11)
        pdf.multi_cell(width, 6, part.get('feedback', '').replace('—', '-'))
        pdf.ln(2)
    pdf.output(out_path)

def save_class_summary_pdf(filename: str, summary: str, average: float) -> None:
    out_path = os.path.join(UPLOAD_FOLDER, filename)
    pdf = FPDF(format='A4', unit='mm')
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)
    width = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_font('Helvetica', 'B', 16)
    pdf.cell(width, 10, 'Class Summary', ln=True)
    pdf.ln(3)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(width, 10, f'Class Average: {average}%', ln=True)
    pdf.ln(5)
    pdf.set_font('Helvetica', '', 11)
    for para in summary.strip().split('\n\n'):
        pdf.multi_cell(width, 6, para.replace('\n', ' '))
        pdf.ln(2)
    pdf.output(out_path)

# Email sending
def send_email_with_attachments(to_email: str, subject: str, body: str, attachments: list[str]) -> None:
    if not email_client:
        print('⚠️ Email client not configured')
        return
    payload = {
        'senderAddress': SMTP_SENDER_EMAIL,
        'content': {'subject': subject, 'plainText': body},
        'recipients': {'to': [{'address': to_email}]},
        'attachments': []
    }
    for fp in attachments:
        if not os.path.exists(fp):
            print(f"⚠️ Skipping missing attachment {fp}")
            continue
        with open(fp, 'rb') as f:
            data = f.read()
        payload['attachments'].append({
            'name': os.path.basename(fp),
            'contentInBase64': base64.b64encode(data).decode(),
            'contentType': 'application/pdf'
        })
    email_client.begin_send(payload).result()
    print('✅ Email sent.')

# Routes
@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'markscheme_file' not in request.files or not request.files.getlist('student_files'):
        return 'Mark scheme and at least one student file required.', 400

    level = request.form.get('level', '').strip()
    subject = request.form.get('subject', '').strip()
    exam_board = request.form.get('exam_board', '').strip()
    teacher_email = request.form.get('teacher_email', '').strip()
    additional = request.form.get('additional_info', '').strip()
    opts = request.form.getlist('delivery_option')
    view_on_site = 'website' in opts
    send_email_flag = 'email' in opts
    feedback_detail = request.form.get('feedback_detail', 'overall')

    allowed_levels = ['KS3', 'GCSE', 'A level']
    allowed_subjects = [
        'Physics', 'Maths', 'English', 'History', 'Geography',
        'Biology', 'Chemistry', 'Computer Science',
        'Design and Technology', 'Art and Design'
    ]
    allowed_boards = ['AQA', 'Edexcel', 'OCR', 'WJEC']
    if level not in allowed_levels or subject not in allowed_subjects or exam_board not in allowed_boards:
        return f"Invalid selection: {level}, {subject}, {exam_board}", 400

    # Process markscheme
    ms = request.files['markscheme_file']
    ms_filename = secure_filename(ms.filename)
    ms_path = os.path.join(UPLOAD_FOLDER, ms_filename)
    ms.save(ms_path)
    markscheme_text = extract_text(ms_path)

    # Optional marking points
    mp_file = request.files.get('marking_points_file')
    if mp_file and mp_file.filename:
        mp_filename = secure_filename(mp_file.filename)
        mp_path = os.path.join(UPLOAD_FOLDER, mp_filename)
        mp_file.save(mp_path)
        marking_points_text = extract_text(mp_path)
    else:
        marking_points_text = ''

    results, marks = [], []
    for f in request.files.getlist('student_files'):
        filename = secure_filename(f.filename)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        f.save(file_path)
        student_text = extract_text(file_path)

        if feedback_detail == 'parts':
            prompt = (
                f"Mark Scheme:\n{markscheme_text}\n\n"
                f"Marking Points (optional):\n{marking_points_text}\n\n"
                f"Additional Instructions:\n{additional}\n\n"
                f"Student Response:\n{student_text}\n\n"
                "For each question-part (e.g. Q4a, Q4b, Q4c), output JSON objects with question, awarded, total, feedback."
            )
            try:
                resp = ai_client.chat.completions.create(
                    model='gpt-4', messages=[
                        {'role':'system', 'content': f"Examiner for {level} {exam_board} {subject}."},
                        {'role':'user', 'content': prompt}
                    ], temperature=0
                )
                parts = json.loads(resp.choices[0].message.content)
            except Exception as e:
                parts = [{'question':'Overall','awarded':None,'total':None,'feedback':f"Error: {e}"}]
        else:
            overall_message = (
                f"Mark Scheme:\n{markscheme_text}\n\n"
                f"Marking Points (optional):\n{marking_points_text}\n\n"
                f"Additional Instructions:\n{additional}\n\n"
                f"Student Response:\n{student_text}\n\n"
                "First state Mark: X/Y, then What went well, Targets, Misconceptions."
            )
            try:
                resp = ai_client.chat.completions.create(
                    model='gpt-4', messages=[
                        {'role':'system', 'content': f"Examiner for {level} {exam_board} {subject}."},
                        {'role':'user', 'content': overall_message}
                    ], temperature=0
                )
                fb_text = resp.choices[0].message.content
                parts = [{'question':'Overall','awarded':None,'total':None,'feedback':fb_text}]
            except Exception as e:
                parts = [{'question':'Overall','awarded':None,'total':None,'feedback':f"Error: {e}"}]

        save_feedback_pdf_structured(f"{os.path.splitext(filename)[0]}_feedback.pdf", filename, parts)
        results.append({'filename': filename, 'parts': parts, 'student_text': student_text})
        for part in parts:
            awarded = part.get('awarded')
            total = part.get('total')
            try:
                awarded_val = float(awarded)
                total_val = float(total)
                marks.append(round(awarded_val / total_val * 100, 1))
            except:
                continue

    # Class summary
    class_avg = round(sum(marks)/len(marks), 1) if marks else 0.0
    feedback_inputs = ["\n".join(p.get('feedback','') for p in r['parts']) for r in results]
    feedback_prompt = chunk_text("\n\n".join(feedback_inputs))
    try:
        summ = ai_client.chat.completions.create(
            model='gpt-4', messages=[
                {'role':'system', 'content': f"TA summarising trends for {level} {subject} {exam_board}."},
                {'role':'user', 'content': f"{feedback_prompt}\n\nPlease summarise key trends."}
            ], temperature=0
        )
        class_feedback = summ.choices[0].message.content
    except Exception as e:
        class_feedback = f"Error: {e}"
    save_class_summary_pdf('class_summary.pdf', class_feedback, class_avg)

    if send_email_flag and email_client:
        attachments = [os.path.join(UPLOAD_FOLDER, f"{os.path.splitext(r['filename'])[0]}_feedback.pdf") for r in results]
        attachments.append(os.path.join(UPLOAD_FOLDER, 'class_summary.pdf'))
        send_email_with_attachments(teacher_email, 'AI Marking Results', f"Class Average: {class_avg}%", attachments)

    if view_on_site:
        return render_template('results.html', results=results, class_average=class_avg, class_feedback=class_feedback)
    return '<h2>Feedback completed.</h2><a href="/">Home</a>'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8000')), debug=debug)
