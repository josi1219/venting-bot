import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Update, User
from telegram.error import TelegramError

import bot


SETTINGS = bot.Settings(token="test-token", group_id=-100123, topic_id=456)


def make_objects(status="member", is_member=False):
    message = SimpleNamespace(
        text=None,
        message_thread_id=456,
        reply_text=AsyncMock(),
        delete=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=SETTINGS.group_id),
    )
    telegram_bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status=status, is_member=is_member)
        ),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=90)),
        send_sticker=AsyncMock(),
    )
    context = SimpleNamespace(bot=telegram_bot, bot_data={"settings": SETTINGS})
    return update, context


class ConfigurationTests(unittest.TestCase):
    def test_load_settings(self):
        values = {"BOT_TOKEN": "secret", "GROUP_ID": "-100123", "TOPIC_ID": "456"}
        with patch.dict(os.environ, values, clear=True):
            settings = bot.load_settings()
        self.assertEqual(settings, bot.Settings("secret", -100123, 456))

    def test_missing_and_invalid_configuration(self):
        cases = [
            ({"BOT_TOKEN": "secret", "GROUP_ID": "-100123"}, "TOPIC_ID must be set"),
            ({"BOT_TOKEN": "secret", "GROUP_ID": "group", "TOPIC_ID": "456"}, "integers"),
            ({"BOT_TOKEN": "secret", "GROUP_ID": "123", "TOPIC_ID": "456"}, "negative"),
            ({"BOT_TOKEN": "secret", "GROUP_ID": "-123", "TOPIC_ID": "0"}, "positive"),
        ]
        for values, expected in cases:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(ValueError, expected):
                    bot.load_settings()

    def test_counter_path_configuration(self):
        values = {
            "BOT_TOKEN": "secret", "GROUP_ID": "-100123",
            "TOPIC_ID": "456", "COUNTER_DB": "data/counter.sqlite3",
        }
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(
                bot.load_settings().counter_db,
                bot.ENV_PATH.parent / "data" / "counter.sqlite3",
            )
        values["COUNTER_DB"] = ""
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ValueError, "COUNTER_DB must not be empty"):
                bot.load_settings()

    def test_setup_loads_token_from_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("BOT_TOKEN=123:file-token\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(bot, "ENV_PATH", path),
                patch.object(sys, "argv", ["bot.py", "--setup"]),
                patch.object(bot.Application, "run_polling", autospec=True) as polling,
            ):
                bot.main()
            self.assertEqual(polling.call_args.args[0].bot.token, "123:file-token")

    def test_existing_environment_takes_precedence_over_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("BOT_TOKEN=123:file-token\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"BOT_TOKEN": "123:environment-token"}, clear=True),
                patch.object(bot, "ENV_PATH", path),
                patch.object(sys, "argv", ["bot.py", "--setup"]),
                patch.object(bot.Application, "run_polling", autospec=True) as polling,
            ):
                bot.main()
            self.assertEqual(polling.call_args.args[0].bot.token, "123:environment-token")


class PublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_patch = patch(
            __name__ + ".SETTINGS",
            replace(SETTINGS, counter_db=Path(directory.name) / "counter.sqlite3"),
        )
        settings_patch.start()
        self.addCleanup(settings_patch.stop)

    async def test_publishes_only_text_as_new_group_message(self):
        update, context = make_objects()
        text = "I'm tired of this.\nLet me vent! 🔥"
        await bot.publish_vent(update, context, text)

        context.bot.get_chat_member.assert_awaited_once_with(-100123, 123)
        context.bot.send_message.assert_awaited_once_with(
            chat_id=-100123, message_thread_id=456, text="AnonymousVent - #1\n\n" + text
        )
        update.effective_message.reply_text.assert_awaited_once_with("Posted anonymously.")

    async def test_command_preserves_multiline_text(self):
        update, context = make_objects()
        update.effective_message.text = "/vent first line\nsecond line"
        await bot.vent_command(update, context)
        self.assertEqual(
            context.bot.send_message.await_args.kwargs["text"],
            "AnonymousVent - #1\n\nfirst line\nsecond line",
        )

    async def test_empty_vent_prompts_without_membership_lookup(self):
        update, context = make_objects()
        await bot.publish_vent(update, context, "  \n")
        context.bot.get_chat_member.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        self.assertIn("Send me the vent", update.effective_message.reply_text.await_args.args[0])

    async def test_current_member_statuses_are_accepted(self):
        for status, is_member in [
            ("creator", False),
            ("administrator", False),
            ("member", False),
            ("restricted", True),
        ]:
            with self.subTest(status=status):
                update, context = make_objects(status, is_member)
                await bot.publish_vent(update, context, "hello")
                context.bot.send_message.assert_awaited_once()

    async def test_nonmembers_and_restricted_nonmembers_are_rejected(self):
        for status in ("left", "kicked", "restricted"):
            with self.subTest(status=status):
                update, context = make_objects(status)
                await bot.publish_vent(update, context, "hello")
                context.bot.send_message.assert_not_awaited()
                self.assertIn("Only current members", update.effective_message.reply_text.await_args.args[0])

    async def test_membership_error_fails_closed(self):
        update, context = make_objects()
        context.bot.get_chat_member.side_effect = TelegramError("test")
        await bot.publish_vent(update, context, "hello")
        context.bot.send_message.assert_not_awaited()
        self.assertIn("couldn't check", update.effective_message.reply_text.await_args.args[0])

    async def test_delivery_error_does_not_claim_success(self):
        update, context = make_objects()
        context.bot.send_message.side_effect = TelegramError("test")
        await bot.publish_vent(update, context, "hello")
        self.assertIn("couldn't confirm", update.effective_message.reply_text.await_args.args[0])

    async def test_sticker_is_posted_as_new_message(self):
        update, context = make_objects()
        update.effective_message.sticker = SimpleNamespace(file_id="sticker-file-id")
        await bot.private_sticker(update, context)
        context.bot.get_chat_member.assert_awaited_once_with(-100123, 123)
        context.bot.send_message.assert_awaited_once_with(
            chat_id=-100123, message_thread_id=456, text="AnonymousVent - #1"
        )
        args = context.bot.send_sticker.await_args.kwargs
        self.assertEqual(args["chat_id"], -100123)
        self.assertEqual(args["message_thread_id"], 456)
        self.assertEqual(args["sticker"], "sticker-file-id")
        self.assertEqual(args["reply_parameters"].message_id, 90)
        update.effective_message.reply_text.assert_awaited_once_with("Posted anonymously.")

    async def test_nonmember_cannot_post_sticker(self):
        update, context = make_objects("left")
        await bot.publish_vent(update, context, sticker_id="sticker-file-id")
        context.bot.send_sticker.assert_not_awaited()
        self.assertIn("Only current members", update.effective_message.reply_text.await_args.args[0])

    async def test_sticker_membership_error_fails_closed(self):
        update, context = make_objects()
        context.bot.get_chat_member.side_effect = TelegramError("test")
        await bot.publish_vent(update, context, sticker_id="sticker-file-id")
        context.bot.send_sticker.assert_not_awaited()
        self.assertIn("couldn't check", update.effective_message.reply_text.await_args.args[0])

    async def test_sticker_delivery_error_does_not_claim_success(self):
        update, context = make_objects()
        context.bot.send_sticker.side_effect = TelegramError("test")
        await bot.publish_vent(update, context, sticker_id="sticker-file-id")
        self.assertIn("couldn't confirm", update.effective_message.reply_text.await_args.args[0])

    async def test_counter_failure_does_not_publish(self):
        update, context = make_objects()
        with patch.object(bot, "reserve_vent_number", side_effect=sqlite3.OperationalError):
            await bot.publish_vent(update, context, "hello")
        context.bot.send_message.assert_not_awaited()
        self.assertIn("couldn't number", update.effective_message.reply_text.await_args.args[0])

    async def test_text_and_sticker_share_counter(self):
        update, context = make_objects()
        await bot.publish_vent(update, context, "first")
        await bot.publish_vent(update, context, sticker_id="sticker-file-id")
        self.assertEqual(context.bot.send_message.await_args_list[0].kwargs["text"], "AnonymousVent - #1\n\nfirst")
        self.assertEqual(context.bot.send_message.await_args_list[1].kwargs["text"], "AnonymousVent - #2")

    async def test_message_length_boundary_including_emoji(self):
        maximum = bot.MAX_MESSAGE_UNITS - bot.utf16_length("AnonymousVent - #1\n\n")
        for text, accepted in [
            ("a" * maximum, True),
            ("a" * (maximum + 1), False),
            ("🔥" * (maximum // 2 + 1), False),
        ]:
            with self.subTest(length=bot.utf16_length(text)):
                update, context = make_objects()
                await bot.publish_vent(update, context, text)
                self.assertEqual(context.bot.send_message.await_count, int(accepted))
                if not accepted:
                    self.assertIn("too long", update.effective_message.reply_text.await_args.args[0])

    async def test_message_length_boundary_accounts_for_more_digits(self):
        limit = bot.MAX_MESSAGE_UNITS - bot.utf16_length("AnonymousVent - #10\n\n")
        for size, accepted in [(limit, True), (limit + 1, False)]:
            with self.subTest(size=size):
                update, context = make_objects()
                with patch.object(bot, "reserve_vent_number", return_value=10):
                    await bot.publish_vent(update, context, "a" * size)
                self.assertEqual(context.bot.send_message.await_count, int(accepted))


class CounterTests(unittest.TestCase):
    def test_counter_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter.sqlite3"
            self.assertEqual(bot.reserve_vent_number(path), 1)
            self.assertEqual(bot.reserve_vent_number(path), 2)
            self.assertEqual(bot.reserve_vent_number(path), 3)

    def test_concurrent_reservations_are_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter.sqlite3"
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(bot.reserve_vent_number, [path] * 30))
            self.assertEqual(sorted(results), list(range(1, 31)))
            self.assertEqual(bot.reserve_vent_number(path), 31)

    def test_counter_continues_after_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter.sqlite3"
            command = [
                sys.executable, "-c",
                "import sys; from pathlib import Path; "
                "from bot import reserve_vent_number; "
                "print(reserve_vent_number(Path(sys.argv[1])))",
                str(path),
            ]
            first = subprocess.run(command, capture_output=True, text=True, check=True)
            second = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(first.stdout.strip(), "1")
            self.assertEqual(second.stdout.strip(), "2")


class GroupSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_group_command_and_does_not_repeat_content(self):
        update, context = make_objects()
        update.effective_message.text = "/vent sensitive text"
        await bot.group_vent(update, context)
        update.effective_message.delete.assert_awaited_once()
        notice = context.bot.send_message.await_args.kwargs
        self.assertEqual(notice["chat_id"], SETTINGS.group_id)
        self.assertEqual(notice["message_thread_id"], SETTINGS.topic_id)
        self.assertNotIn("sensitive text", notice["text"])
        self.assertNotIn("reply_to_message_id", notice)
        self.assertIn("may still have been seen", notice["text"])

    async def test_delete_failure_warns_group_to_delete_message(self):
        update, context = make_objects()
        update.effective_message.delete.side_effect = TelegramError("test")
        await bot.group_vent(update, context)
        self.assertIn("Delete it yourself", context.bot.send_message.await_args.kwargs["text"])

    async def test_ignores_other_groups(self):
        update, context = make_objects()
        update.effective_chat.id = -100999
        await bot.group_vent(update, context)
        update.effective_message.delete.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()

    async def test_setup_reports_topic_ids(self):
        update, context = make_objects()
        update.effective_chat.type = "supergroup"
        await bot.topic_info(update, context)
        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("GROUP_ID=-100123", reply)
        self.assertIn("TOPIC_ID=456", reply)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        counter_patch = patch.object(bot, "reserve_vent_number", return_value=1)
        counter_patch.start()
        self.addCleanup(counter_patch.stop)

    async def test_real_private_sticker_routes_to_anonymous_publication(self):
        values = {"BOT_TOKEN": "123:ABC", "GROUP_ID": "-100123", "TOPIC_ID": "456"}
        with (
            patch.dict(os.environ, values, clear=True),
            patch.object(sys, "argv", ["bot.py"]),
            patch.object(bot.Application, "run_polling", autospec=True) as polling,
        ):
            bot.main()
        application = polling.call_args.args[0]
        application._initialized = True
        application.bot._bot_user = User(id=123, first_name="Bot", is_bot=True, username="vent_bot")
        update = Update.de_json(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "date": 0,
                    "chat": {"id": 1234, "type": "private", "first_name": "Member"},
                    "from": {"id": 1234, "is_bot": False, "first_name": "Member"},
                    "sticker": {
                        "file_id": "sticker-file-id",
                        "file_unique_id": "unique-id",
                        "width": 512,
                        "height": 512,
                        "is_animated": False,
                        "is_video": False,
                        "type": "regular",
                    },
                },
            },
            application.bot,
        )
        with (
            patch.object(
                type(application.bot), "get_chat_member", new_callable=AsyncMock,
                return_value=SimpleNamespace(status="member"),
            ),
            patch.object(type(application.bot), "send_sticker", new_callable=AsyncMock) as send_sticker,
            patch.object(
                type(application.bot), "send_message", new_callable=AsyncMock,
                return_value=SimpleNamespace(message_id=90),
            ) as send_message,
        ):
            await application.process_update(update)

        args = send_sticker.await_args.kwargs
        self.assertEqual(args["chat_id"], -100123)
        self.assertEqual(args["message_thread_id"], 456)
        self.assertEqual(args["sticker"], "sticker-file-id")
        self.assertEqual(args["reply_parameters"].message_id, 90)
        self.assertEqual(send_message.await_args_list[0].kwargs["text"], "AnonymousVent - #1")
        self.assertEqual(send_message.await_args.kwargs["chat_id"], 1234)
        self.assertIn("Posted anonymously", send_message.await_args.kwargs["text"])

    async def test_real_private_command_routes_to_anonymous_publication(self):
        values = {"BOT_TOKEN": "123:ABC", "GROUP_ID": "-100123", "TOPIC_ID": "456"}
        with (
            patch.dict(os.environ, values, clear=True),
            patch.object(sys, "argv", ["bot.py"]),
            patch.object(bot.Application, "run_polling", autospec=True) as polling,
        ):
            bot.main()
        application = polling.call_args.args[0]
        application._initialized = True
        application.bot._bot_user = User(id=123, first_name="Bot", is_bot=True, username="vent_bot")
        update = Update.de_json(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "date": 0,
                    "chat": {"id": 1234, "type": "private", "first_name": "Member"},
                    "from": {"id": 1234, "is_bot": False, "first_name": "Member"},
                    "text": "/vent hello",
                    "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
                },
            },
            application.bot,
        )
        with (
            patch.object(
                type(application.bot), "get_chat_member", new_callable=AsyncMock,
                return_value=SimpleNamespace(status="member"),
            ),
            patch.object(type(application.bot), "send_message", new_callable=AsyncMock) as send,
        ):
            await application.process_update(update)

        self.assertEqual(send.await_count, 2)
        self.assertEqual(
            send.await_args_list[0].kwargs,
            {"chat_id": -100123, "message_thread_id": 456, "text": "AnonymousVent - #1\n\nhello"},
        )
        self.assertEqual(send.await_args_list[1].kwargs["chat_id"], 1234)


if __name__ == "__main__":
    unittest.main()
