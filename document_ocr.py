from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

def extract_text_with_document_intelligence(pdf_path, endpoint, key):
    if not endpoint or not key:
        raise Exception("Azure Document Intelligence credentials not found.")

    # Create client
    client = DocumentAnalysisClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key)
    )

    # Read and analyse PDF
    with open(pdf_path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-document", f)
        result = poller.result()

    # Extract all text
    extracted_text = ""
    for page in result.pages:
        for line in page.lines:
            extracted_text += line.content + "\n"

    return extracted_text.strip()
