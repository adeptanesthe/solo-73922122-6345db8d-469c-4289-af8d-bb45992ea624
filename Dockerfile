FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ABX_DATA_DIR=/data

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and the bundled audible sample pair.
COPY app ./app
COPY samples ./samples
COPY wsgi.py ./wsgi.py

# Ensure sample WAVs exist (regenerate deterministically if missing).
RUN if [ ! -s samples/sample_A.wav ] || [ ! -s samples/sample_B.wav ]; then \
        python samples/make_samples.py; \
    fi

RUN mkdir -p /data && useradd --create-home --uid 10001 abx \
    && chown -R abx:abx /app /data
USER abx

EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"

# Single worker + threads: question state lives on a Docker volume and the
# app uses atomic file writes / an in-process lock for answer submission.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
