"""
Bambu P2S Live Stream App
Simple web app serving printer camera feed via HTTPS behind Cloudflare.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from mqtt_probe import BambuMqttProbe, MqttProbeConfig
from stream_manager import StreamConfig, StreamManager

# ============================================================================
# LOGGING SETUP
# ============================================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bambu_stream_app")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

# ============================================================================
# APP INITIALIZATION
# ============================================================================


APP_TITLE = os.getenv("APP_TITLE", "Bambu P2S Live")
PORT = int(os.getenv("PORT", 8000))
DEBUG_MQTT_ENDPOINT_ENABLED = _env_bool("DEBUG_MQTT_ENDPOINT_ENABLED", False)

config = StreamConfig()
stream_manager = StreamManager(config)
mqtt_probe_config = MqttProbeConfig()
mqtt_probe = BambuMqttProbe(mqtt_probe_config)

app_start_time = time.time()

# Load HTML templates once at startup.
_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX_HTML = open(os.path.join(_HERE, "index.html")
                   ).read().replace("{APP_TITLE}", APP_TITLE)
_ERROR_HTML = open(os.path.join(_HERE, "error.html")).read()


def get_uptime_seconds() -> int:
    return int(time.time() - app_start_time)


def _sanitize_probe_result(probe_result: dict) -> dict:
    discovered_rtsp_url = probe_result.get("discovered_rtsp_url")
    masked_discovered = None
    if isinstance(discovered_rtsp_url, str) and discovered_rtsp_url:
        masked_discovered = config._mask_url(discovered_rtsp_url)

    return {
        "ok": bool(probe_result.get("ok", False)),
        "discovered_rtsp_url_masked": masked_discovered,
        "message_count": probe_result.get("message_count", 0),
        "error": probe_result.get("error"),
        "retry_insecure_used": probe_result.get("retry_insecure_used", False),
        "timestamp": probe_result.get("timestamp"),
    }


# ============================================================================
# LIFESPAN (startup / shutdown)
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Bambu P2S Stream App starting")
    logger.info("APP_TITLE=%s  PORT=%s  LOG_LEVEL=%s",
                APP_TITLE, PORT, LOG_LEVEL)
    config.log_config()

    if not config.is_configured() and mqtt_probe_config.is_configured():
        logger.info("STREAM_URL missing; running one-shot MQTT discovery")
        probe_result = await asyncio.to_thread(mqtt_probe.probe_once)
        discovered_rtsp_url = probe_result.get("discovered_rtsp_url")
        if discovered_rtsp_url:
            config.stream_url = discovered_rtsp_url
            logger.info("STREAM_URL auto-discovered via MQTT: %s",
                        config._mask_url(config.stream_url))
        else:
            logger.warning(
                "MQTT startup discovery did not find rtsp_url: %s", probe_result.get("error"))

    logger.info("Upstream loop will start on first viewer connection")
    logger.info("=" * 60)
    yield
    logger.info("Bambu P2S Stream App shutting down")
    await stream_manager.stop()


app = FastAPI(title="Bambu P2S Live", lifespan=lifespan)

# ============================================================================
# ROUTES
# ============================================================================


@app.get("/health", response_class=JSONResponse)
async def health():
    """Health check. Reflects real upstream connection state."""
    return JSONResponse(
        {
            "ok": True,
            "stream_configured": config.is_configured(),
            "upstream_connected": stream_manager.is_connected,
            "app_version": "0.2.0",
            "uptime_seconds": get_uptime_seconds(),
            "timestamp": datetime.utcnow().isoformat(),
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    """Main page: MJPEG player with WebSocket-driven status and viewer count."""
    if not config.is_configured():
        return HTMLResponse(_ERROR_HTML)
    return HTMLResponse(_INDEX_HTML)


@app.get("/stream")
async def stream():
    """
    Live stream endpoint.

    Subscribes this viewer to the shared upstream connection maintained by
    StreamManager. Chunks arrive via an asyncio.Queue; a None sentinel signals
    that the upstream has ended and the response is closed cleanly so the
    browser can auto-reconnect.
    """
    if not config.is_configured():
        logger.warning("Stream endpoint called but STREAM_URL not configured")
        return JSONResponse(
            {
                "error": "Stream not configured",
                "details": "STREAM_URL environment variable is missing",
            },
            status_code=500,
        )

    viewer_id, queue = await stream_manager.subscribe()

    async def stream_generator():
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    # Upstream ended; close this response so the browser reconnects.
                    break
                yield chunk
        except asyncio.CancelledError:
            # Browser disconnected or Cloudflare timed out the connection.
            pass
        finally:
            stream_manager.unsubscribe(viewer_id)

    headers = stream_manager.get_stream_headers()
    return StreamingResponse(
        stream_generator(),
        media_type=stream_manager.get_response_content_type(),
        headers=headers,
    )


@app.websocket("/ws/stats")
async def ws_stats(websocket: WebSocket):
    """
    Push stream stats to the browser every second.
    Replaces polling — closes cleanly when the client disconnects.
    """
    await websocket.accept()
    try:
        while True:
            s = stream_manager.get_status()
            await websocket.send_json(
                {
                    "viewer_count": s["viewer_count"],
                    "upstream_connected": s["connected"],
                    "reconnect_count": s["reconnect_count"],
                    "last_error": s["last_error"],
                }
            )
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.debug("Stats WebSocket disconnected")
    except Exception:
        logger.exception("Unhandled error in /ws/stats")


@app.get("/status", response_class=JSONResponse)
async def status():
    """Extended status for debugging and monitoring."""
    s = stream_manager.get_status()
    return JSONResponse(
        {
            "stream_configured": config.is_configured(),
            "stream_url_masked": config._mask_url(config.stream_url) if config.stream_url else None,
            "upstream_connected": s["connected"],
            "viewer_count": s["viewer_count"],
            "reconnect_count": s["reconnect_count"],
            "last_error": s["last_error"],
            "content_type": s["content_type"],
            "uptime_seconds": get_uptime_seconds(),
            "timestamp": datetime.utcnow().isoformat(),
            "app_version": "0.2.0",
        }
    )


@app.get("/debug/mqtt", response_class=JSONResponse)
async def debug_mqtt():
    """Run a one-shot MQTT probe with redacted response data."""
    if not DEBUG_MQTT_ENDPOINT_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    probe_result = await asyncio.to_thread(mqtt_probe.probe_once)
    return JSONResponse(_sanitize_probe_result(probe_result))


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Uvicorn on 0.0.0.0:%d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
