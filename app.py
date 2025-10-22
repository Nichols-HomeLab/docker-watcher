#!/usr/bin/env python3
"""
docker-watcher

Behavior highlights
- Global sweep of all containers every SWEEP_ALL_EVERY_SEC (default 10s).
- When a container is first seen DOWN, wait DOWN_RECHECK_SEC (default 60s) before starting notifications.
- DOWN_GRACE_SEC (default 60s) still applies as a minimal grace before first "down" alert.
- Per-container exponential backoff for repeated alerts.
- Per-container notification COUNT window: allow up to MAX_NOTIFIES_IN_WINDOW within NOTIFY_WINDOW_SEC; once reached, mute
  until the container has been RUNNING for at least RECOVERY_QUIET_SEC.
- Restart-loop alerts: fire once per DOWN cycle; suppressed until recovery.
- Uses docker.from_env().api for the low-level client.
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

# ---- SMTP ----
SMTP_HOST           = ENV("SMTP_HOST", "mail")
SMTP_PORT           = int(ENV("SMTP_PORT", "25"))
SMTP_FROM           = ENV("SMTP_FROM", "docker-watcher@localhost")
SMTP_TO             = [x.strip() for x in ENV("SMTP_TO", "root@localhost").split(",") if x.strip()]
SMTP_TLS            = ENV("SMTP_TLS", "0").lower() in ("1", "true", "yes", "on")
SMTP_USER           = ENV("SMTP_USER", "") or None
SMTP_PASS           = ENV("SMTP_PASS", "") or None
SMTP_TIMEOUT        = float(ENV("SMTP_TIMEOUT", "15"))

# ---- Loop detection ----
RESTARTS_IN_WINDOW  = int(ENV("RESTARTS_IN_WINDOW", "3"))
RESTART_WINDOW_SEC  = int(ENV("RESTART_WINDOW_SEC", "60"))

# ---- Backoff ----
BACKOFF_BASE_SEC    = int(ENV("BACKOFF_BASE_SEC", "60"))
BACKOFF_MAX_SEC     = int(ENV("BACKOFF_MAX_SEC", "3600"))

# ---- Behavior ----
INCLUDE_RECOVERY    = ENV("INCLUDE_RECOVERY", "1").lower() in ("1", "true", "yes", "on")
CHECK_PING_EVERY    = int(ENV("CHECK_PING_EVERY", "60"))
DOWN_GRACE_SEC      = int(ENV("DOWN_GRACE_SEC", "60"))

# ---- New caps / cadence ----
NOTIFY_WINDOW_SEC       = int(ENV("NOTIFY_WINDOW_SEC", "3600"))   # 1h
MAX_NOTIFIES_IN_WINDOW  = int(ENV("MAX_NOTIFIES_IN_WINDOW", "3"))
RECOVERY_QUIET_SEC      = int(ENV("RECOVERY_QUIET_SEC", "600"))   # 10m
SWEEP_ALL_EVERY_SEC     = int(ENV("SWEEP_ALL_EVERY_SEC", "10"))   # 10s
DOWN_RECHECK_SEC        = int(ENV("DOWN_RECHECK_SEC", "60"))      # 60s

TZ_STR              = ENV("TZ", "UTC")
HOSTNAME            = ENV("WATCHER_HOSTNAME", socket.gethostname())

def now_utc():
    return datetime.now(timezone.utc)

def fmt_ts(dt: datetime) -> str:
    try:
        import pytz
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

def container_display_name(container):
    return (container.name or "").lstrip("/")

class Notifier:
    def __init__(self):
        self.client = docker.from_env()
        self.low = self.client.api  # reuse configured low-level client

        # Core state
        self.container_state = {}            # cid -> "running"|"exited"
        self.down_since = {}                 # cid -> first time seen exited
        self.next_down_recheck_at = {}       # cid -> earliest time to begin alerts after first down
        self.last_up_at = {}                 # cid -> last time seen running

        # Alerts & suppression
        self.down_alerted = set()            # cid set: we've fired the initial down alert for this cycle
        self.loop_suppressed = set()         # cid set: loop alert muted until recovery
        self.muted_by_cap = set()            # cid set: muted due to hitting MAX_NOTIFIES_IN_WINDOW

        # Backoff
        self.mute_until = defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))
        self.backoff_level = defaultdict(int)

        # Notification history (timestamps) for cap window
        self.notif_history = defaultdict(lambda: deque(maxlen=256))  # cid -> deque[datetime]

        # Loop detection history
        self.restarts = defaultdict(lambda: deque(maxlen=128))       # cid -> deque[datetime]

        # Docker daemon up/down
        self.docker_up = None

    # ---------- helpers ----------
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

    def _below_cap(self, cid: str) -> bool:
        """Return True if container can send another alert within window."""
        if cid in self.muted_by_cap:
            return False
        cutoff = now_utc() - timedelta(seconds=NOTIFY_WINDOW_SEC)
        dq = self.notif_history[cid]
        # prune old
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) < MAX_NOTIFIES_IN_WINDOW

    def _record_sent(self, cid: str):
        self.notif_history[cid].append(now_utc())
        # After appending, if we hit the cap, flip muted_by_cap
        if not self._below_cap(cid):
            self.muted_by_cap.add(cid)
            log(f"[CAP] Muted further alerts for {cid[:12]} until recovery quiet period.")

    def _can_unmute_by_cap(self, cid: str) -> bool:
        """True if container has been RUNNING for >= RECOVERY_QUIET_SEC."""
        up_at = self.last_up_at.get(cid)
        return bool(up_at and (now_utc() - up_at).total_seconds() >= RECOVERY_QUIET_SEC)

    def _notify_once(self, subject: str, body: str, cid: str | None = None):
        try:
            send_email(subject, body)
            if cid:
                self._record_sent(cid)
        except Exception as e:
            log(f"ERROR sending email: {e}")

    # ---------- notifications ----------
    def _notify_down(self, container):
        cid = container.id
        if cid in self.muted_by_cap and not self._can_unmute_by_cap(cid):
            log(f"[CAP] Down alert muted for {container_display_name(container)} (awaiting recovery quiet period)")
            return
        if self._in_backoff(cid):
            log(f"[BACKOFF] Down alert muted for {container_display_name(container)}")
            return
        name = container_display_name(container)
        ts = fmt_ts(now_utc())
        subject = f"{name} container is down at {ts}"
        body = f"{name} container is down at {ts} on {HOSTNAME}."
        self._notify_once(subject, body, cid)
        self._bump_backoff(cid)
        self.down_alerted.add(cid)

    def _notify_loop(self, container, count: int, window_sec: int):
        cid = container.id
        if cid in self.loop_suppressed:
            return
        if cid in self.muted_by_cap and not self._can_unmute_by_cap(cid):
            log(f"[CAP] Loop alert muted for {container_display_name(container)} (awaiting recovery quiet period)")
            return
        if self._in_backoff(cid):
            log(f"[BACKOFF] Loop alert muted for {container_display_name(container)}")
            return
        name = container_display_name(container)
        ts = fmt_ts(now_utc())
        subject = f"{name} is restarting frequently ({count} times ~{window_sec}s) at {ts}"
        body = (
            f"{name} is restarting frequently ({count} restarts within ~{window_sec}s) at {ts} on {HOSTNAME}.\n"
            "Further loop alerts are suppressed until the container comes back up."
        )
        self._notify_once(subject, body, cid)
        self.loop_suppressed.add(cid)

    def _notify_up(self, container):
        cid = container.id
        name = container_display_name(container)
        ts = fmt_ts(now_utc())
        if INCLUDE_RECOVERY:
            subject = f"{name} container is back up at {ts}"
            body = f"{name} container is back up at {ts} on {HOSTNAME}."
            self._notify_once(subject, body, cid=None)  # recovery doesn't count against cap
        # reset state on recovery
        self._reset_backoff(cid)
        self.down_since.pop(cid, None)
        self.down_alerted.discard(cid)
        self.loop_suppressed.discard(cid)
        # If we were muted by cap, keep it until RECOVERY_QUIET_SEC has elapsed; we’ll clear below on sustained UP.

    def _maybe_clear_cap_on_sustained_up(self, cid: str):
        if cid in self.muted_by_cap and self._can_unmute_by_cap(cid):
            self.muted_by_cap.discard(cid)
            # also clear history window to avoid immediate re-cap
            self.notif_history[cid].clear()
            log(f"[CAP] Unmuted alerts for {cid[:12]} after sustained recovery.")

    def _notify_docker_state(self, up: bool):
        state = "UP" if up else "DOWN"
        subject = f"Docker daemon is {state.lower()} at {fmt_ts(now_utc())}"
        body = f"Docker daemon is {state.lower()} on {HOSTNAME} at {fmt_ts(now_utc())}."
        self._notify_once(subject, body, cid=None)

    # ---------- event & sweep ----------
    def _seed_states(self):
        for c in self.client.containers.list(all=True):
            st = (c.status or "").lower()
            cur = "running" if st == "running" else "exited"
            self.container_state[c.id] = cur
            if cur == "running":
                self.last_up_at[c.id] = now_utc()
            else:
                self.down_since[c.id] = now_utc()
                self.next_down_recheck_at[c.id] = now_utc() + timedelta(seconds=DOWN_RECHECK_SEC)
        log(f"Seeded {len(self.container_state)} container states.")

    def _check_docker_ping(self):
        try:
            self.low.ping()
            if self.docker_up is False or self.docker_up is None:
                if self.docker_up is False:
                    self._notify_docker_state(True)
                self.docker_up = True
        except Exception:
            if self.docker_up in (True, None):
                self._notify_docker_state(False)
                self.docker_up = False

    def _should_begin_down_alerts(self, cid: str) -> bool:
        # Must satisfy both: grace period and recheck delay after initial down
        t0 = self.down_since.get(cid)
        if not t0:
            return False
        if (now_utc() - t0).total_seconds() < DOWN_GRACE_SEC:
            return False
        re_at = self.next_down_recheck_at.get(cid, t0 + timedelta(seconds=DOWN_RECHECK_SEC))
        return now_utc() >= re_at

    def _maybe_fire_down_path(self, container):
        cid = container.id
        # If we haven't set timers, set them now
        if cid not in self.down_since:
            self.down_since[cid] = now_utc()
            self.next_down_recheck_at[cid] = now_utc() + timedelta(seconds=DOWN_RECHECK_SEC)
            return
        # If initial DOWN alert already sent, nothing else here
        if cid in self.down_alerted:
            return
        # If we can start alerts now, do it
        if self._should_begin_down_alerts(cid):
            self._notify_down(container)

    def _handle_event(self, ev: dict):
        if ev.get("Type") != "container":
            return

        cid = ev.get("id")
        action = ev.get("Action") or ev.get("status") or ""
        if not cid:
            return

        try:
            c = self.client.containers.get(cid)
        except Exception:
            return

        # loop detection on stop/die/oom
        if action in ("die", "oom", "kill", "stop"):
            self.restarts[cid].append(now_utc())
            window_start = now_utc() - timedelta(seconds=RESTART_WINDOW_SEC)
            recent = [t for t in self.restarts[cid] if t >= window_start]
            if len(recent) >= RESTARTS_IN_WINDOW:
                self._notify_loop(c, len(recent), RESTART_WINDOW_SEC)

        # state transition
        st = (c.status or "").lower()
        prev = self.container_state.get(cid)
        cur = "running" if st == "running" else "exited"

        # update last_up_at and maybe clear cap if sustained up
        if cur == "running":
            self.last_up_at[cid] = now_utc()
            self._maybe_clear_cap_on_sustained_up(cid)

        if prev is None:
            self.container_state[cid] = cur
            if cur == "exited":
                self.down_since[cid] = now_utc()
                self.next_down_recheck_at[cid] = now_utc() + timedelta(seconds=DOWN_RECHECK_SEC)
            return

        if prev == "running" and cur == "exited":
            # mark down, start timers
            self.down_since[cid] = now_utc()
            self.next_down_recheck_at[cid] = now_utc() + timedelta(seconds=DOWN_RECHECK_SEC)
            self._maybe_fire_down_path(c)

        elif prev == "exited" and cur == "exited":
            self._maybe_fire_down_path(c)

        elif prev == "exited" and cur == "running":
            # recovery
            self._notify_up(c)
            # note: cap stays until RECOVERY_QUIET_SEC has elapsed; handled by sustained-up checker

        self.container_state[cid] = cur

    # ---------- main run ----------
    def run(self):
        self._seed_states()
        self._check_docker_ping()
        last_ping = time.time()
        last_sweep = 0.0

        log("Listening for Docker events...")
        events = self.low.events(decode=True)
        while True:
            now = time.time()

            # ping docker daemon
            if now - last_ping >= CHECK_PING_EVERY:
                self._check_docker_ping()
                last_ping = now

            # periodic sweep of all containers
            if now - last_sweep >= SWEEP_ALL_EVERY_SEC:
                try:
                    for c in self.client.containers.list(all=True):
                        st = (c.status or "").lower()
                        cid = c.id
                        cur = "running" if st == "running" else "exited"
                        prev = self.container_state.get(cid)

                        # track up time and maybe clear cap
                        if cur == "running":
                            self.last_up_at[cid] = now_utc()
                            self._maybe_clear_cap_on_sustained_up(cid)

                        if prev is None:
                            self.container_state[cid] = cur
                            if cur == "exited":
                                self.down_since[cid] = now_utc()
                                self.next_down_recheck_at[cid] = now_utc() + timedelta(seconds=DOWN_RECHECK_SEC)
                            continue

                        if prev == "running" and cur == "exited":
                            self.down_since[cid] = now_utc()
                            self.next_down_recheck_at[cid] = now_utc() + timedelta(seconds=DOWN_RECHECK_SEC)
                            self._maybe_fire_down_path(c)
                        elif prev == "exited" and cur == "exited":
                            self._maybe_fire_down_path(c)
                        elif prev == "exited" and cur == "running":
                            self._notify_up(c)

                        self.container_state[cid] = cur
                except Exception as e:
                    log(f"Sweep error: {e}")
                finally:
                    last_sweep = now

            # event processing
            try:
                ev = next(events)
            except StopIteration:
                log("Event stream ended, reconnecting in 3s...")
                time.sleep(3)
                events = self.low.events(decode=True)
                continue
            except Exception as e:
                log(f"Event stream error: {e}; retrying in 3s...")
                time.sleep(3)
                events = self.low.events(decode=True)
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
