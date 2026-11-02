# Roofing Ledger — production image
# Build:  docker build -t roofing-ledger .
# Run:    see docker-compose.yml (recommended: behind Caddy for automatic HTTPS)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user; the data volume is owned by it.
RUN useradd --create-home --uid 1000 ledger
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /data && chown -R ledger:ledger /app /data

USER ledger
ENV DATA_DIR=/data HOST=0.0.0.0 PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# --proxy-headers / --forwarded-allow-ips: trust X-Forwarded-* from the reverse proxy (Caddy/nginx) so
# HTTPS is detected (Secure cookies, HSTS) and the real client IP is used for login rate limiting.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1"]
