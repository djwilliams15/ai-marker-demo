import os
import fitz  # PyMuPDF
from flask import Flask, render_template, request
from dotenv import load_dotenv
import pytesseract
from document_ocr import extract_text_with_document_intelligence
from PIL import Image
from openai import OpenAI

# 🔹 Tell pytesseract where to find the Tesseract engine
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# 🔹 Load API key
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 🔹 Flask setup
app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# 🔹 Text extraction with fallback to tesseract OCR
def extract_text(pdf_path):
    try:
        print(f"📄 Using Azure Document Intelligence on: {pdf_path}")
        return extract_text_with_document_intelligence(pdf_path)
    except Exception as e:
        print("❌ Document Intelligence failed, falling back to local OCR.")
        print("Error:", e)

        # Fallback to local OCR (PyMuPDF + pytesseract)
        text = ""
        with fitz.open(pdf_path) as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=400)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text += pytesseract.image_to_string(img)
        return text.strip()

# 🔹 Home page
@app.route('/')
def index():
    return render_template('upload.html')

# 🔹 Handle upload and marking
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'markscheme_file' not in request.files or 'student_files' not in request.files:
        return 'Mark scheme and at least one student file are required.'

    markscheme_file = request.files['markscheme_file']
    student_files = request.files.getlist('student_files')

    if markscheme_file.filename == '' or not student_files:
        return 'No file(s) selected.'

    # Save and extract mark scheme
    markscheme_path = os.path.join(app.config['UPLOAD_FOLDER'], markscheme_file.filename)
    markscheme_file.save(markscheme_path)
    markscheme_text = extract_text(markscheme_path)

    results = []

    # Loop through student PDFs
    for student_file in student_files:
        student_path = os.path.join(app.config['UPLOAD_FOLDER'], student_file.filename)
        student_file.save(student_path)
        student_text = extract_text(student_path)

        print(f"\n--- {student_file.filename} | OCR Extracted Text ---\n")
        print(student_text)
        print("\n--- End of Extracted Text ---\n")

        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an experienced AQA Physics examiner. Only use the mark scheme to award marks. Be precise, fair, and consistent."
                    },
                    {
                        "role": "user",
                        "content": f"""Mark the student's work below using only the mark scheme.

Mark Scheme:
{markscheme_text}

Student Response:
{student_text}

Instructions:
- First, state the total mark awarded (e.g. 'Mark: 5/6').
- Then provide feedback under the following headings:
  • What went well
  • Targets for improvement
  • Misconceptions or incorrect answers

Format your response like this:
Mark: __/__ 

What went well:
- ...

Targets for improvement:
- ...

Misconceptions or incorrect answers:
- ...

Be concise and use bullet points. Only use mark scheme content to justify marks.
"""
                    }
                ],
                temperature=0.0
            )

            feedback = response.choices[0].message.content

        except Exception as e:
            feedback = f"Error: {str(e)}"

        results.append({
            'filename': student_file.filename,
            'feedback': feedback,
            'student_text': student_text
        })

    # Show all results in a list
    return render_template('results.html', results=results, markscheme_text=markscheme_text)

# 🔹 Run the app
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))  # Required by Render
    print(f"Flask is starting on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
