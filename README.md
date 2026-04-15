# Bambu P2S Live Stream App

A small, self-hosted web app that securely serves a Bambu P2S camera stream through a public HTTPS subdomain behind Cloudflare, maintaining minimal and stable printer connections.

## Architecture

```
Bambu P2S Printer
    ↓
Our Stream App Backend (single upstream connection)
    ↓
Reverse Proxy (Cloudflare/Ingress)
    ↓
Remote Browser (multiple viewers)
```

**Design principle**: The app proxies one upstream stream to any number of viewers, avoiding multiple printer-side connections.

## Features

- **Minimal**: ~200 lines of Python, no complex dependencies
- **Cloudflare-friendly**: Proper no-cache headers, handles reconnects gracefully
- **Stateless**: Containerized, no persistent session state
- **Simple UI**: Built-in HTML5 player with auto-reconnect
- **Robust logging**: Clear visibility into connection state

## Quick Start

### Local Development

1. **Clone and setup:**

   ```bash
   cd /home/coder/dev/bambu-stream
   python3.11 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run with mock upstream stream** (for testing):

   ```bash
   # Terminal 1: Start a simple test stream server
   # (or point to real printer)
   STREAM_URL=http://YOUR_PRINTER_IP:PORT/stream python app.py
   ```

3. **Access:**
   - Player: `http://localhost:8000/`
   - Health: `http://localhost:8000/health`
   - Stream: `http://localhost:8000/stream`

### Docker

1. **Build:**

   ```bash
   docker build -t bambu-stream:latest .
   ```

2. **Run:**

   ```bash
   docker run -d \
     -p 8000:8000 \
     -e STREAM_URL=http://PRINTER_IP:PORT/stream \
     -e APP_TITLE="Bambu P2S Live" \
     -e LOG_LEVEL=INFO \
     --name bambu-stream \
     bambu-stream:latest
   ```

3. **Check logs:**
   ```bash
   docker logs -f bambu-stream
   ```

## Configuration

### Required Environment Variables

| Variable     | Example                            | Description                                              |
| ------------ | ---------------------------------- | -------------------------------------------------------- |
| `STREAM_URL` | `http://192.168.1.100:8080/stream` | **Required.** Direct URL to printer's streaming endpoint |

### Optional Environment Variables

| Variable                    | Default          | Description                            |
| --------------------------- | ---------------- | -------------------------------------- |
| `APP_TITLE`                 | `Bambu P2S Live` | Title shown in browser tab and page    |
| `PORT`                      | `8000`           | Port to listen on                      |
| `LOG_LEVEL`                 | `INFO`           | Log level: DEBUG, INFO, WARNING, ERROR |
| `TZ`                        | `UTC`            | Timezone for logging                   |
| `RECONNECT_INITIAL_SECONDS` | `1`              | Initial reconnect delay (Phase 2)      |
| `RECONNECT_MAX_SECONDS`     | `30`             | Max reconnect delay (Phase 2)          |
| `AUTO_RECONNECT`            | `true`           | Enable auto-reconnect (Phase 2)        |

### Bambu P2S Stream URL

Find your printer's stream endpoint:

- **Direct MJPEG**: `http://PRINTER_IP:8080/mjpegfeed`
- **Bambu P2S port**: Check your printer's LAN settings
- **go2rtc output** (if using): `http://go2rtc-host:8554/api/frame.mjpg?src=bambu`

## Endpoints

### `GET /`

Main player page with embedded HTML5 viewer and reconnect button.

**Response**: HTML page with:

- Stream player
- Connection status indicator
- Manual reconnect button
- Auto-reconnect on error

### `GET /health`

Health check endpoint.

**Response**:

```json
{
  "ok": true,
  "stream_configured": true,
  "upstream_connected": true,
  "app_version": "0.1.0",
  "uptime_seconds": 123,
  "timestamp": "2025-04-15T10:30:45.123456"
}
```

### `GET /stream`

Live stream endpoint (primary endpoint for viewers).

**Headers**:

- `Cache-Control: no-store, no-cache, must-revalidate, no-transform`
- `Pragma: no-cache`
- `Content-Type: multipart/x-mixed-replace; boundary=frame` (MJPEG)

**Response**: Continuous binary stream from printer

**Error handling**:

- Returns 500 if `STREAM_URL` not configured
- Auto-retries on upstream connection failure
- Frontend auto-reconnects on stream disconnect

### `GET /status` (Optional)

Extended status endpoint for debugging.

**Response**:

```json
{
  "stream_configured": true,
  "stream_url_masked": "http://***@192.168.1.100:8080/stream",
  "uptime_seconds": 456,
  "timestamp": "2025-04-15T10:30:45.123456",
  "app_version": "0.1.0"
}
```

## Logging

The app logs these key events (with no secrets exposed):

- **Startup**: App initialization, configuration status
- **Stream state**: Connection attempts, upstream errors, disconnects
- **Client activity**: Stream requests, errors

Example logs:

```
2025-04-15 10:30:01 - bambu_stream_app - INFO - Bambu P2S Stream App starting
2025-04-15 10:30:01 - bambu_stream_app - INFO - APP_TITLE=Bambu P2S Live
2025-04-15 10:30:01 - stream_manager - INFO - Stream configured: http://***@192.168.1.100:8080/...
2025-04-15 10:30:05 - bambu_stream_app - INFO - Stream request received from client
2025-04-15 10:30:05 - bambu_stream_app - INFO - Connecting to upstream stream: http://***@192.168.1.100:8080/...
2025-04-15 10:30:06 - bambu_stream_app - INFO - Upstream connection established, streaming to client
```

## Deployment with k3s/Ingress

1. **Create ConfigMap** (optional, for env vars):

   ```yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: bambu-stream-config
     namespace: default
   data:
     STREAM_URL: "http://printer-internal-ip:8080/stream"
     APP_TITLE: "Bambu P2S Live"
     LOG_LEVEL: "INFO"
   ```

2. **Create Deployment**:

   ```yaml
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: bambu-stream
     namespace: default
   spec:
     replicas: 1
     selector:
       matchLabels:
         app: bambu-stream
     template:
       metadata:
         labels:
           app: bambu-stream
       spec:
         containers:
           - name: app
             image: bambu-stream:latest
             ports:
               - containerPort: 8000
             envFrom:
               - configMapRef:
                   name: bambu-stream-config
             resources:
               requests:
                 cpu: 100m
                 memory: 128Mi
               limits:
                 cpu: 500m
                 memory: 256Mi
   ```

3. **Create Service**:

   ```yaml
   apiVersion: v1
   kind: Service
   metadata:
     name: bambu-stream
     namespace: default
   spec:
     selector:
       app: bambu-stream
     ports:
       - protocol: TCP
         port: 80
         targetPort: 8000
   ```

4. **Create Ingress** (behind Cloudflare):
   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: Ingress
   metadata:
     name: bambu-stream
     namespace: default
   spec:
     rules:
       - host: printer.example.com
         http:
           paths:
             - path: /
               pathType: Prefix
               backend:
                 service:
                   name: bambu-stream
                   port:
                     number: 80
   ```

## Cloudflare Configuration

Ensure Cloudflare is configured to:

1. **Proxy** the subdomain (e.g., `printer.example.com`)
2. **Allow long-lived connections** (HTTP streaming)
3. **Firewall rules** permit only Cloudflare IP ranges to reach your origin
4. **SSL/TLS**: Full (strict) encryption mode recommended

The app handles session interruptions gracefully—the frontend will auto-reconnect.

## Known Limitations (MVP / Phase 1)

### Current

- Each viewer may create a separate connection to the printer (acceptable for small viewer counts)
- No persistent upstream connection maintained
- Simple MJPEG transport (may need WebSocket/WebRTC in future if Cloudflare issues arise)

### Future Phases (Phase 2+)

- **Phase 2**: Single shared upstream connection with fan-out to all viewers
- **Phase 3**: Optional WebSocket or WebRTC for better Cloudflare compatibility

## Troubleshooting

### `/stream` returns 500

- **Check**: `STREAM_URL` is set and printer endpoint is reachable
- **Command**: `curl -v http://PRINTER_IP:8080/stream` (from the app container)

### Stream shows "Disconnected" in UI

- **Check logs**: `docker logs bambu-stream`
- **Verify**: Printer is online and streaming endpoint is accessible
- **Retry**: Click the "Reconnect" button

### High CPU/memory usage

- **Phase 1 limitation**: Multiple viewers = multiple printer connections
- **Phase 2 will fix**: Implement shared upstream + fan-out

## Development & Contributing

### Testing locally without a printer:

```bash
# 1. Start a dummy MJPEG server (e.g., using mjpeg-streamer or similar)
# OR use a public test stream:
STREAM_URL=http://demo.embedded.com/mjpg/video.mjpg python app.py

# 2. Open http://localhost:8000/
# 3. Verify:
#    - Player loads
#    - Status shows "Connected"
#    - Logs show stream activity
```

### Running tests:

```bash
# Add pytest and run unit tests (future)
# For now, manual testing with curl:
curl -v http://localhost:8000/health
curl -v http://localhost:8000/status
```

## Security

- **No auth in this app**: Auth is handled by your reverse proxy (Cloudflare, Ingress)
- **No secrets logged**: URLs are masked in logs
- **No direct printer exposure**: Only the app is public-facing
- **Cloudflare protection**: All requests go through Cloudflare before reaching your origin

## License

MIT (or choose your own)

## Support

For issues, questions, or contributions:

1. Check logs: `docker logs bambu-stream`
2. Verify `STREAM_URL` is correct
3. Test printer endpoint directly: `curl http://PRINTER_IP:8080/stream`
4. Review this README's troubleshooting section
