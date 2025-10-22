# docker-watcher

A tiny Python app to monitor Docker for containers that go **down** or get stuck in a **restart loop**, then notify via **SMTP**.

## What it does

- Watches Docker events (`start`, `stop`, `die`, `oom`) through the Docker socket.
- Sends **one** alert when a container transitions from `running` → `exited`.
- Detects restart loops: **N restarts within T seconds** ⇒ sends a single "RESTART LOOP" alert.
- Uses **exponential backoff** per container to mute repeated alerts if the container keeps flapping.
- Optionally notifies when a container recovers (goes `exited` → `running`).
- Also monitors the **Docker daemon** status and alerts when the daemon goes **down**/**up**.

## Config (env vars)

| Variable | Default | Notes |
|---------|---------|-------|
| `SMTP_HOST` | `mail` | SMTP relay host |
| `SMTP_PORT` | `25` | SMTP port |
| `SMTP_FROM` | `docker-watcher@localhost` | From address |
| `SMTP_TO` | `root@localhost` | Comma-separated recipient list |
| `SMTP_TLS` | `0` | `1` to use STARTTLS |
| `SMTP_USER` | *(empty)* | SMTP auth (optional) |
| `SMTP_PASS` | *(empty)* | SMTP auth (optional) |
| `RESTARTS_IN_WINDOW` | `3` | Loop detection threshold |
| `RESTART_WINDOW_SEC` | `60` | Loop detection window (seconds) |
| `BACKOFF_BASE_SEC` | `60` | Mute duration on first alert |
| `BACKOFF_MAX_SEC` | `3600` | Max mute duration |
| `INCLUDE_RECOVERY` | `1` | Notify when container comes back up |
| `INCLUDE_IMAGE` | `1` | Include image tag in container name |
| `CHECK_PING_EVERY` | `10` | Seconds between Docker ping checks |
| `WATCHER_HOSTNAME` | *(container hostname)* | Display name in alerts |
| `TZ` | `UTC` | Timezone for timestamps |

## Build & run

```bash
docker build -t docker-watcher .
docker run -d --name docker-watcher \
  -e SMTP_HOST=mail -e SMTP_PORT=25 \
  -e SMTP_FROM=docker-watcher@yourdomain \
  -e SMTP_TO=alerts@yourdomain \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  docker-watcher
```

### Prebuilt image (GHCR)

`main` pushes trigger the [`build-and-release`](.github/workflows/build-and-release.yml) workflow, which publishes a multi-arch image to GHCR and makes the package public.

```bash
docker pull ghcr.io/<owner>/<repo>:latest
docker pull ghcr.io/<owner>/<repo>:<short-sha>
docker pull ghcr.io/<owner>/<repo>:vX.Y.Z
```

Replace `<owner>/<repo>` with the lowercase GitHub namespace and repository name (for example, `ghcr.io/nichols-homelab/docker-watcher`).

Or with Compose:

```yaml
# docker-compose.yml (see this repo's example)
services:
  docker-watcher:
    build: .
    restart: unless-stopped
    environment:
      SMTP_HOST: "mail"
      SMTP_FROM: "docker-watcher@yourdomain"
      SMTP_TO: "alerts@yourdomain"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

## Notes

- Mount `/var/run/docker.sock` **read-only**.
- If you're relaying to **ntfy** via SMTP, set your relay's host/port and recipient to the ntfy SMTP endpoint/alias your relay uses.
- Backoff doubles each time a container triggers an alert, up to `BACKOFF_MAX_SEC`. Backoff resets when a container starts and runs healthily again.
- This app does not *stop* or *restart* containers; it only reports. 
