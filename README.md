# Bambu P2S Live Stream App

A small, self-hosted web app that securely serves a Bambu P2S camera stream through a public HTTPS subdomain behind Cloudflare. The backend maintains **one persistent upstream connection** and fans the stream out to all connected viewers, so the printer is never hit by multiple stream sessions.

## Architecture

```
Bambu P2S Printer
    ↓  (one connection, always)
Our Stream App  ──  StreamManager background loop
    ↓  (fan-out via per-viewer asyncio queues)
Reverse Proxy (Cloudflare / k3s Ingress)
    ↓
Remote Browsers  (N viewers, zero extra printer connections)
```

## Features

- **Single upstream connection** – StreamManager keeps one HTTP stream open and distributes chunks to every viewer via asyncio queues.
- **Exponential backoff reconnect** – delays: 1 s → 2 s → 5 s → 10 s → 30 s (max).
- **Cloudflare-friendly** – no-cache / no-buffer headers, frontend auto-reconnects when Cloudflare drops the long-lived connection.
- **Health + status endpoints** – real connection state, viewer count, reconnect count, last error.
- **Minimal stack** – FastAPI + httpx + uvicorn, ~300 lines of Python total.

## Quick Start

### Local Development

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

STREAM_URL=http://PRINTER_IP:PORT/stream python app.py
```

Open: `http://localhost:8000/`

### Docker

```bash
# Build
docker build -t bambu-stream:latest .

# Run
docker run -d \
  -p 8000:8000 \
  -e STREAM_URL=http://PRINTER_IP:PORT/stream \
  -e APP_TITLE="Bambu P2S Live" \
  -e LOG_LEVEL=INFO \
  --name bambu-stream \
  bambu-stream:latest

docker logs -f bambu-stream
```

## Configuration

### Required (one of)

| Option | Variables | Description |
| ------ | --------- | ----------- |
| Direct stream URL | `STREAM_URL` | URL of upstream stream (`http://...` or `rtsps://...`) |
| MQTT auto-discovery (preferred) | `MQTT_SERIAL`, `MQTT_ACCESS_CODE`, `MQTT_HOST` | App discovers RTSP URL from printer MQTT on startup |

### Optional

| Variable                    | Default          | Description                                    |
| --------------------------- | ---------------- | ---------------------------------------------- |
| `APP_TITLE`                 | `Bambu P2S Live` | Title shown in browser tab and page header     |
| `PORT`                      | `8000`           | Listening port                                 |
| `LOG_LEVEL`                 | `INFO`           | `DEBUG` / `INFO` / `WARNING` / `ERROR`         |
| `TZ`                        | `UTC`            | Timezone for log timestamps                    |
| `AUTO_RECONNECT`            | `true`           | Enable upstream reconnect loop                 |
| `RECONNECT_INITIAL_SECONDS` | `1`              | First backoff delay (seconds)                  |
| `RECONNECT_MAX_SECONDS`     | `30`             | Maximum backoff delay (seconds)                |
| `IDLE_DISCONNECT_SECONDS`   | `300`            | Keep upstream alive after last viewer leaves   |
| `MQTT_HOST`                 | ``               | Printer IP for local MQTT                      |
| `MQTT_PORT`                 | `8883`           | Local MQTT TLS port                            |
| `MQTT_SERIAL`               | ``               | Printer serial (used in MQTT topic)            |
| `MQTT_ACCESS_CODE`          | ``               | Printer access code (MQTT password for `bblp`) |
| `MQTT_TIMEOUT_SECONDS`      | `10`             | Probe timeout for `/debug/mqtt` and startup    |
| `MQTT_TLS_INSECURE`         | `false`          | Disable cert verification for printer TLS cert |
| `MQTT_TLS_ALLOW_INSECURE_FALLBACK` | `false`  | Allow one-time fallback to insecure TLS when strict verification fails |
| `MQTT_TLS_CA_CERT`          | ``               | Optional CA cert path for MQTT TLS verification |
| `DEBUG_MQTT_ENDPOINT_ENABLED` | `false`        | Enables `GET /debug/mqtt` (disabled by default) |

For Kubernetes, prefer secret keys: `MQTT_SERIAL`, `MQTT_ACCESS_CODE`, `MQTT_HOST`, `MQTT_TLS_INSECURE`, `MQTT_TLS_CA_CERT`.

### Bambu P2S Stream URL

The P2S camera uses **RTSPS** (RTSP over TLS on port 322). Set `STREAM_URL` to:

```
rtsps://bblp:{ACCESS_CODE}@{PRINTER_IP}:322/streaming/live/1
```

The app connects via FFmpeg (bundled in the Docker image), so no extra tools are needed. TLS verification is disabled automatically to accept the printer's self-signed certificate.

**Alternative** — if you already run go2rtc or another MJPEG bridge:

```
http://go2rtc-host:1984/api/frame.mjpg?src=bambu
```

> ⚠️ Treat your access code like a password — do not commit it to source control.

## Endpoints

### `GET /health`

```json
{
  "ok": true,
  "stream_configured": true,
  "upstream_connected": true,
  "app_version": "0.2.0",
  "uptime_seconds": 123,
  "timestamp": "2025-04-15T10:30:45.123456"
}
```

`upstream_connected` reflects the real state of the StreamManager background connection.

### `GET /`

Player page: MJPEG `<img>`, status dot, reconnect button, `/health` polling every 5 s.

Theater layout note: theater mode caps page width at `1952px`, which is `1920px` stream area plus `16px` horizontal padding on each side (`2 x 16`).

### `GET /stream`

Live stream proxy. Subscribes the viewer to the shared upstream queue.

Headers:
```
Cache-Control: no-store, no-cache, must-revalidate, no-transform
Pragma: no-cache
X-Accel-Buffering: no
```

Returns 500 JSON if `STREAM_URL` is not configured.

### `GET /status`

Extended debug endpoint:

```json
{
  "stream_configured": true,
  "stream_url_masked": "http://192.168.1.100:8080/stream",
  "upstream_connected": true,
  "viewer_count": 2,
  "reconnect_count": 0,
  "last_error": null,
  "content_type": "multipart/x-mixed-replace; boundary=frame",
  "uptime_seconds": 456,
  "timestamp": "2025-04-15T10:30:45.123456",
  "app_version": "0.2.0"
}
```

### `GET /debug/mqtt`

This endpoint is **disabled by default**.
Set `DEBUG_MQTT_ENDPOINT_ENABLED=true` to enable it.

Runs a one-shot MQTT probe similar to HA's request flow:

- Subscribes to `device/{serial}/report`
- Publishes `get_version` and `pushall` to `device/{serial}/request`
- Parses `print.ipcam.rtsp_url` from responses

Response includes:

- `discovered_rtsp_url_masked`
- `message_count`
- error details (if any)

When MQTT returns an RTSP/RTSPS URL without credentials, the app rewrites it to include `bblp:{MQTT_ACCESS_CODE}@...` before using it.

If `STREAM_URL` is not set and MQTT config is present (`MQTT_HOST`, `MQTT_SERIAL`, `MQTT_ACCESS_CODE`), the app runs this probe once on startup, auto-sets `STREAM_URL` when `rtsp_url` is found, then closes MQTT.

If you want strict TLS validation for MQTT, set `MQTT_TLS_INSECURE=false` and provide `MQTT_TLS_CA_CERT` (for example, a local copy of the HA cert file). This affects MQTT probing only.

If strict MQTT TLS validation fails with a certificate verification error, the app retries discovery once with insecure TLS only when `MQTT_TLS_ALLOW_INSECURE_FALLBACK=true`.

## Logging

Key events logged (no secrets / no auth tokens):

```
INFO  - Bambu P2S Stream App starting
INFO  - STREAM_URL configured: http://192.168.1.100:8080/stream
INFO  - StreamManager starting upstream loop
INFO  - Connecting to upstream: http://192.168.1.100:8080/stream (reconnect count: 0)
INFO  - Upstream connected  content-type=multipart/x-mixed-replace; boundary=frame
INFO  - Viewer 0 connected (active viewers: 1)
INFO  - Viewer 0 disconnected (remaining viewers: 0)
ERROR - Upstream error: Connection refused
INFO  - Reconnecting in 2s (attempt #1)…
```

## Deployment with k3s / Ingress

The ingress, service, DNS, and Cloudflare proxy are managed separately. The app only needs a `Deployment` pointing at the container image with the env vars above.

Suggested resource limits:

```yaml
resources:
  requests:
    cpu: 100m
    memory: 64Mi
  limits:
    cpu: 500m
    memory: 256Mi
```

## Cloudflare Notes

- Enable **proxy** on the subdomain.
- Set SSL/TLS to **Full (strict)**.
- The frontend auto-reconnects when Cloudflare interrupts the long-lived stream connection (expected behaviour).
- Firewall rules should allow only Cloudflare IP ranges to reach the origin.

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| `/stream` returns 500 | `STREAM_URL` set? Printer reachable? |
| `upstream_connected: false` in `/health` | Check `/status` for `last_error`; verify printer is on |
| Stream freezes / shows stale frame | Click Reconnect; check `reconnect_count` in `/status` |
| High memory | Shouldn't happen — one upstream; check viewer count in `/status` |
| `FFmpeg stderr: Option tls_verify not found.` | Old cached process/config; current app no longer passes `-tls_verify` |

```bash
# Test printer endpoint directly from inside the container:
curl -v http://PRINTER_IP:PORT/stream
```

## Known Limitations

- **MJPEG only**: transport is HTTP MJPEG. If Cloudflare proves problematic for long-lived connections, Phase 3 would add WebSocket or WebRTC delivery (same backend, same UI path).
- **Single-instance only**: fan-out is in-process; horizontal scaling would require a shared pub/sub broker.
- **No auth in this app**: authentication is delegated to the Cloudflare / ingress layer.

## License

MIT
