"""Owner-only auth gate.

Every handler is wrapped so only the allowlisted numeric Telegram ID(s) can use
the bot. Gating is on effective_user.id (unforgeable), never the username, and a
missing OWNER_TELEGRAM_ID fails closed (config.is_owner returns False for all).
See guide s12.
"""

from __future__ import annotations

import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

import config

log = logging.getLogger(__name__)


def owner_only(func):
    """Decorator: run the handler only for an allowlisted numeric user ID."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        uid = user.id if user else None
        if not config.is_owner(uid):
            log.warning("Rejected non-owner access from user id=%s", uid)
            if update.callback_query:
                await update.callback_query.answer("Not authorised.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "Sorry, this is a private study bot."
                )
            return None
        return await func(update, context, *args, **kwargs)

    return wrapper
