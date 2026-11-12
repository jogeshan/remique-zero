
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.handlers.auth import start_command, help_command
from app.handlers.admin import (
    invite_command,
    revoke_command,
    users_command,
)
from app.handlers.user_management import (
    disable_command,
    enable_command,
    promote_command,
    demote_command,
)
from app.handlers.ai import ai_message_handler


def register_handlers(application: Application) -> None:
    commands = [
        ("start", start_command),
        ("help", help_command),
        ("invite", invite_command),
        ("revoke", revoke_command),
        ("users", users_command),
        ("disable", disable_command),
        ("enable", enable_command),
        ("promote", promote_command),
        ("demote", demote_command),
    ]

    for command, callback in commands:
        application.add_handler(
            CommandHandler(command, callback)
        )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            ai_message_handler,
        )
    )
