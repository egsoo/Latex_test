FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Tectonic: single-binary LaTeX engine, downloads packages on demand into its cache
RUN curl -fsSL https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-unknown-linux-musl.tar.gz \
    | tar xz -C /usr/local/bin tectonic \
 && chmod +x /usr/local/bin/tectonic

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py warmup.py ./

# Keep the Tectonic package cache inside the image layer so it survives restarts of the same deploy
ENV TECTONIC_CACHE_DIR=/app/tex-cache

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
