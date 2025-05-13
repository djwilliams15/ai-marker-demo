import os
import base64
import json
import time
import traceback
import logging

from flask import (
    Flask, request, render_template, make_response,
    send_from_directory, jsonify
)
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF (optional OCR fallback)
from PIL import Image
from fpdf import FPDF  # fpdf2 for PDF generation
from openai import OpenAI
from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient, AnalysisFeature
from azure.communication.email import EmailClient
import markdown
from dotenv import load_dotenv

# Flask setup
debug = os.getenv('FLASK_ENV') == 'development'
app = Flask(__name__, static_folder='static')
app.debug = debug
app.jinja_env.filters['markdown'] = markdown.markdown

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

# Rate-limiting via cookie: 15 free PDF uploads per rolling week
RATE_LIMIT_COOKIE = 'rate_limit'
MAX_UPLOADS      = 15
WINDOW           = 7 * 24 * 60 * 60   # one week in seconds

def load_rate_limit():
    """
    Reads the rate_limit cookie, decodes its JSON payload,
    resets if window expired, and returns a dict with 'count' and 'start'.
    """
    raw = request.cookies.get(RATE_LIMIT_COOKIE)
    now = time.time()
    if raw:
        try:
            payload = base64.b64decode(raw).decode()
            data = json.loads(payload)
        except Exception:
            data = {'count': 0, 'start': now}
    else:
        data = {'count': 0, 'start': now}

    # Rolling window: if more than WINDOW seconds have passed, reset
    if now - data.get('start', now) > WINDOW:
        logger.info("Rate limit window expired; resetting counter")
        data = {'count': 0, 'start': now}

    return data

def save_rate_limit(response, data):
    """
    Encodes the updated data dict into a base64 JSON cookie
    and attaches it to the given response.
    """
    payload = json.dumps(data).encode()
    b64 = base64.b64encode(payload).decode()
    response.set_cookie(
        RATE_LIMIT_COOKIE,
        b64,
        max_age=WINDOW,
        httponly=True,
        secure=not debug,    # only send over HTTPS in production
        samesite='Lax'
    )
    return response

# File upload folder
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Load environment variables
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

# Azure & OpenAI clients
fr_credential = AzureKeyCredential(AZURE_OCR_KEY)
doc_client = DocumentAnalysisClient(AZURE_OCR_ENDPOINT, fr_credential)
ai_client = OpenAI(api_key=OPENAI_API_KEY)
email_client = None
if ACS_EMAIL_CONNECTION_STRING and SMTP_SENDER_EMAIL:
    email_client = EmailClient.from_connection_string(ACS_EMAIL_CONNECTION_STRING)

# Utility: keep last N chars for prompts
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
        return send_from_directory(
            app.static_folder,
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon'
        )
    return '', 204

def extract_text(pdf_path: str) -> str:
    """Extracts styled text from PDF via Azure Form Recognizer."""
    with open(pdf_path, 'rb') as f:
        poller = doc_client.begin_analyze_document(
            'prebuilt-layout',
            document=f,
            features=[AnalysisFeature.STYLE_FONT]
        )
        result = poller.result()
    text = result.content or ''
    chars = list(text)
    for style in result.styles or []:
        tag = '**' if style.font_weight == 'bold' else ''
        for span in sorted(style.spans, key=lambda s: s.offset, reverse=True):
            start, end = span.offset, span.offset + span.length
            chars[start] = f"{tag}{chars[start]}"
            chars[end - 1] = f"{chars[end - 1]}{tag}"
    return ''.join(chars)

def save_feedback_pdf_structured(filename: str, student_name: str, parts: list[dict]) -> None:
    """Generates per-student feedback PDF with proper margins."""
    if isinstance(parts, dict):
        parts = [parts]
    out_path = os.path.join(UPLOAD_FOLDER, filename)
    pdf = FPDF(format='A4', unit='mm')

 #  Set margins, then add page, then re-apply just in case
    pdf.set_margins(20, 20, 20)           # left, top, right
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    pdf.set_left_margin(20)
    pdf.set_right_margin(20)


    # Debug: print margins and current x
    print(f"DEBUG → l_margin={pdf.l_margin}, r_margin={pdf.r_margin}, x={pdf.x}")


    # 2) Compute usable width
    width = pdf.w - pdf.l_margin - pdf.r_margin

    # 3) Header
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(width, 10, f'Feedback for: {student_name}')
    pdf.ln(5)

    # 4) Per-part feedback
    print("DEBUG parts type:", type(parts))
    print("DEBUG parts repr:", repr(parts))
    for part in parts:
        header = part.get('question', 'Unknown')
        if part.get('awarded') is not None and part.get('total') is not None:
            header += f"- {part['awarded']}/{part['total']}"
        pdf.set_font('Helvetica', 'B', 12)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(width, 8, header)

        # strip leading/trailing whitespace, normalize dashes
        feedback_text = part.get('feedback', '').strip().replace('—', '-')

        pdf.set_font('Helvetica', '', 11)
        # force left alignment
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(width, 6, feedback_text, align='L')
        pdf.ln(2)
        # Debug: show raw feedback string with whitespace
        print("DEBUG feedback_text repr:", repr(feedback_text))

    # 5) Write out the file
    pdf.output(out_path)


def save_class_summary_pdf(filename: str, summary: str, average: float) -> None:
    """Generates a class summary feedback PDF with proper margins."""
    out_path = os.path.join(UPLOAD_FOLDER, filename)
    pdf = FPDF(format='A4', unit='mm')

    # 1) Set margins before adding any page
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()

    # 2) Compute usable width
    width = pdf.w - pdf.l_margin - pdf.r_margin

    # 3) Title
    pdf.set_font('Helvetica', 'B', 16)
    pdf.multi_cell(width, 10, 'Class Summary')
    pdf.ln(3)

    # 4) Class average
    pdf.set_font('Helvetica', 'B', 14)
    pdf.multi_cell(width, 10, f'Class Average: {average}%')
    pdf.ln(5)

    # 5) Body paragraphs
    pdf.set_font('Helvetica', '', 11)
    for para in summary.strip().split('\n\n'):
        pdf.multi_cell(width, 6, para.replace('\n', ' '))
        pdf.ln(2)

    # 6) Write out the file
    pdf.output(out_path)


def send_email_with_attachments(to_email: str, subject: str, body: str, attachments: list[str]) -> None:
    """Sends results via Azure Communication Services email."""
    if not email_client:
        logger.warning('Email client not configured; skipping send.')
        return
    payload = {
        'senderAddress': SMTP_SENDER_EMAIL,
        'content': {'subject': subject, 'plainText': body},
        'recipients': {'to': [{'address': to_email}]},
        'attachments': []
    }
    for fp in attachments:
        if not os.path.exists(fp):
            logger.warning(f"Skipping missing attachment {fp}")
            continue
        with open(fp, 'rb') as f:
            data = f.read()
        payload['attachments'].append({
            'name': os.path.basename(fp),
            'contentInBase64': base64.b64encode(data).decode(),
            'contentType': 'application/pdf'
        })
    email_client.begin_send(payload).result()
    logger.info('Email sent to %s', to_email)

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/upload', methods=['POST'])
def upload_file():
    # 1) load/reset rate-limit
    data = load_rate_limit()

    # 2) enforce the limit
    if data['count'] >= MAX_UPLOADS:
        # JSON for API clients
        if 'application/json' in request.headers.get('Accept', ''):
            return (
                jsonify({
                    'error': 'rate_limit_exceeded',
                    'message': 'You have reached 15 free uploads this week.'
                }),
                429
            )
        # ← HTML fallback for browsers goes here:
        return render_template('limit_reached.html'), 429

    # 3) bump the counter
    data['count'] += 1

    logger.info("Incrementing upload count to %d/%d", data['count'], MAX_UPLOADS)

    # 4) validate required files
    if 'markscheme_file' not in request.files or not request.files.getlist('student_files'):
        return 'Mark scheme and at least one student file required.', 400

    # Gather form inputs
    level            = request.form.get('level', '').strip()
    subject          = request.form.get('subject', '').strip()
    exam_board       = request.form.get('exam_board', '').strip()
    teacher_email    = request.form.get('teacher_email', '').strip()
    additional       = request.form.get('additional_info', '').strip()
    opts             = request.form.getlist('delivery_option')
    view_on_site     = 'website' in opts
    send_email_flag  = 'email' in opts

    # Validate selections
    allowed_levels   = ['KS3', 'GCSE', 'A level']
    allowed_subjects = [
        'Physics', 'Maths', 'English', 'History', 'Geography',
        'Biology', 'Chemistry', 'Computer Science',
        'Design and Technology', 'Art and Design'
    ]
    allowed_boards   = ['AQA', 'Edexcel', 'OCR', 'WJEC']
    if level not in allowed_levels or subject not in allowed_subjects or exam_board not in allowed_boards:
        return f"Invalid selection: {level}, {subject}, {exam_board}", 400

    # Save and extract markscheme text
    ms = request.files['markscheme_file']
    ms_filename = secure_filename(ms.filename)
    ms_path = os.path.join(UPLOAD_FOLDER, ms_filename)
    ms.save(ms_path)
    markscheme_text = extract_text(ms_path)

    # Optional marking points file
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
        filename  = secure_filename(f.filename)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        f.save(file_path)
        student_text = extract_text(file_path)

    # if feedback_detail == 'parts':
        prompt = (
            f"Mark Scheme:\n{markscheme_text}\n\n"
            f"Marking Points (optional):\n{marking_points_text}\n\n"
            f"Additional Instructions:\n{additional}\n\n"
            f"Student Response:\n{student_text}\n\n"
            "For each question-part (e.g. Q4a, Q4b), output JSON objects with question, awarded, total, feedback."
        )
        try:
            resp = ai_client.chat.completions.create(
                model='gpt-4',
                messages=[
                    {'role':'system', 'content': f"Examiner for {level} {exam_board} {subject}."},
                    {'role':'user',   'content': prompt}
                ],
                temperature=0
            )
            parts = json.loads(resp.choices[0].message.content)
            if isinstance(parts, dict):
                parts = [parts]
        except Exception as e:

            parts = [{'question':'Error','awarded':None,'total':None,'feedback':f"{e}"}]

        save_feedback_pdf_structured(f"{os.path.splitext(filename)[0]}_feedback.pdf", filename, parts)
        results.append({'filename': filename, 'parts': parts, 'student_text': student_text})
        for part in parts:
            try:
                awarded = float(part.get('awarded') or 0)
                total   = float(part.get('total')   or 1)
                marks.append(round(awarded / total * 100, 1))
            except:
                continue

    # Class summary
    class_avg = round(sum(marks) / len(marks), 1) if marks else 0.0
    feedback_inputs = ["\n".join(p.get('feedback','') for p in r['parts']) for r in results]
    feedback_prompt = chunk_text("\n\n".join(feedback_inputs))
    try:
        summ = ai_client.chat.completions.create(
            model='gpt-4',
            messages=[
                {'role':'system', 'content': f"TA summarising trends for {level} {subject} {exam_board}."},
                {'role':'user',   'content': f"{feedback_prompt}\n\nPlease summarise key trends."}
            ],
            temperature=0
        )
        class_feedback = summ.choices[0].message.content
    except Exception as e:
        class_feedback = f"Error: {e}"
    save_class_summary_pdf('class_summary.pdf', class_feedback, class_avg)

    # Send email if requested
    if send_email_flag and email_client:
        attachments = [
            os.path.join(UPLOAD_FOLDER, f"{os.path.splitext(r['filename'])[0]}_feedback.pdf")
            for r in results
        ]
        attachments.append(os.path.join(UPLOAD_FOLDER, 'class_summary.pdf'))
        send_email_with_attachments(teacher_email, 'AI Marking Results',
                                    f"Class Average: {class_avg}%", attachments)

    # Return response (site view or simple link)
    if view_on_site:
        resp = make_response(render_template(
            'results.html',
            results=results,
            class_average=class_avg,
            class_feedback=class_feedback
        ))
    else:
        resp = make_response('<h2>Feedback completed.</h2><a href="/">Home</a>')

    save_rate_limit(resp, data)
    return resp

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', '8000')),
        debug=debug
    )
