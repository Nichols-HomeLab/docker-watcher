#!/usr/bin/env python3
"""
docker-watcher
- DOWN_GRACE_SEC (default 60s): wait this long after exit before "down".
- CHECK_PING_EVERY default 60s.
- No image tag in names.
- Terse SMTP messages (e.g., "Gitea container is down at ...").
- Exponential backoff for alerts.
- NEW: MAX_LOOP_ALERTS (default 3): stop loop alerts after N until recovery.
"""

import os
import time
import smtplib
import socket
from email.message import EmailMessage
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import docker

ENV = lambda k, d=None: os.environ.get(k, d)

# ---- Configuration (env) ----
SMTP_HOST           = ENV("SMTP_HOST", "mail")
SMTP_PORT           = int(ENV("SMTP_PORT", "25"))
SMTP_FROM           = ENV("SMTP_FROM", "docker-watcher@localhost")
SMTP_TO             = [x.strip() for x in ENV("SMTP_TO", "root@localhost").split(",") if x.strip()]
SMTP_TLS            = ENV("SMTP_TLS", "0") in ("1", "true", "True", "yes", "on")
SMTP_USER           = ENV("SMTP_USER", "") or None
SMTP_PASS           = ENV("SMTP_PASS", "") or None
SMTP_TIMEOUT        = float(ENV("SMTP_TIMEOUT", "15"))

# Restart loop detection
RESTARTS_IN_WINDOW  = int(ENV("RESTARTS_IN_WINDOW", "3"))
RESTART_WINDOW_SEC  = int(ENV("RESTART_WINDOW_SEC", "60"))

# Exponential backoff (per container) for repeated alerts
BACKOFF_BASE_SEC    = int(ENV("BACKOFF_BASE_SEC", "60"))
BACKOFF_MAX_SEC     = int(ENV("BACKOFF_MAX_SEC", "3600"))

# Loop alert suppression cap
MAX_LOOP_ALERTS     = int(ENV("MAX_LOOP_ALERTS", "3"))

# General behavior
INCLUDE_RECOVERY    = ENV("INCLUDE_RECOVERY", "1") in ("1", "true", "True", "yes", "on")

# Ping cadence & down grace
CHECK_PING_EVERY    = int(ENV("CHECK_PING_EVERY", "60"))     # default 60s
DOWN_GRACE_SEC      = int(ENV("DOWN_GRACE_SEC", "60"))       # default 60s

TZ_STR              = ENV("TZ", "UTC")
HOSTNAME            = ENV("WATCHER_HOSTNAME", socket.gethostname())

def now_utc():
    return datetime.now(timezone.utc)

def fmt_ts(dt: datetime) -> str:
    try:
        import pytz  # optional
        tz = pytz.timezone(TZ_STR)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def log(msg: str):
    print(f"[{fmt_ts(now_utc())}] {msg}", flush=True)

def send_email(subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(SMTP_TO)
    msg["Subject"] = subject
    msg.set_content(body)

    log(f"SMTP -> {SMTP_HOST}:{SMTP_PORT} tls={SMTP_TLS} to={SMTP_TO}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
        if SMTP_TLS:
            s.starttls()
        if SMTP_USER and SMTP_PASS:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

def short_id(cid: str) -> str:
    return cid[:12] if cid else ""

def container_display_name(container):
    # Only the container name; no image/tag.
    return (container.name or "").lstrip("/")

class Notifier:
    def __init__(self):
        self.client = docker.from_env()
        self.low_client = self.client.api

        # id -> "running"|"exited"
        self.container_state = {}

        # loop detection
        self.restarts = defaultdict(lambda: deque(maxlen=64))

        # backoff/mute
        self.mute_until = defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))
        self.backoff_level = defaultdict(int)

        # down grace + single-down alert
        self.down_since = {}          # id -> first seen exited at
        self.down_alerted = set()     # ids already down-alerted until recovery

        # loop alert capping
        self.loop_alerts_sent = defaultdict(int)  # id -> count of loop alerts sent
        self.loop_alerts_suppressed = set()       # ids suppressed until recovery

        # docker daemon state
        self.docker_up = None

    def _in_backoff(self, cid: str) -> bool:
        return now_utc() < self.mute_until[cid]

    def _bump_backoff(self, cid: str):
        lvl = self.backoff_level[cid]
        delay = min(BACKOFF_BASE_SEC * (2 ** max(lvl, 0)), BACKOFF_MAX_SEC)
        self.mute_until[cid] = now_utc() + timedelta(seconds=delay)
        self.backoff_level[cid] = min(lvl + 1, 30)

    def _reset_backoff(self, cid: str):
        self.mute_until[cid] = datetime.min.replace(tzinfo=timezone.utc)
        self.backoff_level[cid] = 0

    def _notify_once(self, subject: str, body: str):
        try:
            send_email(subject, body)
        except Exception as e:
            log(f"ERROR sending email: {e}")

    def _notify_down(self, container):
        cid = container.id
        if self._in_backoff(cid):
            log(f"Muted DOWN alert for {container_display_name(container)} (backoff active)")
            return
        name = container_display_name(container)
        ts = fmt_ts(now_utc())
        subject = f"{name} container is down at {ts}"
        body = f"{name} container is down at {ts} on {HOSTNAME}."
        self._notify_once(subject, body)
        self._bump_backoff(cid)
        self.down_alerted.add(cid)

    def _notify_loop(self, container, count: int, window_sec: int):
        cid = container.id

        # Respect hard cap
        if cid in self.loop_alerts_suppressed:
            log(f"Loop alerts suppressed for {container_display_name(container)} (max reached)")
            return
        if self.loop_alerts_sent[cid] >= MAX_LOOP_ALERTS:
            self.loop_alerts_suppressed.add(cid)
            log(f"Reached MAX_LOOP_ALERTS={MAX_LOOP_ALERTS} for {container_display_name(container)}; suppressing until recovery")
            return

        if self._in_backoff(cid):
            log(f"Muted LOOP alert for {container_display_name(container)} (backoff active)")
            return

        name = container_display_name(container)
        ts = fmt_ts(now_utc())
        subject = f"{name} is restarting frequently ({count} times ~{window_sec}s) at {ts}"
        body = (
            f"{name} is restarting frequently ({count} restarts within ~{window_sec}s) at {ts} on {HOSTNAME}.\n"
            f"Further alerts will use exponential backoff and stop entirely after {MAX_LOOP_ALERTS} loop alerts until recovery."
        )
        self._notify_once(subject, body)
        self._bump_backoff(cid)
        self.loop_alerts_sent[cid] += 1

        # If this send hit the cap, mark suppressed for any subsequent attempts
        if self.loop_alerts_sent[cid] >= MAX_LOOP_ALERTS:
            self.loop_alerts_suppressed.add(cid)

    def _notify_up(self, container):
        cid = container.id
        name = container_display_name(container)
        ts = fmt_ts(now_utc())
        subject = f"{name} container is back up at {ts}"
        body = f"{name} container is back up at {ts} on {HOSTNAME}."
        self._notify_once(subject, body)
        # Reset all suppression/backoff on recovery
        self._reset_backoff(cid)
        self.down_since.pop(cid, None)
        self.down_alerted.discard(cid)
        self.loop_alerts_sent[cid] = 0
        self.loop_alerts_suppressed.discard(cid)

    def _notify_docker_state(self, up: bool):
        state = "UP" if up else "DOWN"
        subject = f"Docker daemon is {state.lower()} at {fmt_ts(now_utc())}"
        body = f"Docker daemon is {state.lower()} on {HOSTNAME} at {fmt_ts(now_utc())}."
        self._notify_once(subject, body)

    def _seed_states(self):
        for c in self.client.containers.list(all=True):
            st = (c.status or "").lower()
            self.container_state[c.id] = "running" if st == "running" else "exited"
            if st != "running":
                self.down_since[c.id] = now_utc()
        log(f"Seeded {len(self.container_state)} container states.")

    def _check_docker_ping(self):
        try:
            self.low_client.ping()
            if self.docker_up is False or self.docker_up is None:
                if self.docker_up is False:
                    self._notify_docker_state(True)
                self.docker_up = True
        except Exception:
            if self.docker_up in (True, None):
                self._notify_docker_state(False)
                self.docker_up = False

    def _maybe_fire_down_after_grace(self, container):
        cid = container.id
        started = self.down_since.get(cid)
        if started is None:
            self.down_since[cid] = now_utc()
            return
        if cid in self.down_alerted:
            return
        if (now_utc() - started).total_seconds() >= DOWN_GRACE_SEC:
            self._notify_down(container)

    def _handle_event(self, ev: dict):
        if ev.get("Type") != "container":
            return

        cid = ev.get("id")
        action = ev.get("Action") or ev.get("status") or ""
        if not cid:
            return

        try:
            container = self.client.containers.get(cid)
        except Exception:
            return

        # Loop detection
        if action in ("die", "oom", "kill", "stop"):
            self.restarts[cid].append(now_utc())
            window_start = now_utc() - timedelta(seconds=RESTART_WINDOW_SEC)
            recent = [t for t in self.restarts[cid] if t >= window_start]
            if len(recent) >= RESTARTS_IN_WINDOW:
                self._notify_loop(container, len(recent), RESTART_WINDOW_SEC)

        # State transitions
        st = (container.status or "").lower()
        prev = self.container_state.get(cid)
        current = "running" if st == "running" else "exited"

        if prev is None:
            self.container_state[cid] = current
            if current == "exited":
                self.down_since[cid] = now_utc()
            return

        if prev == "running" and current == "exited":
            self._maybe_fire_down_after_grace(container)
        elif prev == "exited" and current == "exited":
            self._maybe_fire_down_after_grace(container)
        elif prev == "exited" and current == "running":
            if INCLUDE_RECOVERY:
                self._notify_up(container)
            else:
                # Even if not notifying, clear all state on recovery
                self._reset_backoff(cid)
                self.down_since.pop(cid, None)
                self.down_alerted.discard(cid)
                self.loop_alerts_sent[cid] = 0
                self.loop_alerts_suppressed.discard(cid)

        self.container_state[cid] = current

    def run(self):
        self._seed_states()
        self._check_docker_ping()
        last_ping = time.time()

        log("Listening for Docker events...")
        events = self.low_client.events(decode=True)
        while True:
            if time.time() - last_ping >= CHECK_PING_EVERY:
                self._check_docker_ping()
                last_ping = time.time()
                # Periodic sweep for grace elapse
                try:
                    for c in self.client.containers.list(all=True):
                        if (c.status or "").lower() != "running":
                            self._maybe_fire_down_after_grace(c)
                except Exception as e:
                    log(f"Periodic sweep error: {e}")

            try:
                ev = next(events)
            except StopIteration:
                log("Event stream ended, reconnecting in 3s...")
                time.sleep(3)
                events = self.low_client.events(decode=True)
                continue
            except Exception as e:
                log(f"Event stream error: {e}; retrying in 3s...")
                time.sleep(3)
                events = self.low_client.events(decode=True)
                continue

            try:
                self._handle_event(ev or {})
            except Exception as e:
                log(f"ERROR handling event: {e}")

def main():
    log("Starting docker-watcher")
    try:
        Notifier().run()
    except KeyboardInterrupt:
        log("Shutting down (KeyboardInterrupt)")
    except Exception as e:
        log(f"FATAL: {e}")
        time.sleep(2)

if __name__ == "__main__":
    main()
