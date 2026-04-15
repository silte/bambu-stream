"""
Stream proxy handler for Bambu P2S camera feed.

Phase 1: Simple stateless proxy fetching directly from printer's STREAM_URL.
Phase 2: Will add persistent connection + fan-out to multiple viewers.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger("stream_manager")


class StreamConfig:
    """Stream configuration from environment."""

    def __init__(self):
        self.stream_url: Optional[str] = os.getenv("STREAM_URL")
        self.reconnect_initial_seconds = int(os.getenv("RECONNECT_INITIAL_SECONDS", "1"))
        self.reconnect_max_seconds = int(os.getenv("RECONNECT_MAX_SECONDS", "30"))
        self.auto_reconnect = os.getenv("AUTO_RECONNECT", "true").lower() == "true"

    def is_configured(self) -> bool:
        """Check if stream URL is configured."""
        return bool(self.stream_url)

    def log_config(self):
        """Log configuration without exposing secrets."""
        if self.is_configured():
            logger.info("Stream configured: %s", self._mask_url(self.stream_url))
        else:
            logger.warning("Stream NOT configured - STREAM_URL environment variable missing")

    @staticmethod
    def _mask_url(url: str) -> str:
        """Mask credentials in URL for logging."""
        if not url:
            return ""
        # Simple masking: show scheme and host, mask credentials
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.password:
                return f"{parsed.scheme}://***:***@{parsed.hostname}:{parsed.port or ''}{parsed.path}"
            return url
        except Exception:
            return url


class StreamProxy:
    """Phase 1: Stateless proxy. Phase 2: Will add persistent connection."""

    def __init__(self, config: StreamConfig):
        self.config = config
        self.upstream_error: Optional[str] = None

    async def is_upstream_available(self) -> bool:
        """Check if upstream is reachable (Phase 2 will improve this)."""
        if not self.config.is_configured():
            return False
        # Phase 1: We'll know on first stream attempt
        # Phase 2: Will maintain persistent connection state
        return True

    def get_stream_headers(self) -> dict:
        """Return headers that prevent caching on all layers."""
        return {
            "Cache-Control": "no-store, no-cache, must-revalidate, no-transform",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Connection": "keep-alive",
        }
