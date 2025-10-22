# docker-watcher

A tiny Python app to monitor Docker for containers that go **down** (with a configurable grace period) or get stuck in a **restart loop**, then notify via **SMTP**.

## What it does

- Watches Docker events (`start`, `stop`, `die`, `oom`) through the Docker socket.
- Sends a single alert when a container has remained `exited` past the grace window (default 60s).
- Detects restart loops: **N restarts within T seconds** ⇒ sends a single alert and suppresses further loop alerts until the container recovers.
- Uses **exponential backoff** per container to mute repeated alerts if the container keeps flapping.
- Optionally notifies when a container recovers (goes `exited` → `running`).
- Also monitors the **Docker daemon** status and alerts when the daemon goes **down**/**up**.
- Produces short, human-readable email subjects/bodies.

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
| `SMTP_TIMEOUT` | `15` | SMTP socket timeout (seconds) |
| `RESTARTS_IN_WINDOW` | `3` | Loop detection threshold |
| `RESTART_WINDOW_SEC` | `60` | Loop detection window (seconds) |
| `BACKOFF_BASE_SEC` | `60` | Mute duration on first alert |
| `BACKOFF_MAX_SEC` | `3600` | Max mute duration |
| `INCLUDE_RECOVERY` | `1` | Notify when container comes back up |
| `CHECK_PING_EVERY` | `60` | Seconds between Docker ping checks |
| `DOWN_GRACE_SEC` | `60` | Seconds a container must stay down before alerting |
| `WATCHER_HOSTNAME` | *(container hostname)* | Display name in alerts |
| `TZ` | `UTC` | Timezone for timestamps |

## Build & run

```bash
docker build -t docker-watcher .
docker run -d --name docker-watcher \
  -e SMTP_HOST=mail -e SMTP_PORT=25 \
  -e SMTP_FROM=docker-watcher@yourdomain \
  -e SMTP_TO=alerts@yourdomain \
  -e SMTP_TLS=1 \
  -e SMTP_USER=mailer \
  -e SMTP_PASS=secretpass \
  -e SMTP_TIMEOUT=15 \
  -e DOWN_GRACE_SEC=60 \
  -e RESTARTS_IN_WINDOW=3 \
  -e RESTART_WINDOW_SEC=60 \
  -e BACKOFF_BASE_SEC=60 \
  -e BACKOFF_MAX_SEC=3600 \
  -e INCLUDE_RECOVERY=1 \
  -e CHECK_PING_EVERY=60 \
  -e WATCHER_HOSTNAME=$(hostname) \
  -e TZ=UTC \
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
      SMTP_PORT: "25"
      SMTP_TLS: "1"
      SMTP_USER: "mailer"
      SMTP_PASS: "secretpass"
      SMTP_TIMEOUT: "15"
      DOWN_GRACE_SEC: "60"
      RESTARTS_IN_WINDOW: "3"
      RESTART_WINDOW_SEC: "60"
      BACKOFF_BASE_SEC: "60"
      BACKOFF_MAX_SEC: "3600"
      INCLUDE_RECOVERY: "1"
      CHECK_PING_EVERY: "60"
      WATCHER_HOSTNAME: "$(hostname)"
      TZ: "UTC"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

## Notes

- Mount `/var/run/docker.sock` **read-only**.
- If you're relaying to **ntfy** via SMTP, set your relay's host/port and recipient to the ntfy SMTP endpoint/alias your relay uses.
- Backoff doubles each time a container triggers an alert, up to `BACKOFF_MAX_SEC`. Backoff resets when a container starts and runs healthily again.
- Loop alerts fire at most once per down cycle; they resume only after the container runs again.
- This app does not *stop* or *restart* containers; it only reports. 
