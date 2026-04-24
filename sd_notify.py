"""Minimal sd_notify(3) implementation.

Sends notifications to systemd via $NOTIFY_SOCKET. No external deps.
All functions are no-ops when the socket is not set (i.e. running outside systemd).
"""
import os
import socket


def _send(message: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(message.encode("utf-8"), addr)
            return True
        finally:
            sock.close()
    except OSError:
        return False


def ready() -> bool:
    """Tell systemd the service is ready (Type=notify)."""
    return _send("READY=1")


def watchdog() -> bool:
    """Send watchdog ping (must be called within WatchdogSec interval)."""
    return _send("WATCHDOG=1")


def status(text: str) -> bool:
    """Set service status text shown in systemctl status."""
    return _send(f"STATUS={text}")


def stopping() -> bool:
    """Tell systemd the service is shutting down."""
    return _send("STOPPING=1")
