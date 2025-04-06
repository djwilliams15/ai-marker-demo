import os
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

# Load your Azure credentials from environment variables
AZURE_FORM_ENDPOINT = os.getenv("AZURE_FORM_ENDPOINT")
AZURE_FORM_KEY = os.getenv("AZURE_FORM_KEY")

def extract_text_with_document_intelligence(file_path):
    if not AZURE_FORM_ENDPOINT or not AZURE_FORM_KEY:
        raise Exception("Azure Document Intelligence credentials not found.")

    client = DocumentAnalysisClient(
        endpoint=AZURE_FORM_ENDPOINT,
        credential=AzureKeyCredential(AZURE_FORM_KEY)
    )

    with open(file_path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-read", document=f)
        result = poller.result()

    lines = []
    for page in result.pages:
        for line in page.lines:
            lines.append(line.content)

    return "\n".join(lines)
