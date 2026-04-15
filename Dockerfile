FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py .
COPY stream_manager.py .

# Expose port
EXPOSE 8000

# Default environment variables
ENV APP_TITLE="Bambu P2S Live" \
    PORT=8000 \
    LOG_LEVEL=INFO \
    TZ=UTC

# Run the app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
