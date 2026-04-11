from fastapi import FastAPI, UploadFile, File
import fitz  # PyMuPDF

app = FastAPI()

@app.post("/parse_pdf")
async def parse_pdf(file: UploadFile = File(...)):
    content = await file.read()

    doc = fitz.open(stream=content, filetype="pdf")

    text = ""
    for page in doc:
        text += page.get_text()

    return {"text": text}

