import argparse
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyParameters, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

LOGGER = logging.getLogger(__name__)
ENV_PATH = Path(__file__).resolve().with_name(".env")
DEFAULT_COUNTER_DB = ENV_PATH.with_name("vent_counter.sqlite3")
MAX_MESSAGE_UNITS = 4096


@dataclass(frozen=True)
class Settings:
    token: str
    group_id: int
    topic_id: int
    counter_db: Path = DEFAULT_COUNTER_DB


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def load_settings() -> Settings:
    token = required_env("BOT_TOKEN")
    group_value = required_env("GROUP_ID")
    topic_value = required_env("TOPIC_ID")
    try:
        group_id = int(group_value)
        topic_id = int(topic_value)
    except ValueError as exc:
        raise ValueError("GROUP_ID and TOPIC_ID must be integers") from exc
    if group_id >= 0 or topic_id <= 0:
        raise ValueError("GROUP_ID must be negative and TOPIC_ID must be positive")
    counter_path = os.environ.get("COUNTER_DB")
    if counter_path is None:
        counter_db = DEFAULT_COUNTER_DB
    elif not counter_path.strip():
        raise ValueError("COUNTER_DB must not be empty")
    else:
        counter_db = Path(counter_path).expanduser()
        if not counter_db.is_absolute():
            counter_db = ENV_PATH.parent / counter_db
    return Settings(token=token, group_id=group_id, topic_id=topic_id, counter_db=counter_db)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def reserve_vent_number(path: Path) -> int:
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vent_counter "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), "
                "count INTEGER NOT NULL CHECK (count >= 0))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO vent_counter (id, count) VALUES (1, 0)"
            )
            connection.execute("UPDATE vent_counter SET count = count + 1 WHERE id = 1")
            return connection.execute(
                "SELECT count FROM vent_counter WHERE id = 1"
            ).fetchone()[0]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Got something to get off your chest? Send a message or sticker here, "
        "or use /vent followed by your text. I'll post it in the class topic "
        "without your name.\n\n"
        "Don't include identifying details if you want to stay anonymous. "
        "Telegram and whoever runs this bot can see messages sent to the bot."
    )


async def publish_vent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str = "",
    *,
    sticker_id: str | None = None,
) -> None:
    message = update.effective_message
    user = update.effective_user
    settings = context.bot_data["settings"]
    if sticker_id is None:
        text = text.strip()
        if not text:
            await message.reply_text("Send me the vent as a private message, or use /vent your text.")
            return
        if utf16_length(text) > MAX_MESSAGE_UNITS:
            await message.reply_text("That vent is too long for one Telegram message. Please shorten it.")
            return
    if user is None:
        await message.reply_text("I couldn't verify your group membership. Please try again.")
        return

    try:
        member = await context.bot.get_chat_member(settings.group_id, user.id)
    except TelegramError:
        LOGGER.warning("Could not check group membership.")
        await message.reply_text(
            "I couldn't check group membership. Please try again or ask an admin "
            "to check the bot's permissions."
        )
        return

    if member.status not in ("creator", "administrator", "member") and not (
        member.status == "restricted" and member.is_member
    ):
        await message.reply_text("Only current members of the class group can send vents.")
        return

    try:
        number = reserve_vent_number(settings.counter_db)
    except sqlite3.Error:
        LOGGER.warning("Could not reserve a vent number.")
        await message.reply_text("I couldn't number your vent. Please ask an admin to check the bot.")
        return

    label = f"AnonymousVent - #{number}"
    if sticker_id is None and utf16_length(f"{label}\n\n{text}") > MAX_MESSAGE_UNITS:
        await message.reply_text("That vent is too long for one Telegram message. Please shorten it.")
        return

    try:
        if sticker_id is not None:
            label_message = await context.bot.send_message(
                chat_id=settings.group_id,
                message_thread_id=settings.topic_id,
                text=label,
            )
            await context.bot.send_sticker(
                chat_id=settings.group_id,
                message_thread_id=settings.topic_id,
                sticker=sticker_id,
                reply_parameters=ReplyParameters(message_id=label_message.message_id),
            )
        else:
            await context.bot.send_message(
                chat_id=settings.group_id,
                message_thread_id=settings.topic_id,
                text=f"{label}\n\n{text}",
            )
    except TelegramError:
        LOGGER.warning("Could not confirm vent delivery.")
        await message.reply_text(
            "I couldn't confirm whether your vent was posted. Check the topic "
            "before trying again, or ask an admin to check the bot."
        )
        return

    await message.reply_text("Posted anonymously.")


async def vent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.effective_message.text.split(maxsplit=1)
    await publish_vent(update, context, parts[1] if len(parts) == 2 else "")


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await publish_vent(update, context, update.effective_message.text)


async def private_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await publish_vent(update, context, sticker_id=update.effective_message.sticker.file_id)


async def unsupported_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Please send your vent as text or a sticker.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "I don't recognize that command. Send your vent as text or use /vent your text."
    )


async def group_vent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = context.bot_data["settings"]
    if update.effective_chat.id != settings.group_id:
        return

    try:
        await message.delete()
    except TelegramError:
        LOGGER.warning("Could not delete a group /vent command.")
        notice = (
            "A /vent command is visible in the group. Delete it yourself and "
            "send your vent privately to the bot instead."
        )
    else:
        notice = (
            "For anonymity, send your vent privately to the bot, not in the group. "
            "The original command may still have been seen."
        )
    try:
        await context.bot.send_message(
            chat_id=settings.group_id,
            message_thread_id=message.message_thread_id,
            text=notice,
        )
    except TelegramError:
        LOGGER.warning("Could not send the group privacy notice.")


async def topic_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Use /topicid inside the group topic you want to use.")
    elif message.message_thread_id is None:
        await message.reply_text("Use /topicid inside a dedicated group topic.")
    else:
        await message.reply_text(
            f"GROUP_ID={chat.id}\nTOPIC_ID={message.message_thread_id}\n\n"
            "Set these on the bot's server, then restart it without --setup."
        )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("An unexpected error occurred: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == "private":
        try:
            await update.effective_message.reply_text(
                "Something went wrong. Check the topic before retrying, "
                "or ask an admin to check the bot."
            )
        except TelegramError:
            LOGGER.warning("Could not notify the user of an error.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Anonymous class vent bot")
    parser.add_argument("--setup", action="store_true", help="find your group and topic IDs")
    args = parser.parse_args()

    load_dotenv(ENV_PATH, override=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)

    try:
        if args.setup:
            token = required_env("BOT_TOKEN")
            settings = None
        else:
            settings = load_settings()
            token = settings.token
    except ValueError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")

    application = Application.builder().token(token).build()
    application.add_error_handler(handle_error)
    if args.setup:
        application.add_handler(CommandHandler("topicid", topic_info))
        print("Setup mode: send /topicid in the dedicated group topic.", flush=True)
    else:
        application.bot_data["settings"] = settings
        application.add_handler(CommandHandler(["start", "help"], start, filters=filters.ChatType.PRIVATE))
        application.add_handler(CommandHandler("vent", vent_command, filters=filters.ChatType.PRIVATE))
        application.add_handler(CommandHandler("vent", group_vent, filters=filters.ChatType.GROUPS))
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text))
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Sticker.ALL, private_sticker))
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, unsupported_message))
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.COMMAND, unknown_command))
        print("Vent bot is running.", flush=True)
    application.run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
