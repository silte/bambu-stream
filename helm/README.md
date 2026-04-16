# Bambu Stream Helm Chart

Helm wrapper chart using:

```yaml
dependencies:
    - name: nodejs
        alias: application
        version: v1.1.0
        repository: https://charts.silte.fi
```

Application values are configured under `application.*`.

## Prerequisites

1. Build and publish the Docker image (default values expect GHCR).
2. Create a Kubernetes namespace (or use `--create-namespace`).
3. Create secret values for MQTT auto-discovery (preferred):
   - `MQTT_SERIAL`
   - `MQTT_ACCESS_CODE`
   - `MQTT_HOST`
   - `MQTT_TLS_INSECURE`
   - `MQTT_TLS_CA_CERT`

Example secret:

```bash
kubectl create secret generic bambu-stream-secrets \
    --from-literal=MQTT_SERIAL='<printer-serial>' \
    --from-literal=MQTT_ACCESS_CODE='<access-code>' \
    --from-literal=MQTT_HOST='<printer-ip>' \
    --from-literal=MQTT_TLS_INSECURE='false' \
    --from-literal=MQTT_TLS_CA_CERT='/app/bambu_p2s_250626.cert' \
    --namespace bambu-stream
```

With these keys present, the app can discover `STREAM_URL` from MQTT at startup.

## Install or Upgrade

```bash
helm dependency update ./helm

helm upgrade --install bambu-stream ./helm \
    --namespace bambu-stream \
    --create-namespace
```

## Common Overrides

Set image repository and tag:

```bash
helm upgrade --install bambu-stream ./helm \
    --namespace bambu-stream \
    --create-namespace \
    --set application.image.registry=ghcr.io \
    --set application.image.repository=<owner>/bambu-stream \
    --set application.image.tag=<tag>
```

Enable ingress:

```bash
helm upgrade --install bambu-stream ./helm \
    --namespace bambu-stream \
    --set application.ingress.enabled=true \
    --set application.ingress.host=bambu.example.com
```

Use secret-based environment variables via `values.yaml` (`application.secrets`):

```yaml
application:
  secrets:
    - bambu-stream-secrets
```
