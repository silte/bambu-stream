"""
Bambu P2S Live Stream App
Simple web app serving printer camera feed via HTTPS behind Cloudflare.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from stream_manager import StreamConfig, StreamProxy

# ============================================================================
# LOGGING SETUP
# ============================================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bambu_stream_app")

# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = FastAPI(title="Bambu P2S Live")

# Configuration
APP_TITLE = os.getenv("APP_TITLE", "Bambu P2S Live")
PORT = int(os.getenv("PORT", 8000))
TZ = os.getenv("TZ", "UTC")

config = StreamConfig()
stream_proxy = StreamProxy(config)

# Startup tracking
app_start_time = time.time()


def get_uptime_seconds() -> int:
    """Get app uptime in seconds."""
    return int(time.time() - app_start_time)


# ============================================================================
# ROUTES
# ============================================================================


@app.on_event("startup")
async def startup_event():
    """Log startup."""
    logger.info("=" * 60)
    logger.info("Bambu P2S Stream App starting")
    logger.info("APP_TITLE=%s", APP_TITLE)
    logger.info("PORT=%s", PORT)
    logger.info("LOG_LEVEL=%s", LOG_LEVEL)
    config.log_config()
    logger.info("=" * 60)


@app.get("/health", response_class=JSONResponse)
async def health():
    """
    Health check endpoint.
    Returns JSON with app status and configuration state.
    """
    uptime = get_uptime_seconds()
    upstream_ok = await stream_proxy.is_upstream_available()

    return JSONResponse(
        {
            "ok": True,
            "stream_configured": config.is_configured(),
            "upstream_connected": upstream_ok,
            "app_version": "0.1.0",
            "uptime_seconds": uptime,
            "timestamp": datetime.utcnow().isoformat(),
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    """
    Main page: simple HTML5 player with minimal JS.
    """
    if not config.is_configured():
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Bambu P2S Live</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                       background: #1a1a1a; color: #eee; margin: 0; padding: 20px; }
                .container { max-width: 800px; margin: 0 auto; }
                .error { background: #d32f2f; padding: 20px; border-radius: 8px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Bambu P2S Live</h1>
                <div class="error">
                    <strong>Stream Not Configured</strong>
                    <p>STREAM_URL environment variable is not set.</p>
                </div>
            </div>
        </body>
        </html>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{APP_TITLE}</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #1a1a1a;
                color: #e0e0e0;
                padding: 20px;
            }}
            .container {{
                max-width: 960px;
                margin: 0 auto;
            }}
            h1 {{
                font-size: 28px;
                margin-bottom: 20px;
            }}
            .player-section {{
                background: #242424;
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
                border: 1px solid #333;
            }}
            .stream-container {{
                background: #000;
                border-radius: 4px;
                overflow: hidden;
                margin-bottom: 15px;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 300px;
                position: relative;
            }}
            .stream-image {{
                max-width: 100%;
                max-height: 600px;
                object-fit: contain;
            }}
            .status {{
                display: flex;
                align-items: center;
                gap: 10px;
                padding: 12px;
                background: #2c2c2c;
                border-radius: 4px;
                margin-bottom: 15px;
                font-size: 14px;
            }}
            .status-dot {{
                width: 10px;
                height: 10px;
                border-radius: 50%;
                background: #999;
            }}
            .status-dot.connected {{
                background: #4caf50;
                box-shadow: 0 0 8px rgba(76, 175, 80, 0.6);
            }}
            .status-dot.error {{
                background: #f44336;
                box-shadow: 0 0 8px rgba(244, 67, 54, 0.6);
            }}
            .controls {{
                display: flex;
                gap: 10px;
            }}
            button {{
                padding: 10px 20px;
                background: #2196f3;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 14px;
                transition: background 0.2s;
            }}
            button:hover {{
                background: #1976d2;
            }}
            button:disabled {{
                background: #666;
                cursor: not-allowed;
            }}
            .error-text {{
                color: #f44336;
                font-size: 13px;
                margin-top: 10px;
                display: none;
            }}
            .error-text.show {{
                display: block;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>{APP_TITLE}</h1>

            <div class="player-section">
                <div class="status">
                    <div class="status-dot" id="statusDot"></div>
                    <span id="statusText">Loading...</span>
                </div>

                <div class="stream-container">
                    <img
                        id="streamImage"
                        class="stream-image"
                        src="/stream"
                        alt="Live stream"
                    >
                </div>

                <div class="controls">
                    <button onclick="reconnectStream()">Reconnect</button>
                </div>

                <div class="error-text" id="errorText"></div>
            </div>
        </div>

        <script>
            const statusDot = document.getElementById('statusDot');
            const statusText = document.getElementById('statusText');
            const errorText = document.getElementById('errorText');
            const streamImage = document.getElementById('streamImage');

            function updateStatus(connected, message) {{
                if (connected) {{
                    statusDot.className = 'status-dot connected';
                    statusText.textContent = 'Connected';
                    errorText.classList.remove('show');
                }} else {{
                    statusDot.className = 'status-dot error';
                    statusText.textContent = 'Disconnected';
                    if (message) {{
                        errorText.textContent = message;
                        errorText.classList.add('show');
                    }}
                }}
            }}

            streamImage.addEventListener('load', function() {{
                updateStatus(true, '');
            }});

            streamImage.addEventListener('error', function() {{
                updateStatus(false, 'Failed to load stream. Click Reconnect to retry.');
                // Auto-retry after 3 seconds
                setTimeout(function() {{
                    streamImage.src = '/stream?retry=' + Date.now();
                }}, 3000);
            }});

            function reconnectStream() {{
                updateStatus(true, 'Reconnecting...');
                // Force reload by adding timestamp query param
                streamImage.src = '/stream?t=' + Date.now();
            }}

            // Initial status
            updateStatus(true, '');
        </script>
    </body>
    </html>
    """


@app.get("/stream")
async def stream():
    """
    Stream endpoint: Proxy live stream from configured STREAM_URL.

    Phase 1: Each viewer opens separate connection to printer.
    Phase 2: Will use shared upstream connection with fan-out.
    """
    if not config.is_configured():
        logger.warning("Stream endpoint called but STREAM_URL not configured")
        return JSONResponse(
            {"error": "Stream not configured", "details": "STREAM_URL environment variable is missing"},
            status_code=500,
        )

    logger.info("Stream request received from client")

    async def stream_generator():
        """Generator that proxies stream from upstream."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info("Connecting to upstream stream: %s", stream_proxy.config._mask_url(config.stream_url))
                async with client.stream("GET", config.stream_url) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "Upstream returned status %d for %s",
                            response.status_code,
                            stream_proxy.config._mask_url(config.stream_url),
                        )
                        yield b"HTTP upstream error"
                        return

                    logger.info("Upstream connection established, streaming to client")
                    chunk_count = 0
                    try:
                        async for chunk in response.aiter_bytes(chunk_size=4096):
                            if chunk:
                                yield chunk
                                chunk_count += 1
                    except asyncio.CancelledError:
                        logger.info("Client disconnected, closing upstream stream")
                        raise

        except httpx.TimeoutException:
            logger.error("Timeout connecting to upstream stream")
            yield b"Upstream timeout"
        except httpx.ConnectError as e:
            logger.error("Failed to connect to upstream stream: %s", str(e))
            yield b"Upstream connection failed"
        except Exception as e:
            logger.error("Error streaming from upstream: %s", str(e))
            yield b"Stream error"

    headers = stream_proxy.get_stream_headers()

    # Determine content type based on STREAM_URL
    # MJPEG streams typically use multipart/x-mixed-replace
    content_type = "multipart/x-mixed-replace; boundary=frame"
    if config.stream_url and "mjpeg" in config.stream_url.lower():
        content_type = "multipart/x-mixed-replace; boundary=frame"
    elif config.stream_url and "h264" in config.stream_url.lower():
        content_type = "video/h264"
    else:
        # Default to MJPEG for Bambu
        content_type = "multipart/x-mixed-replace; boundary=frame"

    headers["Content-Type"] = content_type

    return StreamingResponse(stream_generator(), headers=headers)


@app.get("/status", response_class=JSONResponse)
async def status():
    """
    Optional status endpoint with more details.
    Useful for debugging and monitoring.
    """
    return JSONResponse(
        {
            "stream_configured": config.is_configured(),
            "stream_url_masked": stream_proxy.config._mask_url(config.stream_url) if config.stream_url else None,
            "uptime_seconds": get_uptime_seconds(),
            "timestamp": datetime.utcnow().isoformat(),
            "app_version": "0.1.0",
        }
    )


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Uvicorn server on 0.0.0.0:%d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
