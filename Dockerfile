FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install ffmpeg for direct RTSP/RTSPS stream support
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /app app \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY --chown=app:app app.py .
COPY --chown=app:app stream_manager.py .
COPY --chown=app:app mqtt_probe.py .
COPY --chown=app:app index.html .
COPY --chown=app:app error.html .
# Bundle the Bambu CA certificate used for MQTT TLS verification.
COPY bambu_p2s_250626.cert .

USER app

# Expose port
EXPOSE 8000

# Default environment variables
ENV APP_TITLE="Bambu P2S Live" \
    PORT=8000 \
    LOG_LEVEL=INFO \
    MQTT_TLS_CA_CERT=/app/bambu_p2s_250626.cert \
    TZ=UTC

# Run the app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
