import requests
import time
import os


# Load your Azure endpoint and key from environment variables
AZURE_ENDPOINT = os.getenv("AZURE_OCR_ENDPOINT")  # e.g. "https://your-resource-name.cognitiveservices.azure.com/"
AZURE_KEY = os.getenv("AZURE_OCR_KEY")

def extract_text_from_azure(file_path):
    if not AZURE_ENDPOINT or not AZURE_KEY:
        raise Exception("Azure OCR credentials not found in environment variables.")

    # API URL
    url = f"{AZURE_ENDPOINT}/vision/v3.2/read/analyze"

    # Open PDF file to send
    with open(file_path, "rb") as file_data:
        headers = {
            "Ocp-Apim-Subscription-Key": AZURE_KEY,
            "Content-Type": "application/pdf"
        }
        response = requests.post(url, headers=headers, data=file_data)

    if response.status_code != 202:
        raise Exception(f"Azure OCR submission failed: {response.status_code} - {response.text}")

    # Get operation location from response header
    operation_url = response.headers["Operation-Location"]

    # Wait for processing to complete
    for _ in range(20):
        result = requests.get(operation_url, headers={"Ocp-Apim-Subscription-Key": AZURE_KEY})
        result_json = result.json()
        if result_json.get("status") == "succeeded":
            break
        time.sleep(1)
    else:
        raise Exception("Azure OCR processing timeout")

    # Extract text from result
    lines = []
    for read_result in result_json["analyzeResult"]["readResults"]:
        for line in read_result["lines"]:
            lines.append(line["text"])

    return "\n".join(lines)
