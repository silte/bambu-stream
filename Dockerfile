FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for direct RTSP/RTSPS stream support
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py .
COPY stream_manager.py .
COPY mqtt_probe.py .
COPY index.html .
COPY error.html .

# Expose port
EXPOSE 8000

# Default environment variables
ENV APP_TITLE="Bambu P2S Live" \
    PORT=8000 \
    LOG_LEVEL=INFO \
    TZ=UTC

# Run the app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
