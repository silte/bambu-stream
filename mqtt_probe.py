"""
Lightweight Bambu MQTT probe for stream URL discovery and debugging.

This mirrors the HA flow at a high level:
- Connect to local MQTT broker on the printer (TLS, port 8883)
- Subscribe to device/{serial}/report
- Publish get_version and pushall requests
- Parse print.ipcam.rtsp_url from incoming payloads
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import uuid
from urllib.parse import urlparse, urlunparse
from typing import Any, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger("mqtt_probe")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class MqttProbeConfig:
    """MQTT probe configuration from environment variables."""

    def __init__(self) -> None:
        self.enabled: bool = _env_bool("MQTT_DISCOVERY_ENABLED", False)
        self.host: str = os.getenv("MQTT_HOST", "")
        self.port: int = int(os.getenv("MQTT_PORT", "8883"))
        self.serial: str = os.getenv("MQTT_SERIAL", "")
        self.access_code: str = os.getenv("MQTT_ACCESS_CODE", "")
        self.timeout_seconds: int = int(
            os.getenv("MQTT_TIMEOUT_SECONDS", "10"))
        self.tls_insecure: bool = _env_bool("MQTT_TLS_INSECURE", False)
        self.tls_allow_insecure_fallback: bool = _env_bool(
            "MQTT_TLS_ALLOW_INSECURE_FALLBACK", False
        )
        self.tls_ca_cert: str = os.getenv("MQTT_TLS_CA_CERT", "")

    def is_configured(self) -> bool:
        return bool(self.host and self.serial and self.access_code)

    @property
    def report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.serial}/request"

    def masked(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "serial": self.serial,
            "access_code": "***" if self.access_code else "",
            "timeout_seconds": self.timeout_seconds,
            "tls_insecure": self.tls_insecure,
            "tls_allow_insecure_fallback": self.tls_allow_insecure_fallback,
            "tls_ca_cert": self.tls_ca_cert,
            "report_topic": self.report_topic if self.serial else "",
            "request_topic": self.request_topic if self.serial else "",
        }


class BambuMqttProbe:
    """One-shot MQTT probe for extracting RTSP URL from printer status payloads."""

    def __init__(self, config: MqttProbeConfig) -> None:
        self.config = config

    @staticmethod
    def _looks_like_cert_verify_error(error_text: str) -> bool:
        if not error_text:
            return False
        lowered = error_text.lower()
        return (
            "certificate_verify_failed" in lowered
            or "unable to get issuer certificate" in lowered
            or "self-signed certificate" in lowered
        )

    def _ensure_rtsp_credentials(self, url: str) -> str:
        """
        Ensure discovered RTSP/RTSPS URL includes bblp credentials.

        Some MQTT payloads provide an rtsp_url without username/password.
        In that case we rewrite it to include bblp:{access_code}@host:port.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"rtsp", "rtsps"}:
                return url
            if parsed.username:
                return url

            host = parsed.hostname or ""
            if not host:
                return url

            # Wrap IPv6 hosts in brackets for netloc formatting.
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"

            auth = f"bblp:{self.config.access_code}@"
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{auth}{host}{port}"

            return urlunparse(
                (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
        except Exception:
            return url

    def probe_once(self) -> dict[str, Any]:
        """
        Connect once, request push data, and return parsed debug info.

        Returns keys:
        - ok
        - discovered_rtsp_url
        - message_count
        - samples
        - error
        """
        if not self.config.is_configured():
            return {
                "ok": False,
                "error": "MQTT probe not configured (need MQTT_HOST, MQTT_SERIAL, MQTT_ACCESS_CODE)",
                "config": self.config.masked(),
            }

        def run_attempt(*, tls_insecure: bool) -> dict[str, Any]:
            discovered_rtsp_url: Optional[str] = None
            error: Optional[str] = None
            samples: list[dict[str, Any]] = []
            message_count = 0
            connected_event = threading.Event()
            done_event = threading.Event()

            client = mqtt.Client(
                client_id=f"bambu-stream-{uuid.uuid4()}", protocol=mqtt.MQTTv311)
            client.enable_logger(logger)

            def on_connect(client_: mqtt.Client, userdata: Any, flags: dict[str, Any], rc: int):
                nonlocal error
                if rc != 0:
                    error = f"MQTT connect failed with rc={rc}"
                    done_event.set()
                    return

                connected_event.set()
                client_.subscribe(self.config.report_topic)
                client_.publish(self.config.request_topic, json.dumps(
                    {"info": {"command": "get_version"}}))
                client_.publish(
                    self.config.request_topic,
                    json.dumps(
                        {"pushing": {"sequence_id": "0", "command": "pushall"}}),
                )

            def on_message(client_: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage):
                nonlocal discovered_rtsp_url, message_count
                message_count += 1
                payload_obj: Any = None

                try:
                    payload_obj = json.loads(
                        msg.payload.decode("utf-8", errors="replace"))
                except Exception:
                    payload_obj = {"raw": msg.payload.decode(
                        "utf-8", errors="replace")[:500]}

                if isinstance(payload_obj, dict):
                    print_data = payload_obj.get("print", {}) if isinstance(
                        payload_obj.get("print"), dict) else {}
                    ipcam = print_data.get("ipcam", {}) if isinstance(
                        print_data.get("ipcam"), dict) else {}
                    rtsp_url = ipcam.get("rtsp_url")
                    if isinstance(rtsp_url, str) and rtsp_url:
                        discovered_rtsp_url = self._ensure_rtsp_credentials(
                            rtsp_url)
                        done_event.set()

                if len(samples) < 5:
                    samples.append(
                        {"topic": msg.topic, "payload": payload_obj})

            def on_disconnect(client_: mqtt.Client, userdata: Any, rc: int):
                if not done_event.is_set() and rc != 0 and not error:
                    done_event.set()

            client.on_connect = on_connect
            client.on_message = on_message
            client.on_disconnect = on_disconnect

            client.username_pw_set("bblp", password=self.config.access_code)

            context = ssl.create_default_context()
            if self.config.tls_ca_cert:
                context.load_verify_locations(cafile=self.config.tls_ca_cert)
            if tls_insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            client.tls_set_context(context)
            if tls_insecure:
                client.tls_insecure_set(True)

            try:
                client.connect(self.config.host,
                               self.config.port, keepalive=10)
                client.loop_start()

                # Wait for connect first for clearer errors, then for discovery.
                if not connected_event.wait(timeout=min(5, self.config.timeout_seconds)):
                    error = error or "MQTT connect timeout"
                else:
                    done_event.wait(timeout=self.config.timeout_seconds)
            except Exception as exc:
                error = str(exc)
            finally:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass

            return {
                "ok": discovered_rtsp_url is not None,
                "discovered_rtsp_url": discovered_rtsp_url,
                "message_count": message_count,
                "samples": samples,
                "error": error,
            }

        initial = run_attempt(tls_insecure=self.config.tls_insecure)
        retry_used = False

        # If strict TLS verification fails, retry once in insecure mode for resilience.
        if (
            not initial["ok"]
            and not self.config.tls_insecure
            and self.config.tls_allow_insecure_fallback
            and self._looks_like_cert_verify_error(initial.get("error", ""))
        ):
            logger.warning(
                "MQTT TLS verification failed; retrying probe once with insecure TLS "
                "(set MQTT_TLS_ALLOW_INSECURE_FALLBACK=true to allow this behavior)"
            )
            retry_used = True
            retry = run_attempt(tls_insecure=True)
            if retry["ok"]:
                initial = retry
            else:
                # Keep original error context but still return retry diagnostics.
                initial["fallback_error"] = retry.get("error")

        return {
            **initial,
            "retry_insecure_used": retry_used,
            "config": self.config.masked(),
            "timestamp": int(time.time()),
        }
