FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 5004

CMD ["gunicorn", "--bind", "0.0.0.0:5004", "--workers", "3", "--timeout", "600", \
     "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info", \
     "--capture-output", "wsgi:app"]
