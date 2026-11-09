"""Telegram alerting helper for profitable arbitrage deals."""
import logging
import httpx

from schemas import Listing, StrategistDecision
import config

logger = logging.getLogger("arbitrage_swarm.notify")


def send_telegram_alert(listing: Listing, decision: StrategistDecision) -> bool:
    """Send deal summary to Telegram if bot credentials are configured."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    text = (
        f"🐝 *Profitable deal found*\n\n"
        f"*{listing.title}*\n"
        f"Price: RM{listing.price:.2f} · Margin: {decision.estimated_margin_pct:.1f}%\n\n"
        f"{decision.reasoning}\n\n"
    )
    if decision.negotiation_message:
        text += f"💬 Suggested message:\n{decision.negotiation_message}\n\n"
    text += listing.url

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = httpx.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Telegram alert")
        return False
