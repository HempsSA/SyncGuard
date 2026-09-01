"""
SyncGuard notifications — ntfy.sh push notification client.

Uses only stdlib (urllib) so no extra dependencies are required.
"""

import json
import urllib.request
import urllib.error
from typing import Optional


NTFY_DEFAULT_SERVER = "https://ntfy.sh"
NTFY_DEFAULT_TOPIC = "syncguard-alerts"


def send_notification(
    message: str,
    topic: str = NTFY_DEFAULT_TOPIC,
    title: str = "SyncGuard",
    priority: int = 3,
    tags: Optional[list] = None,
    server: str = NTFY_DEFAULT_SERVER,
    access_token: str = "",
    action_url: str = "",
) -> bool:
    """
    Send a push notification via ntfy.sh.

    Args:
        message:     Notification body text.
        topic:       The ntfy topic to publish to.
        title:       Notification title.
        priority:    1-5 (1=min, 3=default, 5=max).
        tags:        Emoji tags, e.g. ["warning", "sync"].
        server:      ntfy server URL.
        access_token: Bearer token for authenticated topics.
        action_url:  Optional "Open" action URL in the notification.

    Returns:
        True on success, False on failure.
    """
    if not topic:
        return False

    url = server.rstrip("/") + "/" + topic
    payload = {
        "message": message,
        "title": title,
        "priority": priority,
    }
    if tags:
        payload["tags"] = tags
    if action_url:
        payload["actions"] = [
            {"action": "open", "label": "Open SyncGuard", "url": action_url}
        ]

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if access_token:
        req.add_header("Authorization", "Bearer " + access_token)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def send_job_notification(
    job_name: str,
    status: str,
    total: int = 0,
    changed: int = 0,
    pct: float = 0.0,
    duration_s: float = 0.0,
    triggered_by: str = "",
    topic: str = NTFY_DEFAULT_TOPIC,
    access_token: str = "",
    server: str = NTFY_DEFAULT_SERVER,
    notify_ok: bool = True,
    notify_warn: bool = True,
    notify_error: bool = True,
) -> bool:
    """
    Send a notification for a completed job run.

    Respects per-status filter flags (notify_ok / notify_warn / notify_error).

    Returns:
        True if a notification was sent, False if filtered out or failed.
    """
    # Filter by status
    if status == "OK" and not notify_ok:
        return False
    if status == "WARN" and not notify_warn:
        return False
    if status in ("ERROR", "ABORTED") and not notify_error:
        return False

    tag_map = {
        "OK":      "white_check_mark",
        "WARN":    "warning",
        "ERROR":   "x",
        "ABORTED": "no_entry",
    }
    priority_map = {
        "OK": 1,
        "WARN": 3,
        "ERROR": 5,
        "ABORTED": 5,
    }

    # Build message body
    parts = [f"Job '{job_name}' finished with status: {status}"]
    if total > 0:
        parts.append(f"Files: {total:,} scanned, {changed:,} changed ({pct}%)")
    if duration_s > 0:
        m = int(duration_s // 60)
        s = int(duration_s % 60)
        parts.append(f"Duration: {m}m {s}s" if m > 0 else f"Duration: {s}s")
    if triggered_by:
        parts.append(f"Triggered by: {triggered_by}")

    message = "\n".join(parts)

    return send_notification(
        message=message,
        topic=topic,
        title=f"SyncGuard — {job_name}",
        priority=priority_map.get(status, 3),
        tags=[tag_map.get(status, "bell")],
        server=server,
        access_token=access_token,
    )
