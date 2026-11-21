# Class Vent Bot

A Telegram bot for a group with topics enabled. Class members privately send
text or stickers to the bot; it posts them in one group topic under the bot's name,
without adding their name or linking to their message. It does not use AI,
store vent contents in a database, or deliberately log message contents or user
IDs. A local SQLite file stores only the latest vent number.

## Privacy: please read

This is **not a guarantee of absolute anonymity**. Telegram receives the
private messages; the person operating the bot or hosting infrastructure can
potentially see the sender and text, as can anyone who gets the bot token.
Timing, writing style, or the contents of a vent can reveal who sent it. Do not
include identifying details if you want to stay anonymous. Stickers can also
identify you, for example if they come from a personal sticker pack. The group may have
its own rules; don't share private information or use the bot to harass people.

A message typed in the group is visible under its sender's name. If someone
uses `/vent` there, the bot tries to delete the command and tells the group to
use private messages instead. Deletion is not a guarantee: members or
notifications may already have seen it, and deletion can fail. The bot
needs permission to delete group messages. **Send vents privately.**

## Setup

1. Create a Telegram bot with [BotFather](https://t.me/BotFather) and keep its
   token secret. Never send the token to classmates or commit it to a repo.
2. Enable topics in your class supergroup and create a dedicated vent topic.
   Add the bot to the group as an administrator with permission to delete
   messages. Administrator status is also needed for reliable membership
   checks. Members must have started a private chat with the bot themselves.
3. Install dependencies using the existing virtual environment:

   ```powershell
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. On the machine running the bot, copy `.env.example` to `.env` if `.env`
   doesn't already exist:

   ```powershell
   Copy-Item .env.example .env
   ```

   Open `.env` and set `BOT_TOKEN` to your token from BotFather. Leave the IDs
   blank for now. Keep `.env` private: it contains the token and is excluded
   from Git and Docker build contexts. If the token is exposed, revoke it using
   BotFather and replace it.

   Temporarily start setup mode:

   ```powershell
   .\venv\Scripts\python.exe bot.py --setup
   ```

   Send `/topicid` in the dedicated group topic. The bot replies with
   `GROUP_ID` and `TOPIC_ID`. Stop setup mode with Ctrl+C. Only one instance
   of this bot should poll Telegram at a time; don't run setup alongside the
   normal bot. Setup mode processes pending messages too.
5. Add those IDs to `GROUP_ID` and `TOPIC_ID` in `.env`, save the file, and
   run the bot:

   ```powershell
   .\venv\Scripts\python.exe bot.py
   ```

The bot loads `.env` from the directory containing `bot.py`; existing process
environment variables take precedence. For a server, set all three variables
as secrets/environment variables in your hosting provider and run
`python bot.py` as a long-lived process. Do not run multiple replicas: the bot
uses Telegram long polling, not a webhook. If a webhook was previously
configured for this token, remove it using Telegram's Bot API before starting
polling.

Alternatively, build the included Docker image and configure `BOT_TOKEN`,
`GROUP_ID`, and `TOPIC_ID` as environment variables in your container host.
Run the same image with `python bot.py --setup` for the setup step.

The counter defaults to `vent_counter.sqlite3` next to `bot.py`. Set the
optional `COUNTER_DB` environment variable to use another file; relative paths
are resolved next to `bot.py`. Its parent directory must already exist and be
writable by the bot. On a server, keep this file on persistent storage so
numbering survives restarts and redeployments. The Docker image defaults to
`/data/vent_counter.sqlite3`; mount a persistent volume at `/data` and ensure
it is writable by the container's `bot` user. Copy the file to carry numbering
over when moving between hosts. Deleting it resets numbering to 1.

## Using it

Open the bot's private chat and send `/start`. Send a text message, or use
`/vent your message`; `/vent` alone shows a prompt. You can also send a sticker
privately; the bot posts a numbered label followed by the sticker as its reply.
Only current members of the configured group can post. Photos, attachments and
other media are not supported. The bot won't publish messages that are too long
for Telegram. If delivery cannot be confirmed, check the topic before retrying
to avoid duplicates. Numbers are reserved before delivery and may have gaps if
a vent is too long or delivery fails. If a sticker fails after its label is
posted, the label can remain without the sticker.

## Tests

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests mock Telegram; a real group and bot token are needed to verify
permissions and delivery end to end.
