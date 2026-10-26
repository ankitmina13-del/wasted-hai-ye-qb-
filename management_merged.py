
import asyncio
import html
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError, BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION - EDIT THESE TWO VALUES
# =========================================================

BOT_TOKEN = "PASTE_BOTFATHER_TOKEN_HERE"
OWNER_ID = 123456789  # Apni numeric Telegram user ID likho

DB_FILE = "missrose_data.db"
WARN_LIMIT = 5
BAN_LIMIT = 10
FLOOD_LIMIT = 6
FLOOD_SECONDS = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("MissRose")


# =========================================================
# DATABASE
# =========================================================

def connect():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
            chat_id INTEGER,
            key TEXT,
            value TEXT,
            PRIMARY KEY(chat_id,key)
        );

        CREATE TABLE IF NOT EXISTS warnings(
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id,user_id)
        );

        CREATE TABLE IF NOT EXISTS ban_counts(
            chat_id INTEGER,
            admin_id INTEGER,
            user_id INTEGER,
            created_at TEXT,
            PRIMARY KEY(chat_id,admin_id,user_id)
        );

        CREATE TABLE IF NOT EXISTS filters(
            chat_id INTEGER,
            keyword TEXT,
            reply TEXT,
            PRIMARY KEY(chat_id,keyword)
        );

        CREATE TABLE IF NOT EXISTS notes(
            chat_id INTEGER,
            name TEXT,
            content TEXT,
            PRIMARY KEY(chat_id,name)
        );

        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            actor_id INTEGER,
            action TEXT,
            target_id INTEGER,
            details TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS verified(
            chat_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY(chat_id,user_id)
        );
        """)


def setting(chat_id, key, default=""):
    with connect() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE chat_id=? AND key=?",
            (chat_id, key),
        ).fetchone()
    return row["value"] if row else default


def set_setting(chat_id, key, value):
    with connect() as con:
        con.execute("""
            INSERT INTO settings(chat_id,key,value)
            VALUES(?,?,?)
            ON CONFLICT(chat_id,key)
            DO UPDATE SET value=excluded.value
        """, (chat_id, key, str(value)))


def log_event(chat_id, actor, action, target=None, details=""):
    with connect() as con:
        con.execute("""
            INSERT INTO events
            (chat_id,actor_id,action,target_id,details,created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            chat_id,
            actor,
            action,
            target,
            details,
            datetime.now(timezone.utc).isoformat(),
        ))


# =========================================================
# HELPERS
# =========================================================

def is_group(update):
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


async def admin_status(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status
    except TelegramError:
        return None


async def is_admin(bot, chat_id, user_id):
    return await admin_status(bot, chat_id, user_id) in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )


async def require_admin(update, context):
    if not is_group(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Is command ko group mein use karo."
            )
        return False

    user = update.effective_user
    if not user or not await is_admin(
        context.bot, update.effective_chat.id, user.id
    ):
        await update.effective_message.reply_text(
            "Ye command sirf group admins ke liye hai."
        )
        return False

    return True


def replied_user(update):
    msg = update.effective_message
    if msg and msg.reply_to_message:
        return msg.reply_to_message.from_user
    return None


def parse_duration(value):
    m = re.fullmatch(r"(\d+)(s|m|h|d)", value.lower())
    if not m:
        return None

    n = int(m.group(1))
    seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]

    if seconds < 1 or seconds > 365 * 86400:
        return None

    return timedelta(seconds=seconds)


def full_permissions():
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


async def mute_user(bot, chat_id, user_id, duration=None):
    args = {}
    if duration:
        args["until_date"] = datetime.now(timezone.utc) + duration

    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=ChatPermissions(can_send_messages=False),
        **args,
    )


async def unmute_user(bot, chat_id, user_id):
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=full_permissions(),
    )


async def demote_user(bot, chat_id, user_id):
    member = await bot.get_chat_member(chat_id, user_id)

    if member.status == ChatMemberStatus.OWNER:
        return False, "Group owner ko demote nahi kiya ja sakta."

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, "User ab admin nahi hai."

    await bot.promote_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        is_anonymous=False,
        can_manage_chat=False,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
        can_post_messages=False,
        can_edit_messages=False,
    )
    return True, "Demotion successful."


async def notify_admins(context, chat_id, text):
    # Group notification, visible to group members/admins.
    try:
        await context.bot.send_message(chat_id, text)
    except TelegramError as exc:
        log.warning("Admin alert failed: %s", exc)


async def log_to_channel(context, chat_id, text):
    destination = setting(chat_id, "log_chat", "")
    if not destination:
        return
    try:
        await context.bot.send_message(int(destination), text)
    except (TelegramError, ValueError):
        pass


# =========================================================
# BAN COUNT / AUTO-DEMOTION
# =========================================================

async def record_ban(context, chat_id, admin_id, target_id, admin_name):
    # Count only distinct users per admin per group.
    with connect() as con:
        con.execute("""
            INSERT OR IGNORE INTO ban_counts
            (chat_id,admin_id,user_id,created_at)
            VALUES(?,?,?,?)
        """, (
            chat_id,
            admin_id,
            target_id,
            datetime.now(timezone.utc).isoformat(),
        ))

        count = con.execute("""
            SELECT COUNT(*) AS total FROM ban_counts
            WHERE chat_id=? AND admin_id=?
        """, (chat_id, admin_id)).fetchone()["total"]

    log_event(
        chat_id, admin_id, "successful_ban",
        target_id, f"distinct_bans={count}",
    )

    # Trigger exactly at 10 distinct successful bans.
    if count == BAN_LIMIT:
        try:
            ok, reason = await demote_user(
                context.bot, chat_id, admin_id
            )
        except TelegramError as exc:
            ok, reason = False, str(exc)

        text = (
            "🚨 ADMIN SECURITY ALERT\n\n"
            f"Admin: {admin_name}\n"
            f"Admin ID: {admin_id}\n"
            f"Distinct successful bans: {count}\n"
            f"Demotion: {'SUCCESS' if ok else 'FAILED'}\n"
            f"Details: {reason}"
        )

        await notify_admins(context, chat_id, text)
        await log_to_channel(context, chat_id, text)

        log_event(
            chat_id, context.bot.id,
            "auto_demote_after_10_bans",
            admin_id, f"success={ok}; {reason}",
        )


async def member_update(update, context):
    event = update.chat_member
    if not event:
        return

    old = event.old_chat_member
    new = event.new_chat_member
    actor = event.from_user
    target = new.user
    chat_id = event.chat.id

    # Manual admin ban detected by Telegram.
    if (
        old.status != ChatMemberStatus.BANNED
        and new.status == ChatMemberStatus.BANNED
        and actor
        and actor.id != context.bot.id
        and await is_admin(context.bot, chat_id, actor.id)
    ):
        await record_ban(
            context, chat_id, actor.id, target.id, actor.full_name
        )


# =========================================================
# HELP / INLINE MENU
# =========================================================

HELP = """
🌹 MISSROSE-STYLE BOT

MODERATION
/ban - Reply karke ban
/unban USER_ID - Unban
/kick - Reply karke kick
/mute - Reply karke mute
/unmute - Reply karke unmute
/tban 1h - Temporary ban
/tmute 30m - Temporary mute
/warn - Warning
/warns - Warning count
/resetwarns - Reset warnings
/purge - Reply-based bulk delete
/promote - Reply karke promote
/demote - Reply karke demote
/adminlist - Admin list

WELCOME / RULES
/setwelcome TEXT
/setgoodbye TEXT
/captcha on|off
/setrules TEXT
/rules

FILTERS / NOTES
/filter KEYWORD REPLY
/stop KEYWORD
/filters
/save NAME TEXT
/get NAME
/notes

PROTECTION
/lock links|media|stickers|gifs|forwards
/unlock TYPE
/locks
/setflood NUMBER
/report (reply to message)
/setlog - Save this group as log destination
/settings
"""


async def start(update, context):
    user = update.effective_user
    if user:
        save_join_user(user)

    if not is_group(update):
        await update.effective_message.reply_text(
            "🌹 Group Management Bot\\n\\n"
            "Group moderation aur join-request management ke liye ready hai.\\n"
            "Commands ke liye /help use karein.\\n"
            "Bot manager panel ke liye /panel."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton("📖 Help", callback_data="help"),
            InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("🛡 Security", callback_data="security"),
            InlineKeyboardButton("📋 Rules", callback_data="rules"),
        ],
    ]

    await update.effective_message.reply_text(
        "🌹 Group Management Bot",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_cmd(update, context):
    await update.effective_message.reply_text(HELP)


async def menu_callback(update, context):
    q = update.callback_query
    await q.answer()

    if not q.message or q.message.chat.type not in ("group", "supergroup"):
        return

    cid = q.message.chat.id

    if q.data == "help":
        text = HELP
    elif q.data == "settings":
        text = (
            "⚙️ SETTINGS\n"
            f"CAPTCHA: {setting(cid, 'captcha', 'off')}\n"
            f"Flood: {setting(cid, 'flood_limit', FLOOD_LIMIT)}\n"
            f"Locks: {setting(cid, 'locks', 'none')}\n"
            "Use /settings for configuration."
        )
    elif q.data == "security":
        text = (
            "🛡 SECURITY\n"
            "5 warnings = mute\n"
            "10 distinct successful bans by one admin = demotion attempt\n"
            "Use /lock and /setflood."
        )
    else:
        text = setting(cid, "rules", "Rules abhi set nahi hain.")

    try:
        await q.edit_message_text(text)
    except TelegramError:
        await q.message.reply_text(text)


# =========================================================
# BAN / UNBAN / KICK
# =========================================================

async def ban_cmd(update, context):
    if not await require_admin(update, context):
        return

    msg = update.effective_message
    target = replied_user(update)
    target_id = target.id if target else None

    if not target_id and context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            pass

    if not target_id:
        await msg.reply_text("User ke message par reply karke /ban likho.")
        return

    try:
        await context.bot.ban_chat_member(
            update.effective_chat.id, target_id
        )

        # Count the command's successful ban immediately.
        await record_ban(
            context,
            update.effective_chat.id,
            update.effective_user.id,
            target_id,
            update.effective_user.full_name,
        )

        await msg.reply_text(f"🚫 User {target_id} banned.")
    except TelegramError as exc:
        await msg.reply_text(f"Ban failed: {exc}")


async def unban_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    uid = target.id if target else None

    if not uid and context.args:
        try:
            uid = int(context.args[0])
        except ValueError:
            pass

    if not uid:
        await update.effective_message.reply_text(
            "Reply to a user or use /unban USER_ID."
        )
        return

    try:
        await context.bot.unban_chat_member(
            update.effective_chat.id, uid, only_if_banned=True
        )
        await update.effective_message.reply_text("Ban removed.")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Unban failed: {exc}")


async def kick_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "User ke message par reply karke /kick likho."
        )
        return

    try:
        await context.bot.ban_chat_member(
            update.effective_chat.id, target.id
        )
        await context.bot.unban_chat_member(
            update.effective_chat.id, target.id, only_if_banned=True
        )
        await update.effective_message.reply_text(
            f"👢 {target.full_name} removed."
        )
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Kick failed: {exc}")


# =========================================================
# MUTE / TEMP ACTIONS
# =========================================================

async def mute_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "User ke message par reply karke /mute likho."
        )
        return

    try:
        await mute_user(context.bot, update.effective_chat.id, target.id)
        await update.effective_message.reply_text(
            f"🔇 {target.full_name} muted."
        )
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Mute failed: {exc}")


async def unmute_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "Reply karke /unmute likho."
        )
        return

    try:
        await unmute_user(context.bot, update.effective_chat.id, target.id)
        await update.effective_message.reply_text("User unmuted.")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Unmute failed: {exc}")


async def temp_cmd(update, context, mode):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target or not context.args:
        await update.effective_message.reply_text(
            f"Reply to user: /{mode} 30m, 2h, 1d"
        )
        return

    duration = parse_duration(context.args[0])
    if not duration:
        await update.effective_message.reply_text(
            "Valid duration: 30s, 15m, 2h, 3d."
        )
        return

    try:
        if mode == "tban":
            await context.bot.ban_chat_member(
                update.effective_chat.id,
                target.id,
                until_date=datetime.now(timezone.utc) + duration,
            )
        else:
            await mute_user(
                context.bot, update.effective_chat.id,
                target.id, duration,
            )

        await update.effective_message.reply_text(
            f"Temporary {mode} applied."
        )
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Action failed: {exc}")


async def tban_cmd(u, c):
    await temp_cmd(u, c, "tban")


async def tmute_cmd(u, c):
    await temp_cmd(u, c, "tmute")


# =========================================================
# WARNINGS
# =========================================================

async def warn_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "Reply to the member's message and use /warn."
        )
        return

    cid = update.effective_chat.id

    with connect() as con:
        row = con.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
            (cid, target.id),
        ).fetchone()
        count = (row["count"] if row else 0) + 1

        con.execute("""
            INSERT INTO warnings(chat_id,user_id,count)
            VALUES(?,?,?)
            ON CONFLICT(chat_id,user_id)
            DO UPDATE SET count=excluded.count
        """, (cid, target.id, count))

    log_event(cid, update.effective_user.id, "warn", target.id, str(count))

    if count >= WARN_LIMIT:
        try:
            await mute_user(context.bot, cid, target.id)
            await update.effective_message.reply_text(
                f"⚠️ {target.full_name}: {count} warnings. Muted."
            )
        except TelegramError as exc:
            await update.effective_message.reply_text(
                f"Warning saved, but mute failed: {exc}"
            )
    else:
        await update.effective_message.reply_text(
            f"⚠️ Warning {count}/{WARN_LIMIT}."
        )


async def warns_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "Reply to a member's message and use /warns."
        )
        return

    with connect() as con:
        row = con.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
            (update.effective_chat.id, target.id),
        ).fetchone()

    await update.effective_message.reply_text(
        f"Warnings: {row['count'] if row else 0}"
    )


async def resetwarns_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "Reply to a member and use /resetwarns."
        )
        return

    with connect() as con:
        con.execute(
            "DELETE FROM warnings WHERE chat_id=? AND user_id=?",
            (update.effective_chat.id, target.id),
        )

    await update.effective_message.reply_text("Warnings reset.")


# =========================================================
# PURGE
# =========================================================

async def purge_cmd(update, context):
    if not await require_admin(update, context):
        return

    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text(
            "Jis message se delete shuru karna hai us par reply karke /purge likho."
        )
        return

    cid = update.effective_chat.id
    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    deleted = 0

    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(cid, mid)
            deleted += 1
        except TelegramError:
            pass

    log_event(cid, update.effective_user.id, "purge", details=str(deleted))

    try:
        status = await context.bot.send_message(
            cid, f"🧹 Purge complete. Deleted: {deleted}"
        )
        context.job_queue.run_once(
            delete_status, 5,
            data=(cid, status.message_id),
        )
    except TelegramError:
        pass


async def delete_status(context):
    cid, mid = context.job.data
    try:
        await context.bot.delete_message(cid, mid)
    except TelegramError:
        pass


# =========================================================
# PROMOTE / DEMOTE / ADMIN LIST
# =========================================================

async def promote_cmd(update, context):
    if not is_group(update):
        return

    actor = update.effective_user
    target = replied_user(update)

    if not actor or not target:
        await update.effective_message.reply_text(
            "Jis user ko promote karna hai uske message par reply karo."
        )
        return

    # Special authorization for configured owner.
    # Other admins must have Telegram's Add New Admins permission.
    if actor.id != OWNER_ID:
        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id, actor.id
            )
            if (
                member.status != ChatMemberStatus.ADMINISTRATOR
                or not getattr(member, "can_promote_members", False)
            ):
                await update.effective_message.reply_text(
                    "Add New Admins permission required."
                )
                return
        except TelegramError:
            await update.effective_message.reply_text(
                "Admin permissions verify nahi hui."
            )
            return

    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id,
            target.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_manage_video_chats=True,
            can_restrict_members=True,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=True,
            can_pin_messages=True,
            can_manage_topics=True,
        )
        await update.effective_message.reply_text(
            f"✅ {target.full_name} promoted."
        )
        log_event(
            update.effective_chat.id, actor.id,
            "promote", target.id,
        )
    except TelegramError as exc:
        await update.effective_message.reply_text(
            f"Promotion failed: {exc}"
        )


async def demote_cmd(update, context):
    if not await require_admin(update, context):
        return

    target = replied_user(update)
    if not target:
        await update.effective_message.reply_text(
            "Reply to an admin and use /demote."
        )
        return

    try:
        ok, reason = await demote_user(
            context.bot, update.effective_chat.id, target.id
        )
        await update.effective_message.reply_text(
            f"{'✅' if ok else '⚠️'} {reason}"
        )
    except TelegramError as exc:
        await update.effective_message.reply_text(
            f"Demotion failed: {exc}"
        )


async def adminlist_cmd(update, context):
    if not is_group(update):
        return

    try:
        admins = await context.bot.get_chat_administrators(
            update.effective_chat.id
        )
        lines = ["👑 GROUP ADMINS"]
        for item in admins:
            lines.append(f"• {item.user.full_name} — {item.user.id}")

        await update.effective_message.reply_text("\n".join(lines))
    except TelegramError as exc:
        await update.effective_message.reply_text(str(exc))


# =========================================================
# WELCOME / GOODBYE / CAPTCHA
# =========================================================

async def new_members(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    for user in msg.new_chat_members:
        if user.id == context.bot.id:
            continue

        if setting(chat.id, "captcha", "off") == "on":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Verify",
                    callback_data=f"verify:{chat.id}:{user.id}",
                )
            ]])

            try:
                await context.bot.restrict_chat_member(
                    chat.id,
                    user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                )
            except TelegramError:
                pass

            await msg.reply_text(
                f"🔐 {user.full_name}, verify karne ke liye button dabao.",
                reply_markup=keyboard,
            )
        else:
            welcome = setting(
                chat.id, "welcome",
                "🌹 Welcome {name} to {chat}!",
            )
            await msg.reply_text(
                welcome.replace("{name}", user.full_name)
                       .replace("{chat}", chat.title or "group")
            )


async def left_member(update, context):
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return

    goodbye = setting(
        update.effective_chat.id,
        "goodbye",
        "👋 Goodbye {name}!",
    )
    await msg.reply_text(
        goodbye.replace("{name}", msg.left_chat_member.full_name)
    )


async def verify_callback(update, context):
    q = update.callback_query
    await q.answer()

    try:
        _, chat_id, user_id = q.data.split(":")
        chat_id, user_id = int(chat_id), int(user_id)
    except (ValueError, AttributeError):
        return

    if q.from_user.id != user_id:
        await q.answer("Ye verification button aapka nahi hai.", show_alert=True)
        return

    try:
        await unmute_user(context.bot, chat_id, user_id)

        with connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO verified(chat_id,user_id) VALUES(?,?)",
                (chat_id, user_id),
            )

        await q.edit_message_text("✅ Verification successful.")
    except TelegramError as exc:
        await q.edit_message_text(f"Verification failed: {exc}")


async def setwelcome_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setwelcome Welcome {name} to {chat}"
        )
        return

    set_setting(update.effective_chat.id, "welcome", " ".join(context.args))
    await update.effective_message.reply_text("Welcome message saved.")


async def setgoodbye_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setgoodbye Goodbye {name}"
        )
        return

    set_setting(update.effective_chat.id, "goodbye", " ".join(context.args))
    await update.effective_message.reply_text("Goodbye message saved.")


async def captcha_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text(
            "Usage: /captcha on OR /captcha off"
        )
        return

    set_setting(update.effective_chat.id, "captcha", context.args[0].lower())
    await update.effective_message.reply_text(
        f"CAPTCHA {context.args[0].upper()}."
    )


# =========================================================
# RULES / SETTINGS / LOGS
# =========================================================

async def setrules_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setrules Your rules..."
        )
        return

    set_setting(update.effective_chat.id, "rules", " ".join(context.args))
    await update.effective_message.reply_text("Rules saved.")


async def rules_cmd(update, context):
    await update.effective_message.reply_text(
        setting(update.effective_chat.id, "rules", "Rules set nahi hain.")
    )


async def settings_cmd(update, context):
    if not is_group(update):
        return

    cid = update.effective_chat.id
    text = (
        "⚙️ GROUP SETTINGS\n"
        f"CAPTCHA: {setting(cid, 'captcha', 'off')}\n"
        f"Flood: {setting(cid, 'flood_limit', FLOOD_LIMIT)}\n"
        f"Locks: {setting(cid, 'locks', 'none')}\n"
    )
    await update.effective_message.reply_text(text)


async def setlog_cmd(update, context):
    if not await require_admin(update, context):
        return

    set_setting(update.effective_chat.id, "log_chat", update.effective_chat.id)
    await update.effective_message.reply_text(
        "Log destination set to this group."
    )


# =========================================================
# LOCKS / FLOOD
# =========================================================

ALLOWED_LOCKS = {"links", "media", "stickers", "gifs", "forwards"}


def current_locks(chat_id):
    return set(filter(None, setting(chat_id, "locks", "").split(",")))


def save_locks(chat_id, values):
    set_setting(chat_id, "locks", ",".join(sorted(values)))


async def lock_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /lock links|media|stickers|gifs|forwards"
        )
        return

    key = context.args[0].lower()
    if key not in ALLOWED_LOCKS:
        await update.effective_message.reply_text("Unknown lock type.")
        return

    locks = current_locks(update.effective_chat.id)
    locks.add(key)
    save_locks(update.effective_chat.id, locks)
    await update.effective_message.reply_text(f"🔒 {key} locked.")


async def unlock_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /unlock links")
        return

    key = context.args[0].lower()
    locks = current_locks(update.effective_chat.id)
    locks.discard(key)
    save_locks(update.effective_chat.id, locks)
    await update.effective_message.reply_text(f"🔓 {key} unlocked.")


async def locks_cmd(update, context):
    locks = current_locks(update.effective_chat.id)
    await update.effective_message.reply_text(
        "Active locks: " + (", ".join(sorted(locks)) or "none")
    )


async def setflood_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /setflood 6")
        return

    n = int(context.args[0])
    if not 2 <= n <= 30:
        await update.effective_message.reply_text("Limit 2-30 ke beech honi chahiye.")
        return

    set_setting(update.effective_chat.id, "flood_limit", n)
    await update.effective_message.reply_text(
        f"Flood limit set to {n} messages per {FLOOD_SECONDS} seconds."
    )


# =========================================================
# FILTERS / NOTES
# =========================================================

async def filter_cmd(update, context):
    if not await require_admin(update, context):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /filter keyword reply text"
        )
        return

    key = context.args[0].lower()
    reply = " ".join(context.args[1:])

    with connect() as con:
        con.execute("""
            INSERT INTO filters(chat_id,keyword,reply)
            VALUES(?,?,?)
            ON CONFLICT(chat_id,keyword)
            DO UPDATE SET reply=excluded.reply
        """, (update.effective_chat.id, key, reply))

    await update.effective_message.reply_text(f"Filter saved: {key}")


async def stop_filter_cmd(update, context):
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /stop keyword")
        return

    with connect() as con:
        con.execute(
            "DELETE FROM filters WHERE chat_id=? AND keyword=?",
            (update.effective_chat.id, context.args[0].lower()),
        )

    await update.effective_message.reply_text("Filter removed.")


async def filters_cmd(update, context):
    with connect() as con:
        rows = con.execute(
            "SELECT keyword FROM filters WHERE chat_id=? ORDER BY keyword",
            (update.effective_chat.id,),
        ).fetchall()

    await update.effective_message.reply_text(
        "Filters:\n" + ("\n".join("• " + r["keyword"] for r in rows) or "None")
    )


async def save_cmd(update, context):
    if not await require_admin(update, context):
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /save name content")
        return

    name = context.args[0].lower()
    content = " ".join(context.args[1:])

    with connect() as con:
        con.execute("""
            INSERT INTO notes(chat_id,name,content)
            VALUES(?,?,?)
            ON CONFLICT(chat_id,name)
            DO UPDATE SET content=excluded.content
        """, (update.effective_chat.id, name, content))

    await update.effective_message.reply_text(f"Note {name} saved.")


async def get_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage: /get name")
        return

    with connect() as con:
        row = con.execute(
            "SELECT content FROM notes WHERE chat_id=? AND name=?",
            (update.effective_chat.id, context.args[0].lower()),
        ).fetchone()

    await update.effective_message.reply_text(
        row["content"] if row else "Note not found."
    )


async def notes_cmd(update, context):
    with connect() as con:
        rows = con.execute(
            "SELECT name FROM notes WHERE chat_id=? ORDER BY name",
            (update.effective_chat.id,),
        ).fetchall()

    await update.effective_message.reply_text(
        "Notes:\n" + ("\n".join("• " + r["name"] for r in rows) or "None")
    )


# =========================================================
# REPORTS
# =========================================================

async def report_cmd(update, context):
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text(
            "Report karne wale message par reply karke /report likho."
        )
        return

    target = msg.reply_to_message.from_user
    text = (
        "🚨 MEMBER REPORT\n"
        f"Reporter: {update.effective_user.full_name} "
        f"({update.effective_user.id})\n"
        f"Target: {target.full_name} ({target.id})\n"
        f"Message ID: {msg.reply_to_message.message_id}"
    )

    await notify_admins(context, update.effective_chat.id, text)
    await log_to_channel(context, update.effective_chat.id, text)
    await msg.reply_text("Report admins ko bhej di gayi hai.")


# =========================================================
# MESSAGE PROTECTION / FLOOD / FILTERS
# =========================================================

def contains_link(text):
    return bool(text and re.search(
        r"(https?://|www\.|t\.me/|telegram\.me/|tg://)",
        text,
        re.IGNORECASE,
    ))


async def moderate_message(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user or not is_group(update):
        return

    # Do not apply member locks to administrators.
    if await is_admin(context.bot, chat.id, user.id):
        return

    locks = current_locks(chat.id)
    text = msg.text or msg.caption or ""
    reason = ""

    if "links" in locks and contains_link(text):
        reason = "links"

    if "media" in locks and any((
        msg.photo, msg.video, msg.document, msg.audio,
        msg.voice, msg.video_note,
    )):
        reason = "media"

    if "stickers" in locks and msg.sticker:
        reason = "stickers"

    if "gifs" in locks and msg.animation:
        reason = "gifs"

    if "forwards" in locks and msg.forward_origin:
        reason = "forwards"

    if reason:
        try:
            await msg.delete()
            log_event(chat.id, context.bot.id, "locked_message", user.id, reason)
        except TelegramError:
            pass
        return

    # Flood control. Recent messages are kept in process memory.
    now = time.monotonic()
    key = (chat.id, user.id)
    history = context.application.bot_data.setdefault("flood_history", {})
    recent = [
        t for t in history.get(key, [])
        if now - t <= FLOOD_SECONDS
    ]
    recent.append(now)
    history[key] = recent

    limit = int(setting(chat.id, "flood_limit", FLOOD_LIMIT))

    if len(recent) > limit:
        try:
            await mute_user(
                context.bot, chat.id, user.id, timedelta(minutes=5)
            )
        except TelegramError:
            pass

        try:
            await msg.delete()
        except TelegramError:
            pass

        await notify_admins(
            context, chat.id,
            f"🚨 Flood detected: {user.full_name} ({user.id}).",
        )
        return

    # Keyword filters.
    if text:
        with connect() as con:
            rows = con.execute(
                "SELECT keyword,reply FROM filters WHERE chat_id=?",
                (chat.id,),
            ).fetchall()

        lower = text.lower()
        for row in rows:
            if row["keyword"] in lower:
                await msg.reply_text(row["reply"])
                break





async def myid_cmd(update, context):
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"Your Telegram user ID: {user.id}")


# =========================================================
# MERGED JOIN-REQUEST MANAGEMENT FEATURES
# =========================================================

JOIN_DESIGNS = [
    {"name": "Classic", "text": "🔔 <b>NEW JOIN REQUEST</b>\n\n👤 Name: {name}\n🆔 User ID: <code>{user_id}</code>\n🔗 Username: {username}\n💬 Group: {chat_title}\n\nChoose an action below."},
    {"name": "Premium", "text": "╔══════ ✦ JOIN REQUEST ✦ ══════╗\n\n👑 <b>New member waiting for approval</b>\n\n🙍 <b>Name:</b> {name}\n🆔 <b>ID:</b> <code>{user_id}</code>\n🔗 <b>Username:</b> {username}\n🏠 <b>Group:</b> {chat_title}\n\n╚══════════════════════════════╝"},
    {"name": "Minimal", "text": "📩 <b>Join request</b>\n\n{name} | <code>{user_id}</code>\n{username}\nGroup: {chat_title}"},
    {"name": "Modern", "text": "🚀 <b>MEMBER REQUEST RECEIVED</b>\n\n┌ 👤 {name}\n├ 🆔 <code>{user_id}</code>\n├ 🔗 {username}\n└ 🏠 {chat_title}\n\n⚙️ Select an action:"},
    {"name": "Security", "text": "🛡️ <b>JOIN REQUEST SECURITY CHECK</b>\n\nA user is waiting to join the group.\n\n👤 {name}\n🆔 <code>{user_id}</code>\n🔗 {username}\n📍 {chat_title}\n\nReview the request before deciding."},
    {"name": "Emoji", "text": "✨✨✨✨✨✨✨✨\n📨 <b>NEW REQUEST!</b>\n✨✨✨✨✨✨✨✨\n\n🙋 {name}\n🆔 <code>{user_id}</code>\n🔗 {username}\n🏡 {chat_title}\n\n👇 <b>Choose below</b>"},
    {"name": "Professional", "text": "📋 <b>GROUP ACCESS REVIEW</b>\n\nStatus: <b>Pending</b>\nApplicant: {name}\nTelegram ID: <code>{user_id}</code>\nUsername: {username}\nDestination: {chat_title}\n\nAction is required from a group administrator."},
    {"name": "Neon", "text": "⚡ <b>NEW ACCESS REQUEST</b> ⚡\n\n👤 <b>{name}</b>\n🆔 <code>{user_id}</code>\n🔗 {username}\n🌐 {chat_title}\n\n⚡ <i>Waiting for admin decision...</i>"},
    {"name": "Friendly", "text": "👋 <b>Someone wants to join!</b>\n\nSay hello to: {name}\nTheir ID: <code>{user_id}</code>\nUsername: {username}\nGroup: {chat_title}\n\nAn admin can approve or decline below."},
    {"name": "Detailed", "text": "🔎 <b>JOIN REQUEST DETAILS</b>\n\n• <b>Name:</b> {name}\n• <b>User ID:</b> <code>{user_id}</code>\n• <b>Username:</b> {username}\n• <b>Group:</b> {chat_title}\n• <b>Status:</b> Pending\n\nSelect Accept or Decline to process this request."},
]


def init_join_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS join_users(
            user_id INTEGER PRIMARY KEY,
            first_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            started_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS join_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS join_request_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            chat_title TEXT DEFAULT '',
            user_id INTEGER,
            username TEXT DEFAULT '',
            action TEXT DEFAULT '',
            actor_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS bot_blocklist(
            user_id INTEGER PRIMARY KEY,
            reason TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        """)
        con.execute("INSERT OR IGNORE INTO join_settings(key,value) VALUES('active_design','0')")
        con.execute("INSERT OR IGNORE INTO join_settings(key,value) VALUES('dm_template','')")


def join_setting(key, default=""):
    with connect() as con:
        row = con.execute("SELECT value FROM join_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_join_setting(key, value):
    with connect() as con:
        con.execute("""
            INSERT INTO join_settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def save_join_user(user):
    if not user:
        return
    with connect() as con:
        con.execute("""
            INSERT INTO join_users(user_id,first_name,username,started_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name, username=excluded.username
        """, (user.id, user.first_name or "", user.username or "",
              datetime.now(timezone.utc).isoformat()))


def join_log(chat_id, chat_title, user_id, username, action, actor_id=0):
    with connect() as con:
        con.execute("""
            INSERT INTO join_request_logs
            (chat_id,chat_title,user_id,username,action,actor_id,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (chat_id, chat_title or "", user_id, username or "", action,
              actor_id, datetime.now(timezone.utc).isoformat()))


def join_is_blocked(user_id):
    with connect() as con:
        return con.execute("SELECT 1 FROM bot_blocklist WHERE user_id=?", (user_id,)).fetchone() is not None


def active_join_design():
    try:
        return int(join_setting("active_design", "0")) % len(JOIN_DESIGNS)
    except (ValueError, TypeError):
        return 0


def join_username(user):
    return "@" + user.username if user.username else "Not set"


def join_design_text(index, user, chat_title):
    # Escape user-controlled strings so HTML in names/group titles cannot break the message.
    return JOIN_DESIGNS[index]["text"].format(
        name=html.escape(user.full_name or "Unknown"),
        user_id=user.id,
        username=html.escape(join_username(user)),
        chat_title=html.escape(chat_title or "Unknown group"),
    )


def join_design_keyboard(index):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Previous", callback_data=f"jdesign_prev:{index}"),
            InlineKeyboardButton(f"{index + 1}/{len(JOIN_DESIGNS)}", callback_data="jdesign_info"),
            InlineKeyboardButton("Next ➡️", callback_data=f"jdesign_next:{index}"),
        ],
        [InlineKeyboardButton("✅ Use This Design", callback_data=f"jdesign_use:{index}")],
    ])


def join_request_keyboard(chat_id, user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"jreq_accept:{chat_id}:{user_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"jreq_decline:{chat_id}:{user_id}"),
    ]])


async def join_manager_only(update, context):
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        await update.effective_message.reply_text("❌ यह कमांड केवल configured bot owner के लिए है।")
        return False
    return True


async def panel_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    idx = active_join_design()
    with connect() as con:
        users = con.execute("SELECT COUNT(*) n FROM join_users").fetchone()["n"]
        requests = con.execute("SELECT COUNT(*) n FROM join_request_logs").fetchone()["n"]
        blocked = con.execute("SELECT COUNT(*) n FROM bot_blocklist").fetchone()["n"]
    await update.effective_message.reply_text(
        "🛠️ BOT OWNER PANEL\n\n"
        f"🎨 Join design: {idx + 1}/{len(JOIN_DESIGNS)} — {JOIN_DESIGNS[idx]['name']}\n"
        f"💬 Auto-DM: {'Configured' if join_setting('dm_template') else 'Not configured'}\n"
        f"👥 Users who started bot: {users}\n"
        f"📋 Request actions logged: {requests}\n"
        f"🚫 Bot blocklist: {blocked}\n\n"
        "Commands: /design /setdm /testdm /stats /requestlogs /broadcast /botban /botunban"
    )


async def join_stats_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    with connect() as con:
        users = con.execute("SELECT COUNT(*) n FROM join_users").fetchone()["n"]
        total = con.execute("SELECT COUNT(*) n FROM join_request_logs").fetchone()["n"]
        accepted = con.execute("SELECT COUNT(*) n FROM join_request_logs WHERE action='accepted'").fetchone()["n"]
        declined = con.execute("SELECT COUNT(*) n FROM join_request_logs WHERE action='declined'").fetchone()["n"]
        blocked = con.execute("SELECT COUNT(*) n FROM bot_blocklist").fetchone()["n"]
    await update.effective_message.reply_text(
        f"📊 BOT STATISTICS\n\n👥 Started users: {users}\n📋 Logged actions: {total}\n"
        f"✅ Accepted: {accepted}\n❌ Declined: {declined}\n🚫 Blocklisted: {blocked}"
    )


async def join_design_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    idx = active_join_design()
    await update.effective_message.reply_text(
        f"🎨 Join notification design {idx + 1}/{len(JOIN_DESIGNS)}\n"
        f"Style: {JOIN_DESIGNS[idx]['name']}\nBrowse Previous/Next, then activate.",
        reply_markup=join_design_keyboard(idx),
    )


async def join_design_callback(query, context, data):
    if query.from_user.id != OWNER_ID:
        await query.answer("केवल bot owner design बदल सकता है।", show_alert=True)
        return
    if data == "jdesign_info":
        await query.answer(f"{len(JOIN_DESIGNS)} designs available.")
        return
    try:
        action, num = data.split(":")
        idx = int(num) % len(JOIN_DESIGNS)
    except (ValueError, TypeError):
        await query.answer("Invalid design.")
        return
    if action == "jdesign_prev":
        idx = (idx - 1) % len(JOIN_DESIGNS)
    elif action == "jdesign_next":
        idx = (idx + 1) % len(JOIN_DESIGNS)
    elif action == "jdesign_use":
        set_join_setting("active_design", idx)
        await query.answer(f"Design {idx + 1} activated.", show_alert=True)
        if query.message:
            await query.edit_message_text(
                f"✅ Active design: {JOIN_DESIGNS[idx]['name']} ({idx + 1}/{len(JOIN_DESIGNS)})"
            )
        return
    else:
        await query.answer("Unknown action.")
        return
    await query.answer()
    if query.message:
        await query.edit_message_text(
            f"🎨 Join notification design {idx + 1}/{len(JOIN_DESIGNS)}\n"
            f"Style: {JOIN_DESIGNS[idx]['name']}",
            reply_markup=join_design_keyboard(idx),
        )


async def setdm_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    template = " ".join(context.args).strip()
    if not template:
        await update.effective_message.reply_text(
            "उपयोग: /setdm Welcome {name}! ID: {user_id}, Username: {username}"
        )
        return
    try:
        template.format(name="Example", username="@example", user_id=12345)
    except (KeyError, ValueError, IndexError) as exc:
        await update.effective_message.reply_text(f"❌ Template गलत है: {exc}")
        return
    set_join_setting("dm_template", template)
    await update.effective_message.reply_text("✅ Auto-DM template सेव हो गया।")


async def testdm_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    template = join_setting("dm_template")
    if not template:
        await update.effective_message.reply_text("पहले /setdm से template सेट करें।")
        return
    user = update.effective_user
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=template.format(name=user.full_name or "Owner",
                                 username=join_username(user), user_id=user.id),
        )
        await update.effective_message.reply_text("✅ Test DM भेज दिया गया।")
    except TelegramError as exc:
        await update.effective_message.reply_text(f"❌ DM नहीं भेज सके: {exc}")


async def send_join_dm(context, user):
    template = join_setting("dm_template")
    if not template:
        return
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=template.format(name=user.full_name or "User",
                                 username=join_username(user), user_id=user.id),
        )
    except (Forbidden, TelegramError):
        logger.info("Auto-DM unavailable for user %s; user may not have started bot", user.id)
    except (KeyError, ValueError, IndexError):
        logger.exception("Invalid saved auto-DM template")


async def on_join_request(update, context):
    request = update.chat_join_request
    if not request:
        return
    chat, user = request.chat, request.from_user
    save_join_user(user)
    if join_is_blocked(user.id):
        try:
            await context.bot.decline_chat_join_request(chat_id=chat.id, user_id=user.id)
            join_log(chat.id, chat.title, user.id, user.username, "declined_blocklisted")
        except TelegramError:
            logger.exception("Could not decline blocklisted join request")
        return

    idx = active_join_design()
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=join_design_text(idx, user, chat.title),
            parse_mode="HTML",
            reply_markup=join_request_keyboard(chat.id, user.id),
        )
    except TelegramError:
        logger.exception("Could not send join-request notification to %s", chat.id)
    join_log(chat.id, chat.title, user.id, user.username, "pending")
    await send_join_dm(context, user)


async def join_request_callback(query, context, data):
    try:
        action, chat_id_text, user_id_text = data.split(":")
        chat_id, user_id = int(chat_id_text), int(user_id_text)
    except (ValueError, TypeError):
        await query.answer("Invalid request button.", show_alert=True)
        return
    if not await is_admin(context.bot, chat_id, query.from_user.id):
        await query.answer("❌ केवल group admins यह बटन इस्तेमाल कर सकते हैं।", show_alert=True)
        return
    try:
        if action == "jreq_accept":
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            result, response = "accepted", "✅ Join request accepted."
        elif action == "jreq_decline":
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            result, response = "declined", "❌ Join request declined."
        else:
            await query.answer("Unknown action.")
            return
        join_log(chat_id, "", user_id, "", result, query.from_user.id)
    except TelegramError:
        logger.exception("Join request action failed")
        await query.answer(
            "Action failed. Bot admin permissions aur pending request check karein.",
            show_alert=True,
        )
        return
    await query.answer(response, show_alert=True)
    if query.message:
        old_text = query.message.text_html or query.message.text or ""
        try:
            await query.edit_message_text(
                old_text + f"\n\n<b>Status:</b> {result.title()}\n"
                f"<b>By admin ID:</b> <code>{query.from_user.id}</code>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramError:
            pass


async def requestlogs_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    with connect() as con:
        rows = con.execute("""
            SELECT chat_title,user_id,username,action,actor_id,created_at
            FROM join_request_logs ORDER BY id DESC LIMIT 15
        """).fetchall()
    if not rows:
        await update.effective_message.reply_text("अभी कोई join-request logs नहीं हैं।")
        return
    lines = ["📜 RECENT JOIN-REQUEST LOGS\n"]
    for row in rows:
        lines.append(
            f"• {row['action']} | {row['chat_title'] or 'Group'} | "
            f"ID: {row['user_id']} | @{row['username'] or 'none'} | "
            f"Actor: {row['actor_id']}\n{row['created_at']}"
        )
    await update.effective_message.reply_text("\n".join(lines)[:3900])


async def broadcast_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    message = " ".join(context.args).strip()
    if not message:
        await update.effective_message.reply_text("उपयोग: /broadcast आपका संदेश")
        return
    with connect() as con:
        rows = con.execute("SELECT user_id FROM join_users").fetchall()
    sent = failed = 0
    status = await update.effective_message.reply_text("📣 Broadcast शुरू हो गया...")
    for row in rows:
        try:
            await context.bot.send_message(chat_id=row["user_id"], text=message)
            sent += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1)))
            try:
                await context.bot.send_message(chat_id=row["user_id"], text=message)
                sent += 1
            except TelegramError:
                failed += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"📣 Broadcast पूरा हुआ\n\n✅ Sent: {sent}\n❌ Failed: {failed}")


async def botban_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("उपयोग: /botban USER_ID [reason]")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID नंबर होना चाहिए।")
        return
    reason = " ".join(context.args[1:]).strip()
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO bot_blocklist(user_id,reason,created_at) VALUES(?,?,?)",
            (uid, reason, datetime.now(timezone.utc).isoformat()),
        )
    await update.effective_message.reply_text(f"🚫 User {uid} bot blocklist में जोड़ दिया गया।")


async def botunban_cmd(update, context):
    if not await join_manager_only(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("उपयोग: /botunban USER_ID")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID नंबर होना चाहिए।")
        return
    with connect() as con:
        cur = con.execute("DELETE FROM bot_blocklist WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(
        f"✅ User {uid} blocklist से हटा दिया गया।" if cur.rowcount else "यह user blocklist में नहीं था।"
    )



# =========================================================
# ERROR HANDLER
# =========================================================


async def join_design_callback_router(update, context):
    query = update.callback_query
    if query:
        await join_design_callback(query, context, query.data)


async def join_request_callback_router(update, context):
    query = update.callback_query
    if query:
        await join_request_callback(query, context, query.data)


async def error_handler(update, context):
    log.error("Bot error: %s", context.error)


# =========================================================
# START BOT
# =========================================================

def main():
    if BOT_TOKEN == "PASTE_BOTFATHER_TOKEN_HERE":
        raise SystemExit(
            "Code mein BOT_TOKEN ko BotFather token se replace karo."
        )

    init_db()
    init_join_db()

    app = Application.builder().token(BOT_TOKEN).build()

    handlers = [
        ("start", start),
        ("help", help_cmd),

        ("ban", ban_cmd),
        ("unban", unban_cmd),
        ("kick", kick_cmd),
        ("mute", mute_cmd),
        ("unmute", unmute_cmd),
        ("tban", tban_cmd),
        ("tmute", tmute_cmd),
        ("warn", warn_cmd),
        ("warns", warns_cmd),
        ("resetwarns", resetwarns_cmd),
        ("purge", purge_cmd),
        ("promote", promote_cmd),
        ("demote", demote_cmd),
        ("adminlist", adminlist_cmd),

        ("setwelcome", setwelcome_cmd),
        ("setgoodbye", setgoodbye_cmd),
        ("captcha", captcha_cmd),
        ("setrules", setrules_cmd),
        ("rules", rules_cmd),
        ("settings", settings_cmd),
        ("setlog", setlog_cmd),

        ("lock", lock_cmd),
        ("unlock", unlock_cmd),
        ("locks", locks_cmd),
        ("setflood", setflood_cmd),

        ("filter", filter_cmd),
        ("stop", stop_filter_cmd),
        ("filters", filters_cmd),
        ("save", save_cmd),
        ("get", get_cmd),
        ("notes", notes_cmd),
        ("report", report_cmd),
        ("myid", myid_cmd),
        ("panel", panel_cmd),
        ("stats", join_stats_cmd),
        ("design", join_design_cmd),
        ("setdm", setdm_cmd),
        ("testdm", testdm_cmd),
        ("requestlogs", requestlogs_cmd),
        ("broadcast", broadcast_cmd),
        ("botban", botban_cmd),
        ("botunban", botunban_cmd),
    ]

    for command, callback in handlers:
        app.add_handler(CommandHandler(command, callback))

    app.add_handler(CallbackQueryHandler(
        verify_callback, pattern=r"^verify:"
    ))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(
        join_design_callback_router, pattern=r"^jdesign_"
    ))
    app.add_handler(CallbackQueryHandler(
        join_request_callback_router, pattern=r"^jreq_(accept|decline):"
    ))
    app.add_handler(CallbackQueryHandler(
        menu_callback, pattern=r"^(help|settings|security|rules)$"
    ))

    app.add_handler(ChatMemberHandler(
        member_update, ChatMemberHandler.CHAT_MEMBER
    ))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members
    ))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.LEFT_CHAT_MEMBER, left_member
    ))

    # Only ordinary group messages are sent to moderation.
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND,
        moderate_message,
    ), group=10)

    app.add_error_handler(error_handler)

    log.info("MissRose-style bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
