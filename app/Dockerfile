FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN MEETBOT_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput \
    && chown -R app:app /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=4)"

# 300 s timeout: /ask waits for Claude synchronously.
CMD ["gunicorn", "meetbot.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "300", "--access-logfile", "-"]
