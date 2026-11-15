
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
from app.handlers.ai import (
    ai_message_handler,
    memories_command,
    newchat_command,
    remind_command,
    reminders_command,
    tasks_command,
)
from app.handlers.media import (
    document_message_handler,
    photo_message_handler,
    voice_message_handler,
)
from app.handlers.finance import (
    debts_command,
    expense_command,
    income_command,
    summary_command,
)
from app.handlers.callbacks import (
    reminder_callback_handler,
    settings_callback_handler,
    settings_command,
)
from app.handlers.dashboard import apitoken_command
from app.handlers.export import export_command


def register_handlers(application: Application) -> None:
    commands = [
        ("start", start_command),
        ("help", help_command),
        ("newchat", newchat_command),
        ("memories", memories_command),
        ("remind", remind_command),
        ("reminders", reminders_command),
        ("expense", expense_command),
        ("income", income_command),
        ("summary", summary_command),
        ("debts", debts_command),
        ("tasks", tasks_command),
        ("settings", settings_command),
        ("apitoken", apitoken_command),
        ("export", export_command),
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

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_message_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO,
            voice_message_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_message_handler,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            reminder_callback_handler,
            pattern=r"^rem:",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            settings_callback_handler,
            pattern=r"^set:",
        )
    )
