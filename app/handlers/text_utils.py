"""
Helpers for sending long Telegram messages.

Telegram rejects messages longer than 4096 characters, and AI
replies (and some reports) can exceed that. split_message breaks
text into chunks that respect the limit, preferring paragraph,
then line, then word boundaries; reply_long sends the chunks in
order through a message's reply_text.
"""

TELEGRAM_MESSAGE_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """
    Split `text` into chunks of at most `limit` characters.

    Boundaries are chosen in order of preference: paragraph
    breaks, line breaks, spaces. A hard cut at the limit is the
    last resort (e.g. one very long word). Short text — including
    empty text — comes back as a single chunk, unchanged.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text

    while len(rest) > limit:
        window = rest[:limit]

        cut = window.rfind("\n\n")
        separator = 2

        if cut <= 0:
            cut = window.rfind("\n")
            separator = 1

        if cut <= 0:
            cut = window.rfind(" ")
            separator = 1

        if cut <= 0:
            # No usable boundary inside the window: hard cut.
            chunks.append(rest[:limit])
            rest = rest[limit:]
            continue

        chunks.append(rest[:cut])
        rest = rest[cut + separator:]

    if rest:
        chunks.append(rest)

    return chunks


async def reply_long(message, text: str) -> None:
    """Reply with `text`, split into as many messages as needed."""
    for chunk in split_message(text):
        await message.reply_text(chunk)
