FROM python:3.10-slim

WORKDIR /app

# Install system deps for Pillow
RUN apt-get update && \
    apt-get install -y --no-install-recommends libjpeg62-turbo-dev zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY enhanced_steam_bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY enhanced_steam_bot/ ./enhanced_steam_bot/

# Persist data outside the container
VOLUME /app/data

# Health check endpoint (port comes from PORT env var, default 3000)
ARG PORT=3000
EXPOSE ${PORT}
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",3000)}/health')" || exit 1

ENTRYPOINT ["python", "-m", "enhanced_steam_bot"]
CMD ["run"]
