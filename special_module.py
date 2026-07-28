# ============================================================
# FEATURE: /special
# ------------------------------------------------------------
# For any channel the bot administers (from the existing `channels`
# registry), lets the owner configure:
#   - a DM message auto-sent the moment a new user joins that channel
#     (editable any time), containing either one Telegram folder-invite
#     link (t.me/addlist/...) or a manual list of individual channel
#     invite links -- Telegram does not allow silently auto-adding a
#     user to another chat with zero action from them, so this DM +
#     one-tap-join (with join-requests auto-approved on the target
#     chats) is the maximum automation actually possible.
#   - one attached group, with per-word auto-moderation: Delete mode
#     silently removes any message containing a listed word, Warn mode
#     replies with a warning instead of deleting.
#
# All config lives in D1 table `special_configs`, one row per main
# channel_id. Wired into app.py's chat_member update handler (new
# join detection) and handle_callback (spec_* callback_data prefix).
# ============================================================
import json
import logging

logger = logging.getLogger("atlas.special")

from core import d1_run, d1_select, tg_post, send_msg, db_get_channels

_SPECIAL_TABLE_ENSURED = False


async def _ensure_special_table():
    global _SPECIAL_TABLE_ENSURED
    if _SPECIAL_TABLE_ENSURED:
        return
    try:
        await d1_run(
            "CREATE TABLE IF NOT EXISTS special_configs ("
            "main_channel_id TEXT PRIMARY KEY, "
            "dm_message TEXT, "
            "folder_link TEXT, "
            "extra_links TEXT, "       # JSON list of individual invite links
            "group_id TEXT, "
            "group_mode TEXT, "        # 'delete' or 'warn'
            "banned_words TEXT, "      # JSON list of words
            "updated_at INTEGER)"
        )
        _SPECIAL_TABLE_ENSURED = True
    except Exception as e:
        logger.warning(f"[Special] ensure table warn: {e}")


async def get_special_config(main_channel_id: str) -> dict:
    await _ensure_special_table()
    rows = await d1_select(
        "SELECT * FROM special_configs WHERE main_channel_id=?1", [main_channel_id]
    )
    if not rows:
        return {
            "main_channel_id": main_channel_id, "dm_message": "", "folder_link": "",
            "extra_links": [], "group_id": "", "group_mode": "delete", "banned_words": [],
        }
    row = rows[0]
    return {
        "main_channel_id": main_channel_id,
        "dm_message": row.get("dm_message") or "",
        "folder_link": row.get("folder_link") or "",
        "extra_links": json.loads(row.get("extra_links") or "[]"),
        "group_id": row.get("group_id") or "",
        "group_mode": row.get("group_mode") or "delete",
        "banned_words": json.loads(row.get("banned_words") or "[]"),
    }


async def save_special_config(main_channel_id: str, **fields):
    """Upserts only the given fields, keeping the rest as-is."""
    import time
    await _ensure_special_table()
    cfg = await get_special_config(main_channel_id)
    cfg.update(fields)
    await d1_run(
        "INSERT INTO special_configs "
        "(main_channel_id, dm_message, folder_link, extra_links, group_id, group_mode, banned_words, updated_at) "
        "VALUES (?1,?2,?3,?4,?5,?6,?7,?8) "
        "ON CONFLICT(main_channel_id) DO UPDATE SET "
        "dm_message=excluded.dm_message, folder_link=excluded.folder_link, "
        "extra_links=excluded.extra_links, group_id=excluded.group_id, "
        "group_mode=excluded.group_mode, banned_words=excluded.banned_words, "
        "updated_at=excluded.updated_at",
        [
            main_channel_id, cfg["dm_message"], cfg["folder_link"],
            json.dumps(cfg["extra_links"], ensure_ascii=False), cfg["group_id"],
            cfg["group_mode"], json.dumps(cfg["banned_words"], ensure_ascii=False),
            int(time.time()),
        ],
    )


# ---- Pending text-input state (per admin uid), for the multi-step edit flow ----
# value: {"channel_id": str, "field": "dm_message"|"folder_link"|"extra_links"|"banned_words"|"group_id"}
SPECIAL_INPUT_PENDING: dict = {}


# ============================================================
# UI: channel list -> config menu
# ============================================================
async def show_special_channel_list(chat_id, edit_message_id=None):
    channels = await db_get_channels()
    if not channels:
        txt = "📢 কোনো channel সেভ নেই! আগে <code>/channel</code> দিয়ে channel add করো।"
        if edit_message_id:
            await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id,
                                                "text": txt, "parse_mode": "HTML"})
        else:
            await send_msg(chat_id, txt, parse_mode="HTML")
        return
    txt = "⚙️ <b>/special Setup</b>\n\nকোন channel এর জন্য setup করবে?"
    buttons = []
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        buttons.append([{"text": f"📢 {ch_name}", "callback_data": f"spch_{ch_id}"}])
    reply_markup = {"inline_keyboard": buttons}
    if edit_message_id:
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id,
                                            "text": txt, "parse_mode": "HTML", "reply_markup": reply_markup})
    else:
        await send_msg(chat_id, txt, parse_mode="HTML", reply_markup=reply_markup)


async def show_special_channel_menu(chat_id, message_id, channel_id: str):
    cfg = await get_special_config(channel_id)
    dm_preview = (cfg["dm_message"][:60] + "…") if len(cfg["dm_message"]) > 60 else (cfg["dm_message"] or "(সেট করা নেই)")
    link_mode = "📁 Folder link" if cfg["folder_link"] else (f"🔗 {len(cfg['extra_links'])}টা link" if cfg["extra_links"] else "(সেট করা নেই)")
    group_status = f"✅ Group সেট আছে ({cfg['group_mode']})" if cfg["group_id"] else "(কোনো group সেট নেই)"
    txt = (
        f"⚙️ <b>Special Setup</b>\n🔗 <code>{channel_id}</code>\n\n"
        f"💬 <b>DM Message:</b> {dm_preview}\n"
        f"🔗 <b>Linked channels:</b> {link_mode}\n"
        f"👥 <b>Attached Group:</b> {group_status}\n"
    )
    buttons = [
        [{"text": "💬 DM Message Set/Edit", "callback_data": f"spdm_{channel_id}"}],
        [{"text": "📁 Folder Link Set", "callback_data": f"spfl_{channel_id}"}],
        [{"text": "🔗 Individual Links Set", "callback_data": f"spil_{channel_id}"}],
        [{"text": "👥 Group Attach/Edit", "callback_data": f"spgr_{channel_id}"}],
        [{"text": "⬅️ Back", "callback_data": "spback"}],
    ]
    await tg_post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                        "text": txt, "parse_mode": "HTML",
                                        "reply_markup": {"inline_keyboard": buttons}})


async def show_special_group_menu(chat_id, message_id, channel_id: str):
    cfg = await get_special_config(channel_id)
    group_line = f"🔗 <code>{cfg['group_id']}</code>" if cfg["group_id"] else "(এখনো সেট নেই — group id/username পাঠাও)"
    words_line = ", ".join(cfg["banned_words"]) if cfg["banned_words"] else "(কোনো শব্দ সেট নেই)"
    mode_emoji = "🗑️ Delete" if cfg["group_mode"] == "delete" else "⚠️ Warn"
    txt = (
        f"👥 <b>Group Moderation</b>\n{group_line}\n\n"
        f"⚙️ Mode: {mode_emoji}\n"
        f"🚫 Banned words: {words_line}\n"
    )
    buttons = [
        [{"text": "🆔 Group ID Set", "callback_data": f"spgid_{channel_id}"}],
        [{"text": "🗑️ Delete Mode", "callback_data": f"spmode_delete_{channel_id}"},
         {"text": "⚠️ Warn Mode", "callback_data": f"spmode_warn_{channel_id}"}],
        [{"text": "🚫 Banned Words Set", "callback_data": f"spwords_{channel_id}"}],
        [{"text": "⬅️ Back", "callback_data": f"spch_{channel_id}"}],
    ]
    await tg_post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                        "text": txt, "parse_mode": "HTML",
                                        "reply_markup": {"inline_keyboard": buttons}})


# ============================================================
# Callback dispatch — call from app.py's handle_callback
# ============================================================
async def handle_special_callback(query: dict) -> bool:
    """Returns True if handled."""
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    msg_id = query["message"]["message_id"]
    uid = query["from"]["id"]

    if data == "spback":
        await show_special_channel_list(chat_id, edit_message_id=msg_id)
        return True

    if data.startswith("spch_"):
        channel_id = data[len("spch_"):]
        await show_special_channel_menu(chat_id, msg_id, channel_id)
        return True

    if data.startswith("spdm_"):
        channel_id = data[len("spdm_"):]
        SPECIAL_INPUT_PENDING[uid] = {"channel_id": channel_id, "field": "dm_message"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": "💬 এই channel-এ কেউ join করলে যে message DM এ পাবে সেটা লিখে পাঠাও:\n\n"
                    "(পরে যেকোনো সময় আবার এই অপশনে এসে edit করা যাবে)",
            "parse_mode": "HTML"})
        return True

    if data.startswith("spfl_"):
        channel_id = data[len("spfl_"):]
        SPECIAL_INPUT_PENDING[uid] = {"channel_id": channel_id, "field": "folder_link"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": "📁 Telegram folder-invite link দাও (t.me/addlist/... ফরম্যাটে) — "
                    "এটা সেট করলে individual links আর ব্যবহার হবে না, শুধু এই folder link-ই DM এ যাবে:",
            "parse_mode": "HTML"})
        return True

    if data.startswith("spil_"):
        channel_id = data[len("spil_"):]
        SPECIAL_INPUT_PENDING[uid] = {"channel_id": channel_id, "field": "extra_links"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": "🔗 প্রতিটা লাইনে একটা করে channel/group invite link পাঠাও:\n\n"
                    "<code>Link1\nLink2\nLink3</code>\n\n"
                    "এটা সেট করলে আগের folder link (থাকলে) সরে যাবে, এই list-ই ব্যবহার হবে।",
            "parse_mode": "HTML"})
        return True

    if data.startswith("spgr_"):
        channel_id = data[len("spgr_"):]
        await show_special_group_menu(chat_id, msg_id, channel_id)
        return True

    if data.startswith("spgid_"):
        channel_id = data[len("spgid_"):]
        SPECIAL_INPUT_PENDING[uid] = {"channel_id": channel_id, "field": "group_id"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": "🆔 Group এর ID বা @username পাঠাও (bot ওই group এ admin থাকতে হবে):",
            "parse_mode": "HTML"})
        return True

    if data.startswith("spmode_"):
        rest = data[len("spmode_"):]
        mode, channel_id = rest.split("_", 1)
        await save_special_config(channel_id, group_mode=mode)
        await show_special_group_menu(chat_id, msg_id, channel_id)
        return True

    if data.startswith("spwords_"):
        channel_id = data[len("spwords_"):]
        SPECIAL_INPUT_PENDING[uid] = {"channel_id": channel_id, "field": "banned_words"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": "🚫 যে শব্দগুলো লিখলে auto-action নেওয়া হবে, কমা দিয়ে আলাদা করে পাঠাও:\n\n"
                    "<code>শব্দ১, শব্দ২, badword3</code>\n\n"
                    "আগের list থাকলে এটা সম্পূর্ণ replace করবে।",
            "parse_mode": "HTML"})
        return True

    return False


async def handle_special_text_input(msg: dict) -> bool:
    """Called from handle_message for plain-text replies while a
    SPECIAL_INPUT_PENDING entry exists for this uid. Returns True if handled."""
    uid = msg.get("from", {}).get("id")
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    pending = SPECIAL_INPUT_PENDING.get(uid)
    if not pending or not text:
        return False

    channel_id = pending["channel_id"]
    field = pending["field"]

    if field == "dm_message":
        await save_special_config(channel_id, dm_message=text)
        await send_msg(chat_id, "✅ DM message সেভ হয়েছে।")
    elif field == "folder_link":
        await save_special_config(channel_id, folder_link=text, extra_links=[])
        await send_msg(chat_id, "✅ Folder link সেভ হয়েছে।")
    elif field == "extra_links":
        links = [l.strip() for l in text.splitlines() if l.strip()]
        await save_special_config(channel_id, extra_links=links, folder_link="")
        await send_msg(chat_id, f"✅ {len(links)}টা link সেভ হয়েছে।")
    elif field == "group_id":
        await save_special_config(channel_id, group_id=text)
        await send_msg(chat_id, "✅ Group সেট হয়েছে।")
    elif field == "banned_words":
        words = [w.strip() for w in text.split(",") if w.strip()]
        await save_special_config(channel_id, banned_words=words)
        await send_msg(chat_id, f"✅ {len(words)}টা শব্দ সেভ হয়েছে।")
    else:
        return False

    SPECIAL_INPUT_PENDING.pop(uid, None)
    return True


# ============================================================
# Runtime: new-join DM trigger + group word-moderation
# ============================================================
async def on_channel_join(chat_id, user_id: int):
    """Call this when a chat_member update shows a user's status
    changed to 'member' in a channel that has a special_configs row.
    Sends the configured DM with join links, best-effort (user may
    have DMs closed to the bot -- that's fine, just fails silently)."""
    cfg = await get_special_config(str(chat_id))
    if not cfg["dm_message"] and not cfg["folder_link"] and not cfg["extra_links"]:
        return  # not configured for this channel, nothing to do
    lines = [cfg["dm_message"]] if cfg["dm_message"] else []
    if cfg["folder_link"]:
        lines.append(f'\n📁 এক ট্যাপে সব চ্যানেল/গ্রুপে জয়েন করো:\n{cfg["folder_link"]}')
    elif cfg["extra_links"]:
        lines.append("\n🔗 নিচের লিংকগুলোতে জয়েন করো:")
        lines.extend(cfg["extra_links"])
    text = "\n".join(lines).strip()
    if not text:
        return
    try:
        await tg_post("sendMessage", {"chat_id": user_id, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        logger.info(f"[Special] DM to {user_id} failed (likely blocked bot): {e}")


async def maybe_approve_join_request(chat_id, user_id: int) -> bool:
    """If any special_configs row references this chat as a linked
    target, auto-approve the join request so the user's one tap on the
    DM'd link gets them straight in. Call this from a chat_join_request
    update handler. Returns True if approved."""
    try:
        r = await tg_post("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})
        return bool(r.get("ok"))
    except Exception as e:
        logger.warning(f"[Special] approveChatJoinRequest failed: {e}")
        return False


async def moderate_group_message(msg: dict) -> bool:
    """Call this from handle_message for every group/supergroup text
    message. Checks against that group's banned_words (if this group_id
    is attached to any special_configs row) and deletes/warns per mode.
    Returns True if action was taken (delete/warn) so the caller can
    skip any further processing of this message."""
    chat = msg.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return False
    group_id = str(chat.get("id"))
    text = (msg.get("text") or msg.get("caption") or "")
    if not text:
        return False

    await _ensure_special_table()
    rows = await d1_select(
        "SELECT * FROM special_configs WHERE group_id=?1", [group_id]
    )
    if not rows:
        return False
    row = rows[0]
    banned_words = json.loads(row.get("banned_words") or "[]")
    if not banned_words:
        return False

    text_lower = text.lower()
    hit = next((w for w in banned_words if w.lower() in text_lower), None)
    if not hit:
        return False

    mode = row.get("group_mode") or "delete"
    msg_id = msg.get("message_id")
    try:
        if mode == "delete":
            await tg_post("deleteMessage", {"chat_id": group_id, "message_id": msg_id})
        else:  # warn
            uname = msg.get("from", {}).get("first_name", "User")
            await tg_post("sendMessage", {
                "chat_id": group_id,
                "text": f"⚠️ {uname}, এই শব্দ ব্যবহার নিষেধ এই গ্রুপে।",
                "reply_to_message_id": msg_id,
            })
        return True
    except Exception as e:
        logger.warning(f"[Special] moderate action failed: {e}")
        return False
