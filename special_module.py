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
        await d1_run(
            "CREATE TABLE IF NOT EXISTS special_groups ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "main_channel_id TEXT, "
            "group_id TEXT, "
            "group_mode TEXT, "        # 'delete' or 'warn' (for banned_words)
            "banned_words TEXT, "      # JSON list of words
            "fl_enabled INTEGER DEFAULT 0, "   # forward/link filter on(1)/off(0)
            "fl_action TEXT DEFAULT 'delete', " # 'delete' | 'warn' | 'ban'
            "updated_at INTEGER)"
        )
        for stmt in (
            "ALTER TABLE special_groups ADD COLUMN fl_enabled INTEGER DEFAULT 0",
            "ALTER TABLE special_groups ADD COLUMN fl_action TEXT DEFAULT 'delete'",
            "ALTER TABLE special_groups ADD COLUMN delete_enabled INTEGER DEFAULT 0",
            "ALTER TABLE special_groups ADD COLUMN delete_words TEXT DEFAULT '[]'",
            "ALTER TABLE special_groups ADD COLUMN warn_enabled INTEGER DEFAULT 0",
            "ALTER TABLE special_groups ADD COLUMN warn_words TEXT DEFAULT '[]'",
            "ALTER TABLE special_groups ADD COLUMN ban_enabled INTEGER DEFAULT 0",
            "ALTER TABLE special_groups ADD COLUMN ban_words TEXT DEFAULT '[]'",
            "ALTER TABLE special_groups ADD COLUMN group_name TEXT DEFAULT ''",
        ):
            try:
                await d1_run(stmt)
            except Exception:
                pass  # column already exists
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
# For group fields ("group_id_new"/"group_words") a "group_row_id" key may also be present.
SPECIAL_INPUT_PENDING: dict = {}


# ============================================================
# Multi-group CRUD (special_groups table)
# ============================================================
async def list_special_groups(main_channel_id: str) -> list:
    await _ensure_special_table()
    rows = await d1_select(
        "SELECT * FROM special_groups WHERE main_channel_id=?1 ORDER BY id", [main_channel_id]
    )
    return [_row_to_group(r) for r in rows]


def _row_to_group(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "main_channel_id": r.get("main_channel_id") or "",
        "group_id": r.get("group_id") or "",
        "group_name": r.get("group_name") or "",
        "delete_enabled": bool(r.get("delete_enabled") or 0),
        "delete_words": json.loads(r.get("delete_words") or "[]"),
        "warn_enabled": bool(r.get("warn_enabled") or 0),
        "warn_words": json.loads(r.get("warn_words") or "[]"),
        "ban_enabled": bool(r.get("ban_enabled") or 0),
        "ban_words": json.loads(r.get("ban_words") or "[]"),
    }


async def get_special_group(row_id) -> dict:
    await _ensure_special_table()
    rows = await d1_select("SELECT * FROM special_groups WHERE id=?1", [row_id])
    if not rows:
        return {}
    return _row_to_group(rows[0])


async def add_special_group(group_id: str, main_channel_id: str = "", group_name: str = "") -> int:
    """Adds a new group. main_channel_id is optional/legacy — groups are
    managed globally now, not per-channel."""
    import time
    await _ensure_special_table()
    await d1_run(
        "INSERT INTO special_groups (main_channel_id, group_id, group_name, updated_at) VALUES (?1,?2,?3,?4)",
        [main_channel_id, group_id, group_name, int(time.time())],
    )
    rows = await d1_select(
        "SELECT id FROM special_groups WHERE group_id=?1 ORDER BY id DESC", [group_id],
    )
    return rows[0]["id"] if rows else None


async def update_special_group(row_id, **fields):
    import time
    await _ensure_special_table()
    cur = await get_special_group(row_id)
    if not cur:
        return
    cur.update(fields)
    await d1_run(
        "UPDATE special_groups SET delete_enabled=?1, delete_words=?2, "
        "warn_enabled=?3, warn_words=?4, ban_enabled=?5, ban_words=?6, updated_at=?7 WHERE id=?8",
        [
            1 if cur["delete_enabled"] else 0, json.dumps(cur["delete_words"], ensure_ascii=False),
            1 if cur["warn_enabled"] else 0, json.dumps(cur["warn_words"], ensure_ascii=False),
            1 if cur["ban_enabled"] else 0, json.dumps(cur["ban_words"], ensure_ascii=False),
            int(time.time()), row_id,
        ],
    )


async def delete_special_group(row_id):
    await _ensure_special_table()
    await d1_run("DELETE FROM special_groups WHERE id=?1", [row_id])


# ============================================================
# UI: main menu -> (Channel list | Group) -> config menu
# ============================================================
async def show_special_main_menu(chat_id, edit_message_id=None):
    txt = "⚙️ <b>/special Setup</b>\n\nকী নিয়ে কাজ করবে?"
    buttons = [
        [{"text": "📢 Channel List", "callback_data": "spmainch"}],
        [{"text": "👥 Group", "callback_data": "spmaingr"}],
    ]
    reply_markup = {"inline_keyboard": buttons}
    if edit_message_id:
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id,
                                            "text": txt, "parse_mode": "HTML", "reply_markup": reply_markup})
    else:
        await send_msg(chat_id, txt, parse_mode="HTML", reply_markup=reply_markup)


async def show_special_channel_list(chat_id, edit_message_id=None):
    channels = await db_get_channels()
    if not channels:
        txt = "📢 কোনো channel সেভ নেই! আগে <code>/channel</code> দিয়ে channel add করো।"
        buttons = [[{"text": "⬅️ Back", "callback_data": "spmainback"}]]
        if edit_message_id:
            await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id,
                                                "text": txt, "parse_mode": "HTML",
                                                "reply_markup": {"inline_keyboard": buttons}})
        else:
            await send_msg(chat_id, txt, parse_mode="HTML", reply_markup={"inline_keyboard": buttons})
        return
    txt = "📢 <b>Channel List</b>\n\nকোন channel এর জন্য setup করবে?"
    buttons = []
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        ch_name = ch.get("channel_name", ch_id)
        buttons.append([{"text": f"📢 {ch_name}", "callback_data": f"spch_{ch_id}"}])
    buttons.append([{"text": "⬅️ Back", "callback_data": "spmainback"}])
    reply_markup = {"inline_keyboard": buttons}
    if edit_message_id:
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id,
                                            "text": txt, "parse_mode": "HTML", "reply_markup": reply_markup})
    else:
        await send_msg(chat_id, txt, parse_mode="HTML", reply_markup=reply_markup)


async def list_all_special_groups() -> list:
    """All groups — for the global /special -> Group view. Groups are managed
    globally now, independent of any channel."""
    await _ensure_special_table()
    rows = await d1_select("SELECT * FROM special_groups ORDER BY id", [])
    return [_row_to_group(r) for r in (rows or [])]


def _nav_row():
    """Always shown at the bottom of every Group screen."""
    return [{"text": "📋 Main List", "callback_data": "spmaingr"},
            {"text": "⬅️ Back", "callback_data": "spmainback"}]


async def show_special_group_list_global(chat_id, message_id):
    """Top-level 'Group' option: every group, with full moderation setup."""
    groups = await list_all_special_groups()
    if groups:
        lines = ["👥 <b>Group List</b>\n"]
        for g in groups:
            d = "🗑️✅" if g["delete_enabled"] else "🗑️❌"
            w = "⚠️✅" if g["warn_enabled"] else "⚠️❌"
            b = "🚫✅" if g["ban_enabled"] else "🚫❌"
            name_part = f" — {g['group_name']}" if g["group_name"] else ""
            lines.append(f"<code>{g['group_id']}</code>{name_part}\n{d} {w} {b}")
        txt = "\n".join(lines)
    else:
        txt = "👥 <b>Group List</b>\n\n(কোনো group যোগ করা নেই)"
    buttons = [[{"text": f"⚙️ {g['group_name'] or g['group_id']}", "callback_data": f"spgview_{g['id']}"}] for g in groups]
    buttons.append([{"text": "➕ নতুন Group Add", "callback_data": "spgaddg"}])
    buttons.append([{"text": "⬅️ Back", "callback_data": "spmainback"}])
    await tg_post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                        "text": txt, "parse_mode": "HTML",
                                        "reply_markup": {"inline_keyboard": buttons}})


async def show_special_channel_menu(chat_id, message_id, channel_id: str):
    cfg = await get_special_config(channel_id)
    dm_preview = (cfg["dm_message"][:60] + "…") if len(cfg["dm_message"]) > 60 else (cfg["dm_message"] or "(সেট করা নেই)")
    link_mode = "📁 Folder link" if cfg["folder_link"] else (f"🔗 {len(cfg['extra_links'])}টা link" if cfg["extra_links"] else "(সেট করা নেই)")
    txt = (
        f"⚙️ <b>Special Setup</b>\n🔗 <code>{channel_id}</code>\n\n"
        f"💬 <b>DM Message:</b> {dm_preview}\n"
        f"🔗 <b>Linked channels:</b> {link_mode}\n"
    )
    buttons = [
        [{"text": "💬 DM Message Set/Edit", "callback_data": f"spdm_{channel_id}"}],
        [{"text": "📁 Folder Link Set", "callback_data": f"spfl_{channel_id}"}],
        [{"text": "🔗 Individual Links Set", "callback_data": f"spil_{channel_id}"}],
        [{"text": "⬅️ Back", "callback_data": "spmainch"}],
    ]
    await tg_post("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                        "text": txt, "parse_mode": "HTML",
                                        "reply_markup": {"inline_keyboard": buttons}})


async def show_special_group_detail(chat_id, message_id, row_id):
    g = await get_special_group(row_id)
    if not g:
        await show_special_group_list_global(chat_id, message_id)
        return
    dw = ", ".join(g["delete_words"]) if g["delete_words"] else "(নেই)"
    ww = ", ".join(g["warn_words"]) if g["warn_words"] else "(নেই)"
    bw = ", ".join(g["ban_words"]) if g["ban_words"] else "(নেই)"
    d_status = "✅ ON" if g["delete_enabled"] else "❌ OFF"
    w_status = "✅ ON" if g["warn_enabled"] else "❌ OFF"
    b_status = "✅ ON" if g["ban_enabled"] else "❌ OFF"
    txt = (
        f"👥 <b>Group Rules</b>\n🔗 <code>{g['group_id']}</code>" + (f" — {g['group_name']}" if g['group_name'] else "") + "\n\n"
        f"🗑️ <b>Delete</b> — {d_status}\n🔤 {dw}\n\n"
        f"⚠️ <b>Warn</b> — {w_status}\n🔤 {ww}\n\n"
        f"🚫 <b>Ban</b> — {b_status}\n🔤 {bw}\n"
    )
    d_toggle = "🗑️ Delete: ✅ ON" if g["delete_enabled"] else "🗑️ Delete: ❌ OFF"
    w_toggle = "⚠️ Warn: ✅ ON" if g["warn_enabled"] else "⚠️ Warn: ❌ OFF"
    b_toggle = "🚫 Ban: ✅ ON" if g["ban_enabled"] else "🚫 Ban: ❌ OFF"
    buttons = [
        [{"text": d_toggle, "callback_data": f"sptg_delete_{row_id}"},
         {"text": w_toggle, "callback_data": f"sptg_warn_{row_id}"},
         {"text": b_toggle, "callback_data": f"sptg_ban_{row_id}"}],
        [{"text": "📝 Delete Words", "callback_data": f"spwd_delete_{row_id}"},
         {"text": "📝 Warn Words", "callback_data": f"spwd_warn_{row_id}"},
         {"text": "📝 Ban Words", "callback_data": f"spwd_ban_{row_id}"}],
        [{"text": "❌ Group সরাও", "callback_data": f"spgdel_{row_id}"}],
        _nav_row(),
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

    if data == "spmainback":
        await show_special_main_menu(chat_id, edit_message_id=msg_id)
        return True

    if data == "spmainch":
        await show_special_channel_list(chat_id, edit_message_id=msg_id)
        return True

    if data == "spmaingr":
        await show_special_group_list_global(chat_id, msg_id)
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

    if data == "spgaddg":
        SPECIAL_INPUT_PENDING[uid] = {"field": "group_id_new"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": "🆔 নতুন Group এর ID/@username আর নামটা দাও:\n\n"
                    "<code>GroupID GroupName</code>\n\n"
                    "(bot ওই group এ admin থাকতে হবে)",
            "parse_mode": "HTML"})
        return True

    if data.startswith("spgview_"):
        row_id = data[len("spgview_"):]
        await show_special_group_detail(chat_id, msg_id, row_id)
        return True

    if data.startswith("spgdel_"):
        row_id = data[len("spgdel_"):]
        await delete_special_group(row_id)
        await show_special_group_list_global(chat_id, msg_id)
        return True

    if data.startswith("sptg_"):
        rest = data[len("sptg_"):]
        mode, row_id = rest.split("_", 1)
        g = await get_special_group(row_id)
        await update_special_group(row_id, **{f"{mode}_enabled": not g.get(f"{mode}_enabled")})
        await show_special_group_detail(chat_id, msg_id, row_id)
        return True

    if data.startswith("spwd_"):
        rest = data[len("spwd_"):]
        mode, row_id = rest.split("_", 1)
        SPECIAL_INPUT_PENDING[uid] = {"group_row_id": row_id, "field": f"group_words_{mode}"}
        await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
            "text": f"🚫 <b>{mode.capitalize()}</b> mode-এ যে শব্দগুলো ধরলে action নেওয়া হবে, কমা দিয়ে আলাদা করে পাঠাও:\n\n"
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

    field = pending.get("field")

    if field in ("dm_message", "folder_link", "extra_links"):
        channel_id = pending["channel_id"]
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
    elif field == "group_id_new":
        # Accept "GroupID GroupName" or "GroupID | GroupName" or just "GroupID"
        if "|" in text:
            gid, gname = [p.strip() for p in text.split("|", 1)]
        elif " " in text:
            gid, gname = text.split(" ", 1)
            gid, gname = gid.strip(), gname.strip()
        else:
            gid, gname = text.strip(), ""
        await add_special_group(gid, group_name=gname)
        label = f"{gid} ({gname})" if gname else gid
        await send_msg(chat_id, f"✅ Group যোগ হয়েছে: {label}")
    elif field.startswith("group_words_"):
        mode = field[len("group_words_"):]  # delete | warn | ban
        row_id = pending["group_row_id"]
        words = [w.strip() for w in text.split(",") if w.strip()]
        await update_special_group(row_id, **{f"{mode}_words": words})
        await send_msg(chat_id, f"✅ {mode.capitalize()} — {len(words)}টা শব্দ সেভ হয়েছে।")
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


async def _apply_moderation_action(group_id: str, msg: dict, action: str, reason_text: str):
    """Executes delete / warn / ban on a message. Ban = delete + banChatMember."""
    msg_id = msg.get("message_id")
    uid = msg.get("from", {}).get("id")
    uname = msg.get("from", {}).get("first_name", "User")
    try:
        if action == "delete":
            await tg_post("deleteMessage", {"chat_id": group_id, "message_id": msg_id})
        elif action == "warn":
            await tg_post("sendMessage", {
                "chat_id": group_id,
                "text": f"⚠️ {uname}, {reason_text}",
                "reply_to_message_id": msg_id,
            })
        elif action == "ban":
            await tg_post("deleteMessage", {"chat_id": group_id, "message_id": msg_id})
            if uid:
                await tg_post("banChatMember", {"chat_id": group_id, "user_id": uid})
            await tg_post("sendMessage", {
                "chat_id": group_id,
                "text": f"🚫 {uname} কে ব্যান করা হলো — {reason_text}",
            })
        return True
    except Exception as e:
        logger.warning(f"[Special] moderate action ({action}) failed: {e}")
        return False


def _msg_has_forward_or_link(msg: dict) -> bool:
    if msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_from_chat") or msg.get("forward_date"):
        return True
    text = msg.get("text") or msg.get("caption") or ""
    entities = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    for e in entities:
        if e.get("type") in ("url", "text_link", "mention"):
            return True
    if "t.me/" in text or "://" in text:
        return True
    return False


async def moderate_group_message(msg: dict) -> bool:
    """Call this from handle_message for every group/supergroup message.
    Checks the attached special_groups row for this group_id across all
    3 independent modes (Delete/Warn/Ban), each with its own word list
    and on/off toggle. Returns True if action was taken so the caller
    can skip further processing of this message."""
    chat = msg.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return False
    group_id = str(chat.get("id"))

    await _ensure_special_table()
    rows = await d1_select(
        "SELECT * FROM special_groups WHERE group_id=?1", [group_id]
    )
    if not rows:
        return False
    g = _row_to_group(rows[0])

    text = (msg.get("text") or msg.get("caption") or "")
    if not text:
        return False
    text_lower = text.lower()

    # Ban checked first (most severe), then Warn, then Delete.
    for mode, action in (("ban", "ban"), ("warn", "warn"), ("delete", "delete")):
        if not g.get(f"{mode}_enabled"):
            continue
        words = g.get(f"{mode}_words") or []
        if not words:
            continue
        if any(w.lower() in text_lower for w in words):
            return await _apply_moderation_action(
                group_id, msg, action, "এই শব্দ ব্যবহার নিষেধ এই গ্রুপে।"
            )

    return False
