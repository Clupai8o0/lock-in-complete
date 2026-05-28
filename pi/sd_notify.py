"""Minimal sd_notify(3) client — no external dependency.

systemd sets NOTIFY_SOCKET in the environment when the unit has
`Type=notify`. We just push messages into that AF_UNIX socket.

Used for:
- READY=1 on startup (so systemd marks the unit "active" only after the
  orchestrator has finished bringing up the FSM, DB, serial reader, etc.)
- WATCHDOG=1 every ~30s (paired with WatchdogSec=120 in the unit so a
  hung orchestrator is killed and restarted).
"""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("lockin.sd_notify")


def notify(message: str) -> bool:
    """Send a single sd_notify message. Returns True if the socket
    delivered the message; False if NOTIFY_SOCKET isn't set (not under
    systemd) or sending failed."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Linux abstract socket: "@/foo" -> "\0/foo"
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), addr)
        return True
    except OSError as e:
        log.warning("sd_notify failed: %s", e)
        return False


def ready() -> bool:
    return notify("READY=1")


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def stopping() -> bool:
    return notify("STOPPING=1")
