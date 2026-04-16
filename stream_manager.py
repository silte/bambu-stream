"""
Stream manager for Bambu P2S camera feed.

Single upstream connection with fan-out to multiple viewers.
The background task connects to the configured STREAM_URL, reads chunks, and
distributes them to all subscribed viewer queues. On disconnect it reconnects
with exponential backoff so the printer sees only one stream consumer.
"""

import asyncio
import logging
import os
import re
import time
from typing import Dict, Optional

import httpx

logger = logging.getLogger("stream_manager")

# Backoff delays in seconds; capped at RECONNECT_MAX_SECONDS from env.
_BACKOFF_SEQUENCE = [1, 2, 5, 10, 30]
# Per-viewer queue depth. Slow viewers drop frames rather than stalling the broadcast.
_QUEUE_MAX = 64
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<creds>[^/@\s]+)@"
)
_TRANSIENT_FFMPEG_MESSAGES = (
    "error in the push function",
    "error in the pull function",
    "io error:",
    "connection reset by peer",
    "end of file",
    "session has been invalidated",
    "error during demuxing: input/output error",
)


class StreamConfig:
    """Stream configuration loaded from environment variables."""

    def __init__(self):
        self.stream_url: Optional[str] = os.getenv("STREAM_URL")
        self.auto_reconnect: bool = os.getenv(
            "AUTO_RECONNECT", "true").lower() == "true"
        self.reconnect_initial_seconds: int = int(
            os.getenv("RECONNECT_INITIAL_SECONDS", "1"))
        self.reconnect_max_seconds: int = int(
            os.getenv("RECONNECT_MAX_SECONDS", "30"))
        self.idle_disconnect_seconds: int = int(
            os.getenv("IDLE_DISCONNECT_SECONDS", "300"))

    def is_configured(self) -> bool:
        return bool(self.stream_url)

    def log_config(self) -> None:
        if self.is_configured():
            logger.info("STREAM_URL configured: %s",
                        self._mask_url(self.stream_url))
            logger.info(
                "AUTO_RECONNECT=%s  initial=%ds  max=%ds  idle_disconnect=%ds",
                self.auto_reconnect,
                self.reconnect_initial_seconds,
                self.reconnect_max_seconds,
                self.idle_disconnect_seconds,
            )
        else:
            logger.warning("STREAM_URL not set — /stream will return errors")

    @staticmethod
    def _mask_url(url: str) -> str:
        """Return URL with credentials replaced by *** for safe logging."""
        if not url:
            return ""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.username:
                host = parsed.hostname or ""
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                port = f":{parsed.port}" if parsed.port else ""
                return (
                    f"{parsed.scheme}://***:***@{host}"
                    f"{port}{parsed.path}"
                )
            return url
        except Exception:
            return url


class StreamManager:
    """
    Maintains one persistent upstream HTTP stream and fans chunks out to viewers.

    Lifecycle
    ---------
    Call ``await start()`` on app startup and ``await stop()`` on shutdown.

    Viewer flow
    -----------
    Each viewer calls ``subscribe()`` to get a (viewer_id, asyncio.Queue) pair.
    The viewer reads chunks from the queue and yields them to the HTTP response.
    A ``None`` sentinel in the queue signals that the upstream has ended; the
    viewer should close its response so the browser can auto-reconnect.
    Call ``unsubscribe(viewer_id)`` when the viewer is done (from a finally block).

    Fan-out is non-blocking: if a viewer queue is full the frame is dropped for
    that viewer rather than blocking the shared upstream reader.
    """

    def __init__(self, config: StreamConfig) -> None:
        self.config = config
        self._viewers: Dict[int, asyncio.Queue] = {}
        self._next_id: int = 0
        self._task: Optional[asyncio.Task] = None
        self._idle_disconnect_task: Optional[asyncio.Task] = None
        self._connected: bool = False
        self._reconnect_count: int = 0
        self._last_error: Optional[str] = None
        # Default boundary matches FFmpeg mpjpeg output so first RTSP response
        # can be parsed even before upstream content-type is refreshed.
        self._content_type: str = "multipart/x-mixed-replace; boundary=ffmpeg"
        self._start_time: float = time.time()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background upstream connection loop (idempotent)."""
        if not self.config.is_configured():
            logger.warning(
                "StreamManager not starting: STREAM_URL not configured")
            return
        if self._task and not self._task.done():
            return  # already running
        logger.info("StreamManager starting upstream loop")
        self._task = asyncio.create_task(
            self._upstream_loop(), name="upstream-loop")

    async def stop(self) -> None:
        """Cancel the upstream loop and signal all viewers to close."""
        if self._idle_disconnect_task and not self._idle_disconnect_task.done():
            self._idle_disconnect_task.cancel()
            try:
                await self._idle_disconnect_task
            except asyncio.CancelledError:
                pass

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._broadcast(None)
        logger.info("StreamManager stopped")

    # ------------------------------------------------------------------
    # Viewer subscription
    # ------------------------------------------------------------------

    async def subscribe(self) -> tuple[int, asyncio.Queue]:
        """
        Register a new viewer. Returns (viewer_id, queue).

        Starts the upstream loop on demand if it is not already running
        (e.g. after it was stopped because the last viewer left).
        """
        viewer_id = self._next_id
        self._next_id += 1
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._viewers[viewer_id] = queue

        if self._idle_disconnect_task and not self._idle_disconnect_task.done():
            self._idle_disconnect_task.cancel()
            self._idle_disconnect_task = None

        logger.info("Viewer %d connected (active viewers: %d)",
                    viewer_id, len(self._viewers))
        await self.start()
        return viewer_id, queue

    def unsubscribe(self, viewer_id: int) -> None:
        """
        Remove a viewer. Stops the upstream loop when the last viewer leaves
        so the printer is not held open for an idle app.
        """
        self._viewers.pop(viewer_id, None)
        remaining = len(self._viewers)
        logger.info("Viewer %d disconnected (remaining viewers: %d)",
                    viewer_id, remaining)
        if remaining == 0:
            if not self._connected and self._task and not self._task.done():
                logger.info(
                    "No viewers and upstream is disconnected — pausing reconnect loop"
                )
                self._task.cancel()
                return

            if self._idle_disconnect_task and not self._idle_disconnect_task.done():
                self._idle_disconnect_task.cancel()
            logger.info(
                "No viewers remaining — keeping upstream alive for %ds",
                self.config.idle_disconnect_seconds,
            )
            self._idle_disconnect_task = asyncio.create_task(
                self._disconnect_after_idle_timeout(),
                name="idle-disconnect",
            )

    async def _disconnect_after_idle_timeout(self) -> None:
        """Stop the upstream loop if no viewers reconnect within the grace period."""
        try:
            await asyncio.sleep(self.config.idle_disconnect_seconds)
            if len(self._viewers) == 0 and self._task and not self._task.done():
                logger.info(
                    "Idle timeout reached — pausing upstream connection")
                self._task.cancel()
                self._connected = False
        except asyncio.CancelledError:
            logger.debug("Idle disconnect timer cancelled")
            raise
        finally:
            self._idle_disconnect_task = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _broadcast(self, chunk: Optional[bytes]) -> None:
        """
        Distribute chunk to every viewer queue.

        For data chunks: drop silently if a viewer queue is full.
        For the None sentinel: clear space if needed so it always lands.
        """
        for viewer_id, queue in list(self._viewers.items()):
            if chunk is None:
                # Drain stale data to make room for the end-of-stream sentinel.
                while queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                logger.debug("Dropped frame for slow viewer %d", viewer_id)

    def _sanitize_error(self, value: str) -> str:
        """Best-effort redaction for credential-bearing URLs in error text."""
        if not value:
            return value

        sanitized = _URL_CREDENTIALS_RE.sub(r"\g<scheme>***:***@", value)
        if self.config.stream_url:
            sanitized = sanitized.replace(
                self.config.stream_url, self.config._mask_url(
                    self.config.stream_url)
            )
        return sanitized

    @staticmethod
    def _is_transient_ffmpeg_disconnect(stderr_text: str) -> bool:
        """Return True for known RTSP/TLS disconnect messages from Bambu streams."""
        lower = (stderr_text or "").lower()
        return any(msg in lower for msg in _TRANSIENT_FFMPEG_MESSAGES)

    @staticmethod
    def _summarize_ffmpeg_stderr(stderr_text: str) -> str:
        """Extract a compact one-line reason from FFmpeg stderr output."""
        lines = [line.strip()
                 for line in (stderr_text or "").splitlines() if line.strip()]
        if not lines:
            return "unknown upstream disconnect"

        for line in reversed(lines):
            lower = line.lower()
            if any(msg in lower for msg in _TRANSIENT_FFMPEG_MESSAGES):
                return line
        return lines[-1]

    @staticmethod
    def _is_rtsp(url: str) -> bool:
        return url.lower().startswith(("rtsp://", "rtsps://"))

    async def _upstream_loop(self) -> None:
        """
        Core reconnect loop. Dispatches to HTTP or RTSP/FFmpeg path based on scheme.
        """
        backoff_idx = 0

        while True:
            try:
                masked = self.config._mask_url(self.config.stream_url)
                logger.info(
                    "Connecting to upstream: %s (reconnect count: %d)",
                    masked,
                    self._reconnect_count,
                )

                if self._is_rtsp(self.config.stream_url):
                    await self._read_rtsp()
                else:
                    await self._read_http()

                backoff_idx = 0  # reset after a successful connection
                # Stream ended cleanly.
                logger.warning("Upstream stream ended")
                self._connected = False
                self._broadcast(None)

            except asyncio.CancelledError:
                logger.info("Upstream loop cancelled")
                self._connected = False
                self._broadcast(None)
                raise

            except Exception as exc:
                if self._connected:
                    self._connected = False
                    self._broadcast(None)
                self._last_error = self._sanitize_error(str(exc))
                logger.error("Upstream error: %s", self._last_error)

            if not self.config.auto_reconnect:
                logger.info("AUTO_RECONNECT=false — upstream loop exiting")
                break

            # Avoid reconnect churn while no one is watching.
            if len(self._viewers) == 0:
                logger.info(
                    "No active viewers — upstream reconnect paused until next viewer"
                )
                break

            self._reconnect_count += 1
            raw_delay = _BACKOFF_SEQUENCE[min(
                backoff_idx, len(_BACKOFF_SEQUENCE) - 1)]
            delay = min(raw_delay, self.config.reconnect_max_seconds)
            backoff_idx = min(backoff_idx + 1, len(_BACKOFF_SEQUENCE) - 1)
            logger.info(
                "Reconnecting in %ds (attempt #%d)…", delay, self._reconnect_count
            )
            await asyncio.sleep(delay)

    async def _read_http(self) -> None:
        """Fetch a plain HTTP stream and broadcast chunks."""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10.0)
        ) as client:
            async with client.stream("GET", self.config.stream_url) as response:
                if response.status_code != 200:
                    msg = f"Upstream returned HTTP {response.status_code}"
                    logger.warning(msg)
                    self._last_error = msg
                    self._connected = False
                    self._broadcast(None)
                    raise httpx.HTTPStatusError(
                        msg, request=response.request, response=response
                    )

                self._content_type = response.headers.get(
                    "content-type", "multipart/x-mixed-replace; boundary=frame"
                )
                self._connected = True
                self._last_error = None
                logger.info("Upstream connected  content-type=%s",
                            self._content_type)

                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if chunk:
                        self._broadcast(chunk)

    async def _read_rtsp(self) -> None:
        """
        Connect to an RTSP or RTSPS URL via FFmpeg and broadcast MJPEG output.

        FFmpeg is invoked as a subprocess. It outputs multipart/x-mixed-replace
        (mpjpeg muxer) to stdout which is read and broadcast chunk-by-chunk.

        Requires ffmpeg to be installed and available on PATH.
        TLS verification is disabled so self-signed Bambu certs are accepted.
        """
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.config.stream_url,
            "-f", "mpjpeg",
            "-q:v", "3",
            "-r", "15",
            "pipe:1",
        ]

        logger.info("Launching FFmpeg for RTSP stream")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._content_type = "multipart/x-mixed-replace; boundary=ffmpeg"
        self._connected = True
        self._last_error = None
        logger.info("FFmpeg started  content-type=%s", self._content_type)

        bytes_streamed = 0
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                bytes_streamed += len(chunk)
                self._broadcast(chunk)
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

            stderr = await proc.stderr.read()
            if stderr:
                decoded = stderr.decode(errors="replace").strip()
                sanitized = self._sanitize_error(decoded)
                summary = self._summarize_ffmpeg_stderr(sanitized)
                if self._is_transient_ffmpeg_disconnect(decoded):
                    logger.warning("FFmpeg upstream disconnect: %s", summary)
                else:
                    logger.error("FFmpeg stderr: %s", sanitized)

            if proc.returncode not in (0, None) and bytes_streamed == 0:
                raise RuntimeError(
                    f"FFmpeg exited before producing stream (code {proc.returncode})"
                )

    # ------------------------------------------------------------------
    # Public state / headers
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def content_type(self) -> str:
        return self._content_type

    def get_stream_headers(self) -> dict:
        """No-cache, no-buffer headers required for live streaming through proxies."""
        return {
            "Cache-Control": "no-store, no-cache, must-revalidate, no-transform",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }

    def get_response_content_type(self) -> str:
        """
        Content type to use for downstream /stream responses.

        For RTSP inputs, FFmpeg always emits mpjpeg with boundary=ffmpeg.
        Use that boundary immediately so first viewer responses do not race
        against async upstream startup and accidentally send a stale boundary.
        """
        if self.config.stream_url and self._is_rtsp(self.config.stream_url):
            return "multipart/x-mixed-replace; boundary=ffmpeg"
        return self._content_type

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "viewer_count": len(self._viewers),
            "reconnect_count": self._reconnect_count,
            "last_error": self._last_error,
            "content_type": self._content_type,
            "uptime_seconds": int(time.time() - self._start_time),
        }
