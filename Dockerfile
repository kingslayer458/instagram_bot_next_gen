FROM python:3.10-slim

WORKDIR /app

# Install system deps for Pillow
RUN apt-get update && \
    apt-get install -y --no-install-recommends libjpeg62-turbo-dev zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY enhanced_steam_bot/ ./enhanced_steam_bot/

# Persist data outside the container
VOLUME /app/data

# Health check endpoint
EXPOSE 4000
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health')" || exit 1

ENTRYPOINT ["python", "-m", "enhanced_steam_bot"]
CMD ["run"]
