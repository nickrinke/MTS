FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn feedparser

COPY api.py .
COPY static/ static/

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
