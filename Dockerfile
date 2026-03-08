FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mts.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Create data directory for SQLite
RUN mkdir -p /data

ENTRYPOINT ["./entrypoint.sh"]
