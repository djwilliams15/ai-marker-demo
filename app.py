# app.py
import os
import re
import base64
import json
from flask import Flask, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import fitz                           # PyMuPDF (optional OCR fallback)
from PIL import Image                

from fpdf import FPDF                # fpdf2 for wrapped PDF generation

from openai import OpenAI
from azure.core.credentials import AzureKeyCredential
# top of app.py – extend the import line
from azure.ai.formrecognizer import DocumentAnalysisClient, AnalysisFeature

from azure.communication.email import EmailClient
import traceback
import markdown

from dotenv import load_dotenv
os.environ['FLASK_ENV'] = 'development'


# ─── Load env ────────────────────────────────────────────────────────────────
load_dotenv(".env.development")

AZURE_OCR_ENDPOINT          = os.getenv("AZURE_OCR_ENDPOINT", "").rstrip("/")
AZURE_OCR_KEY               = os.getenv("AZURE_OCR_KEY")
OPENAI_API_KEY              = os.getenv("OPENAI_API_KEY")
ACS_EMAIL_CONNECTION_STRING = os.getenv("ACS_EMAIL_CONNECTION_STRING")
SMTP_SENDER_EMAIL           = os.getenv("SMTP_SENDER_EMAIL")

if not AZURE_OCR_ENDPOINT or not AZURE_OCR_KEY:
    raise RuntimeError("AZURE_OCR_ENDPOINT and AZURE_OCR_KEY must both be set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY must be set")
if not ACS_EMAIL_CONNECTION_STRING or not SMTP_SENDER_EMAIL:
    print("⚠️ Email disabled: missing ACS_EMAIL_CONNECTION_STRING or SMTP_SENDER_EMAIL")

# print(f"[startup] OCR Endpoint: '{AZURE_OCR_ENDPOINT}'")
# print(f"[startup] OCR Key present: {bool(AZURE_OCR_KEY)}")
# print(f"[startup] OpenAI Key present: {bool(OPENAI_API_KEY)}")

# ─── Clients ─────────────────────────────────────────────────────────────────
fr_credential = AzureKeyCredential(AZURE_OCR_KEY)
doc_client    = DocumentAnalysisClient(AZURE_OCR_ENDPOINT, fr_credential)
ai_client     = OpenAI(api_key=OPENAI_API_KEY)

# ─── Flask setup ─────────────────────────────────────────────────────────────
app = Flask(__name__)
app.jinja_env.filters['markdown'] = markdown.markdown
UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# this will catch *any* exception anywhere in your routes
@app.errorhandler(Exception)
def handle_exception(e):
    # print to console
    traceback.print_exc()
    # render the full traceback in your browser
    return "<pre>" + traceback.format_exc() + "</pre>", 500


# ─── OCR utility ──────────────────────────────────────────────────────────────
def extract_text(pdf_path: str) -> str:
    """
    Returns a single Markdown string where:
      **bold**  ⇢ original bold text
      __underline__ ⇢ underlined text
    """
    with open(pdf_path, "rb") as f:
        poller = doc_client.begin_analyze_document(
            "prebuilt-layout",
            document=f,
            features=[AnalysisFeature.STYLE_FONT]
        )
        result = poller.result()

    chars = list(result.content)              # editable char array

    # Walk spans *right-to-left* so offsets don’t shift
    for style in (result.styles or []):
        tag_open, tag_close = "", ""
        if style.font_weight == "bold":
            tag_open, tag_close = "**", "**"
        # if style.text_decoration == "underline":
            # tag_open, tag_close = "__", "__"
        # combine if both

        for span in sorted(style.spans, key=lambda s: s.offset, reverse=True):
            start, end = span.offset, span.offset + span.length
            chars[start] = f"{tag_open}{chars[start]}"
            chars[end - 1] = f"{chars[end - 1]}{tag_close}"

    return "".join(chars)



# ─── Structured-feedback PDF ─────────────────────────────────────────────────
def save_feedback_pdf_structured(filename: str, student_name: str, parts: list[dict]) -> str:
    out_path = os.path.join(UPLOAD_FOLDER, filename)
    pdf = FPDF(format='A4', unit='mm')
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(left=20, top=20, right=20)

    usable_width = pdf.w - pdf.l_margin - pdf.r_margin  # 170 mm

    # Header
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(usable_width, 10, f'Feedback for: {student_name}', ln=True)
    pdf.ln(5)

    # Per-part feedback
    for part in parts:
        qid     = part.get('question', 'Unknown')
        awarded = part.get('awarded')
        total   = part.get('total')
        comment = part.get('feedback', '').replace('—', '-')

        header = qid
        if awarded is not None and total is not None:
            header += f" - {awarded}/{total}"
        pdf.set_font('Helvetica', 'B', 12)
        pdf.cell(usable_width, 8, header, ln=True)
        pdf.ln(1)

        pdf.set_font('Helvetica', '', 11)
        pdf.multi_cell(usable_width, 6, comment)
        pdf.ln(4)

    pdf.output(out_path)
    return out_path

# ─── Class-summary PDF ────────────────────────────────────────────────────────
def save_class_summary_pdf(filename: str, summary: str, average: float) -> str:
    out_path = os.path.join(UPLOAD_FOLDER, filename)
    pdf = FPDF(format='A4', unit='mm')
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(left=20, top=20, right=20)

    usable_width = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font('Helvetica', 'B', 16)
    pdf.cell(usable_width, 10, 'Class Summary', ln=True)
    pdf.ln(3)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(usable_width, 10, f'Class Average: {average}%', ln=True)
    pdf.ln(5)

    pdf.set_font('Helvetica', '', 11)
    line_height = 6
    for para in summary.strip().split('\n\n'):
        text = para.replace('\n', ' ')
        pdf.multi_cell(usable_width, line_height, text)
        pdf.ln(2)

    pdf.output(out_path)
    return out_path

# ─── Email utility ────────────────────────────────────────────────────────────
def send_email_with_attachments_acs(to_email: str, subject: str, body: str, attachments: list[str]):
    if not ACS_EMAIL_CONNECTION_STRING or not SMTP_SENDER_EMAIL:
        print("❌ Email not sent: ACS settings missing")
        return
    client = EmailClient.from_connection_string(ACS_EMAIL_CONNECTION_STRING)
    payload = {
        "senderAddress": SMTP_SENDER_EMAIL,
        "content":       {"subject": subject, "plainText": body},
        "recipients":    {"to": [{"address": to_email}]}
    }
    attach_list = []
    for fp in attachments:
        with open(fp, "rb") as f:
            data = f.read()
        attach_list.append({
            "name":             os.path.basename(fp),
            "contentInBase64":  base64.b64encode(data).decode(),
            "contentType":      "application/pdf"
        })
    payload["attachments"] = attach_list
    poller = client.begin_send(payload)
    _ = poller.result()
    print("✅ Email sent.")

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("upload.html")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/upload", methods=["POST"])
def upload_file():

    # Validate uploads
    if "markscheme_file" not in request.files or not request.files.getlist("student_files"):
        return "Mark scheme and at least one student file are required.", 400

    # Read form
    level           = request.form.get("level", "").strip()
    subject         = request.form.get("subject", "").strip()
    exam_board      = request.form.get("exam_board", "").strip()
    teacher_email   = request.form.get("teacher_email", "").strip()
    additional      = request.form.get("additional_info", "").strip()
    opts            = request.form.getlist("delivery_option")
    view_on_site    = "website" in opts
    send_email      = "email" in opts
    # ─── NEW: overall vs parts choice ───────────────────────────
    feedback_detail = request.form.get("feedback_detail", "overall")

    # Validate dropdowns…
    allowed_levels   = ["KS3","GCSE","A level"]
    allowed_subjects = ["Physics","Maths","English","History","Geography",
                        "Biology","Chemistry","Computer Science",
                        "Design and Technology","Art and Design"]
    allowed_boards   = ["AQA","Edexcel","OCR","WJEC"]
    if level not in allowed_levels:
        return f"Invalid level '{level}'.", 400
    if subject not in allowed_subjects:
        return f"Invalid subject '{subject}'.", 400
    if exam_board not in allowed_boards:
        return f"Invalid exam board '{exam_board}'.", 400

    # Save & OCR mark scheme
    ms      = request.files["markscheme_file"]
    ms_name = secure_filename(ms.filename)
    ms_path = os.path.join(UPLOAD_FOLDER, ms_name)
    ms.save(ms_path)
    markscheme_text = extract_text(ms_path)
    # print("[DEBUG] First 300 chars of mark-scheme OCR →")
    # print(markscheme_text[:300])

# ─── Optional Marking-points PDF ───────────────────────────────
    mp_file = request.files.get("marking_points_file")
    if mp_file and mp_file.filename:
        mp_name = secure_filename(mp_file.filename)
        mp_path = os.path.join(UPLOAD_FOLDER, mp_name)
        mp_file.save(mp_path)
        marking_points_text = extract_text(mp_path)
    else:
        marking_points_text = ""          # empty if none provided
    

    results, marks, all_fb = [], [], []

    # Process each student PDF
    for f in request.files.getlist("student_files"):
        name = secure_filename(f.filename)
        path = os.path.join(UPLOAD_FOLDER, name)
        f.save(path)
        student_text = extract_text(path)

        # ─── Branch on feedback_detail ─────────────────────────────
        if feedback_detail == "parts":
            # per-question-part path
            prompt = (
                f"Mark Scheme:\n{markscheme_text}\n\n"
                f"Marking Points (optional):\n{marking_points_text}\n\n"
                f"Additional Instructions:\n{additional}\n\n"
                f"Student Response:\n{student_text}\n\n"
                "For each question-part (e.g. 4a, 4b, i, ii), output a JSON object with:\n"
                "  - question: the part identifier,\n"
                "  - awarded: student's marks for this part,\n"
                "  - total: maximum marks for this part,\n"
                "  - feedback: a short sentence explaining where they lost marks.\n"
                "Ignore obvious spelling mistakes unless correct spelling is required for the mark.\n"
                "Wrap all objects in a top-level JSON array and output ONLY the JSON."
            )
            try:
                resp  = ai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role":"system","content":
                            f"You are an examiner for {level} ({exam_board} {subject}). Use only the mark scheme and additional instructions."},
                        {"role":"user","content": prompt}
                    ],
                    temperature=0.0
                )
                parts = json.loads(resp.choices[0].message.content)
            except Exception as e:
                parts = [{
                    "question": "Overall",
                    "awarded":  None,
                    "total":    None,
                    "feedback": f"Error generating structured feedback: {e}"
                }]
        else:
            # overall-feedback fallback
            try:
                resp    = ai_client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role":"system","content":
                            f"You are an examiner for {level} ({exam_board} {subject}). Use only the mark scheme and additional instructions."},
                        {"role":"user","content":
                            f"Mark Scheme:\n{markscheme_text}\n\n"
                            f"Marking Points (optional):\n{marking_points_text}\n\n"
                            f"Additional Instructions:\n{additional}\n\n"
                            f"Student Response:\n{student_text}\n\n"
                            "Instructions:\n"
                            "- First state 'Mark: X/Y'.\n"
                            "Ignore obvious spelling mistakes unless correct spelling is required for the mark.\n"
                            "- Then feedback under 'What went well', 'Targets for improvement', and 'Misconceptions'."}
                    ],
                    temperature=0.0
                )
                fb_text = resp.choices[0].message.content
            except Exception as e:
                fb_text = f"Error generating feedback: {e}"
            parts = [{
                "question": "Overall",
                "awarded":  None,
                "total":    None,
                "feedback": fb_text
            }]

        all_fb.append(parts)

        # Save structured-feedback PDF
        pdf_name = f"{os.path.splitext(name)[0]}_feedback.pdf"
        save_feedback_pdf_structured(pdf_name, name, parts)

        # Extract marks from parts for class average
        for p in parts:
            if p.get("awarded") is not None and p.get("total") is not None:
                marks.append(round(p["awarded"]/p["total"]*100,1))

        # Collect for site rendering
        results.append({
            "filename":     name,
            "parts":        parts,
            "student_text": student_text
        })

    # Compute class summary
    valid     = [m for m in marks if m is not None]
    class_avg = round(sum(valid)/max(len(valid),1),1)
    try:
        summ = ai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role":"system","content":
                    f"You are a TA summarising feedback trends for {level} {subject} ({exam_board})."},
                {"role":"user","content":
                    "\n\n".join([""] * len(all_fb)) + "\n\nPlease summarise key trends in performance."}
            ],
            temperature=0.0
        )
        class_feedback = summ.choices[0].message.content
    except Exception as e:
        class_feedback = f"Class summary error: {e}"



    # Save class-summary PDF
    save_class_summary_pdf("class_summary.pdf", class_feedback, class_avg)

    # Email if requested
    if send_email:
        atts = [
            os.path.join(UPLOAD_FOLDER, f"{os.path.splitext(r['filename'])[0]}_feedback.pdf")
            for r in results
        ]
        atts.append(os.path.join(UPLOAD_FOLDER, "class_summary.pdf"))
        send_email_with_attachments_acs(
            teacher_email,
            "Your AI Marking Results",
            f"Class Average: {class_avg}%\n\nPlease find attached feedback.",
            atts
        )

    # Render on site or confirm email
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

    return (
        "<h2>Feedback is being emailed to you.</h2>"
        "<p>Thank you! You’ll receive your results shortly.</p>"
        "<a href='/'>Mark another set of papers</a>"
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT",8000))
    app.run(host="0.0.0.0", port=port, debug=True)

