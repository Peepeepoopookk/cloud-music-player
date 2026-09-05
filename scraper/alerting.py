import os
import logging
import datetime
import requests

logger = logging.getLogger(__name__)

LEVEL_COLORS = {
    "error": 0xE74C3C,    # Red
    "warning": 0xF39C12,  # Orange
    "info": 0x3498DB,     # Blue
    "success": 0x2ECC71,  # Green
}


def send_alert(title, details, level="error"):
    """
    Sends a concise alert notification to a Discord webhook if configured.
    Guaranteed to be a safe no-op if DISCORD_ALERT_WEBHOOK_URL is unset.
    Swallows all internal errors so alerting never crashes the caller.
    """
    webhook_url = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.debug("send_alert: DISCORD_ALERT_WEBHOOK_URL not set. Skipping alert.")
        return False

    try:
        color = LEVEL_COLORS.get(level.lower(), LEVEL_COLORS["error"])
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"

        payload = {
            "username": "Cloud Music Player Alerts",
            "embeds": [
                {
                    "title": f"[{level.upper()}] {str(title)[:240]}",
                    "description": str(details)[:2000],
                    "color": color,
                    "timestamp": timestamp,
                    "footer": {
                        "text": "Wavify Backend Alert"
                    }
                }
            ]
        }

        response = requests.post(webhook_url, json=payload, timeout=5)
        if response.status_code in (200, 204):
            logger.info(f"send_alert: Alert '{title}' successfully sent to Discord.")
            return True
        else:
            logger.warning(
                f"send_alert: Discord webhook returned HTTP {response.status_code}: {response.text[:200]}"
            )
            return False

    except Exception as e:
        logger.warning(f"send_alert: Failed to dispatch alert '{title}': {e}")
        return False
