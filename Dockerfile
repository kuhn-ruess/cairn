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

# gthread workers give each worker a thread pool, so a request waiting on the
# DB no longer blocks the others (the API is I/O-bound on MongoDB). 3 workers x
# 8 threads = 24 concurrent requests. Timeout cut from 600s to 120s so a stuck
# request fails fast instead of hanging a worker.
CMD ["gunicorn", "--bind", "0.0.0.0:5004", "--workers", "3", \
     "--worker-class", "gthread", "--threads", "8", "--timeout", "120", \
     "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info", \
     "--capture-output", "wsgi:app"]
