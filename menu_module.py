# ============================================================
# Custom Nested Menu System (/menu)
# - /menu <name>            -> naya main menu item add hobe
# - /menu                   -> shob main menu item list, each row e "➕ Add more"
# - item tap                -> ওই item er under-e thaka sub-items dekhabe (+ Add more + Back)
# - "➕ Add more" tap        -> naya sub-item er naam type korte bola hobe (unlimited nested)
# Storage: D1 table menu_items (self-referencing parent_id)
# ============================================================
import json
from core import d1_run, d1_select, send_msg, tg_post

_TABLE_READY = False

# uid -> parent_id (0 = root) jekhane "Add more" chaper por naya item add hobe
MENU_ADD_PENDING = {}


async def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    await d1_run(
        "CREATE TABLE IF NOT EXISTS menu_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "parent_id INTEGER NOT NULL DEFAULT 0, "
        "name TEXT NOT NULL, "
        "created_by INTEGER, "
        "created_at INTEGER)"
    )
    _TABLE_READY = True


async def _add_item(parent_id: int, name: str, uid: int) -> int:
    await _ensure_table()
    import time
    res = await d1_run(
        "INSERT INTO menu_items (parent_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
        [parent_id, name, uid, int(time.time())],
        return_id=True,
    )
    return res


async def _get_children(parent_id: int) -> list:
    await _ensure_table()
    rows = await d1_select(
        "SELECT id, name FROM menu_items WHERE parent_id = ? ORDER BY id ASC",
        [parent_id],
    )
    return rows or []


async def _get_item(item_id: int) -> dict:
    await _ensure_table()
    rows = await d1_select("SELECT id, parent_id, name FROM menu_items WHERE id = ?", [item_id])
    return rows[0] if rows else None


def _build_keyboard(children: list, parent_id: int, is_root: bool) -> dict:
    buttons = []
    for ch in children:
        buttons.append([
            {"text": f"📁 {ch['name']}", "callback_data": f"mnuopen_{ch['id']}"},
            {"text": "➕ Add more", "callback_data": f"mnuadd_{ch['id']}"},
        ])
    buttons.append([{"text": "➕ Add more (এখানে নতুন যোগ করো)", "callback_data": f"mnuadd_{parent_id}"}])
    if not is_root:
        # parent_id (current node) er nijer parent e back
        pass
    return {"inline_keyboard": buttons}


async def cmd_menu(msg: dict):
    text = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]

    name = text[len("/menu"):].strip()

    if name:
        # /menu <name> -> root e (parent_id=0) notun item add
        await _add_item(0, name, uid)
        await send_msg(chat_id, f"✅ Menu-তে যোগ হয়েছে: <b>{name}</b>")
        return

    # /menu (khali) -> root list dekhabe
    children = await _get_children(0)
    if not children:
        kb = {"inline_keyboard": [[{"text": "➕ Add more", "callback_data": "mnuadd_0"}]]}
        await send_msg(chat_id, "📋 <b>Menu খালি!</b>\n\nনিচের বাটনে চাপ দিয়ে প্রথম item যোগ করো।", reply_markup=kb)
        return

    kb = _build_keyboard(children, 0, is_root=True)
    await send_msg(chat_id, "📋 <b>Main Menu</b>", reply_markup=kb)


async def handle_menu_callback(query: dict) -> bool:
    """Returns True if handled."""
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    msg_id = query["message"]["message_id"]
    uid = query["from"]["id"]

    if data.startswith("mnuopen_"):
        item_id = int(data[len("mnuopen_"):])
        item = await _get_item(item_id)
        if not item:
            await tg_post("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "❌ Item পাওয়া যায়নি।"})
            return True
        children = await _get_children(item_id)
        buttons = []
        for ch in children:
            buttons.append([
                {"text": f"📁 {ch['name']}", "callback_data": f"mnuopen_{ch['id']}"},
                {"text": "➕ Add more", "callback_data": f"mnuadd_{ch['id']}"},
            ])
        buttons.append([{"text": "➕ Add more (এখানে)", "callback_data": f"mnuadd_{item_id}"}])
        nav_row = [{"text": "🔙 Back", "callback_data": f"mnuopen_{item['parent_id']}" if item["parent_id"] else "mnuroot"}]
        buttons.append(nav_row)
        kb = {"inline_keyboard": buttons}
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": f"📁 <b>{item['name']}</b>",
            "parse_mode": "HTML", "reply_markup": kb,
        })
        return True

    if data == "mnuroot":
        children = await _get_children(0)
        kb = _build_keyboard(children, 0, is_root=True)
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "📋 <b>Main Menu</b>", "parse_mode": "HTML", "reply_markup": kb,
        })
        return True

    if data.startswith("mnuadd_"):
        parent_id = int(data[len("mnuadd_"):])
        MENU_ADD_PENDING[uid] = parent_id
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "✏️ নতুন item-এর নাম লিখে পাঠাও:",
        })
        return True

    return False


async def handle_menu_pending_text(msg: dict) -> bool:
    """Returns True if consumed (uid was awaiting a menu-add name)."""
    uid = msg["from"]["id"]
    if uid not in MENU_ADD_PENDING:
        return False
    text = msg.get("text", "").strip()
    if not text or text.startswith("/"):
        return False
    parent_id = MENU_ADD_PENDING.pop(uid)
    chat_id = msg["chat"]["id"]
    await _add_item(parent_id, text, uid)
    await send_msg(chat_id, f"✅ যোগ হয়েছে: <b>{text}</b>")
    children = await _get_children(parent_id)
    buttons = []
    for ch in children:
        buttons.append([
            {"text": f"📁 {ch['name']}", "callback_data": f"mnuopen_{ch['id']}"},
            {"text": "➕ Add more", "callback_data": f"mnuadd_{ch['id']}"},
        ])
    buttons.append([{"text": "➕ Add more (এখানে)", "callback_data": f"mnuadd_{parent_id}"}])
    if parent_id:
        item = await _get_item(parent_id)
        buttons.append([{"text": "🔙 Back", "callback_data": f"mnuopen_{item['parent_id']}" if item and item["parent_id"] else "mnuroot"}])
        title = f"📁 {item['name']}" if item else "📋 Menu"
    else:
        title = "📋 Main Menu"
    kb = {"inline_keyboard": buttons}
    await send_msg(chat_id, title, reply_markup=kb)
    return True
