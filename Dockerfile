FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent SQLite database lives here (mount a volume to keep it)
RUN mkdir -p /app/data
VOLUME ["/app/data"]

CMD ["python", "-m", "bot"]
