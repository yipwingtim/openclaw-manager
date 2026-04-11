FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pdf_service.py /app/

#RUN pip install --no-cache-dir fastapi uvicorn pymupdf python-multipart

CMD ["uvicorn", "pdf_service:app", "--host", "0.0.0.0", "--port", "8001"]

