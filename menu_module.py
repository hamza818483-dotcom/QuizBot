# ============================================================
# Custom Nested Menu System (/menu)
# - /menu <name>            -> naya main menu item add hobe
# - /menu                   -> shob main menu item list, each row e "➕ Add more"
# - item tap                -> ওই item er under-e thaka sub-items dekhabe (+ Add more + Back)
# - "➕ Add more" tap        -> naya sub-item er naam type korte bola hobe (unlimited nested)
# Storage: D1 table menu_items (self-referencing parent_id)
# ============================================================
# ============================================================
# Custom Nested Menu System (/menu)
# - /menu <name>            -> naya main menu item add hobe
# - /menu                   -> shob main menu item list, each row e Open/Add/Delete
# - item tap                -> ওই item er under-e thaka sub-items dekhabe (+ Add + Delete + Back)
# - "➕ Add more" tap        -> naya sub-item er naam type korte bola hobe (unlimited nested)
#   -- CSV file pathle       -> সেই item-এ CSV internally save hoye thakbe, taarpor koyta MCQ
#                              practice korte chan seta jiggesh korbe. Count dile Quiz/Poll/
#                              Website Exam banaye inline button hisebe dibe.
# - "🗑 Delete" tap          -> ওই item + tar shob sub-item delete (confirm shoho)
# Storage: D1 table menu_items (self-referencing parent_id) + menu_csv (csv per item)
# ============================================================
import json
import time
from core import d1_run, d1_select, send_msg, tg_post, download_tg_file

_TABLE_READY = False

# uid -> parent_id (0 = root) jekhane "Add more" chaper por naya item add hobe
MENU_ADD_PENDING = {}
# uid -> {"item_id": int} jekhane CSV shobe save hoyeche, ekhon count jiggesh kora hocche
MENU_COUNT_PENDING = {}


async def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    await d1_run(
        "CREATE TABLE IF NOT EXISTS menu_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "parent_id INTEGER NOT NULL DEFAULT 0, "
        "name TEXT NOT NULL, "
        "csv_data TEXT, "
        "created_by INTEGER, "
        "created_at INTEGER)"
    )
    _TABLE_READY = True


async def _add_item(parent_id: int, name: str, uid: int, csv_data: str = None) -> int:
    await _ensure_table()
    res = await d1_run(
        "INSERT INTO menu_items (parent_id, name, csv_data, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        [parent_id, name, csv_data, uid, int(time.time())],
        return_id=True,
    )
    return res


async def _get_children(parent_id: int) -> list:
    await _ensure_table()
    rows = await d1_select(
        "SELECT id, name, csv_data FROM menu_items WHERE parent_id = ? ORDER BY id ASC",
        [parent_id],
    )
    return rows or []


async def _get_item(item_id: int) -> dict:
    await _ensure_table()
    rows = await d1_select("SELECT id, parent_id, name, csv_data FROM menu_items WHERE id = ?", [item_id])
    return rows[0] if rows else None


async def _delete_item_recursive(item_id: int):
    """Delete item + all nested children/grandchildren."""
    children = await _get_children(item_id)
    for ch in children:
        await _delete_item_recursive(ch["id"])
    await d1_run("DELETE FROM menu_items WHERE id = ?", [item_id])


def _item_row_buttons(item_id: int, name: str) -> list:
    return [{"text": f"📁 {name}", "callback_data": f"mnuopen_{item_id}"}]


async def _render_listing(parent_id: int) -> tuple:
    """Returns (text, reply_markup) for a menu level (root or a specific item)."""
    if parent_id:
        item = await _get_item(parent_id)
        title = f"📁 <b>{item['name']}</b>" if item else "📋 <b>Menu</b>"
        back_target = f"mnuopen_{item['parent_id']}" if (item and item["parent_id"]) else "mnuroot"
    else:
        title = "📋 <b>Main Menu</b>"
        back_target = None

    children = await _get_children(parent_id)
    flat = [{"text": f"📁 {ch['name']}", "callback_data": f"mnuopen_{ch['id']}"} for ch in children]
    buttons = [flat[i:i + 3] for i in range(0, len(flat), 3)]
    action_row = [{"text": "➕ Add", "callback_data": f"mnuadd_{parent_id}"}]
    if children:
        action_row.append({"text": "🗑 Delete", "callback_data": f"mnudelpick_{parent_id}"})
    buttons.append(action_row)
    if back_target:
        buttons.append([{"text": "🔙 Back", "callback_data": back_target}])
    return title, {"inline_keyboard": buttons}


async def cmd_menu(msg: dict):
    text = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]

    name = text[len("/menu"):].strip()

    if name:
        await _add_item(0, name, uid)
        await send_msg(chat_id, f"✅ Menu-তে যোগ হয়েছে: <b>{name}</b>")
        return

    title, kb = await _render_listing(0)
    await send_msg(chat_id, title, reply_markup=kb)


async def handle_menu_callback(query: dict) -> bool:
    """Returns True if handled."""
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    msg_id = query["message"]["message_id"]
    uid = query["from"]["id"]

    if data.startswith("mnuopen_") or data == "mnuroot":
        parent_id = 0 if data == "mnuroot" else int(data[len("mnuopen_"):])
        title, kb = await _render_listing(parent_id)
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": title, "parse_mode": "HTML", "reply_markup": kb,
        })
        return True

    if data.startswith("mnuadd_"):
        parent_id = int(data[len("mnuadd_"):])
        MENU_ADD_PENDING[uid] = parent_id
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "✏️ নতুন item-এর নাম লিখে পাঠাও।\n📎 অথবা CSV ফাইল পাঠাও (নাম হিসেবে ফাইলের নাম ব্যবহার হবে)।",
        })
        return True

    if data.startswith("mnudelpick_"):
        parent_id = int(data[len("mnudelpick_"):])
        children = await _get_children(parent_id)
        if not children:
            return True
        flat = [{"text": f"🗑 {ch['name']}", "callback_data": f"mnudelask_{ch['id']}"} for ch in children]
        buttons = [flat[i:i + 2] for i in range(0, len(flat), 2)]
        back_target = f"mnuopen_{parent_id}" if parent_id else "mnuroot"
        buttons.append([{"text": "🔙 Back", "callback_data": back_target}])
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "🗑 কোনটা Delete করবে?", "reply_markup": {"inline_keyboard": buttons},
        })
        return True

    if data.startswith("mnudelask_"):
        item_id = int(data[len("mnudelask_"):])
        item = await _get_item(item_id)
        if not item:
            return True
        kb = {"inline_keyboard": [[
            {"text": "✅ হ্যাঁ, Delete করো", "callback_data": f"mnudelyes_{item_id}"},
            {"text": "❌ না", "callback_data": f"mnuopen_{item['parent_id']}" if item["parent_id"] else "mnuroot"},
        ]]}
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": f"🗑 <b>{item['name']}</b> এবং এর ভেতরের সব কিছু delete করবে?",
            "parse_mode": "HTML", "reply_markup": kb,
        })
        return True

    if data.startswith("mnudelyes_"):
        item_id = int(data[len("mnudelyes_"):])
        item = await _get_item(item_id)
        parent_id = item["parent_id"] if item else 0
        await _delete_item_recursive(item_id)
        title, kb = await _render_listing(parent_id)
        await tg_post("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": f"✅ Delete হয়েছে।\n\n{title}", "parse_mode": "HTML", "reply_markup": kb,
        })
        return True

    if data.startswith("mnucnt_"):
        # mnucnt_{item_id}_{count}_{mode}  -> generate quiz/poll/exam
        parts = data.split("_")
        item_id = int(parts[1])
        count = int(parts[2])
        mode = parts[3]
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _generate_from_item(chat_id, uid, item_id, count, mode, msg_id)
        return True

    return False


async def handle_menu_pending_text(msg: dict) -> bool:
    """Returns True if consumed (uid was awaiting a menu-add name)."""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]

    if uid in MENU_ADD_PENDING:
        # CSV file pathano hole seta directly item hisebe save hobe
        if msg.get("document"):
            fname = msg["document"].get("file_name", "")
            if fname.lower().endswith(".csv"):
                parent_id = MENU_ADD_PENDING.pop(uid)
                loading = await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
                try:
                    csv_bytes = await download_tg_file(msg["document"]["file_id"])
                    from app import _parse_csv_bytes
                    mcqs = _parse_csv_bytes(csv_bytes)
                    if not mcqs:
                        await send_msg(chat_id, "❌ CSV-তে কোনো MCQ পাওয়া যায়নি!")
                        return True
                    name = fname.rsplit(".", 1)[0]
                    item_id = await _add_item(parent_id, name, uid, csv_data=json.dumps(mcqs))
                    await send_msg(
                        chat_id,
                        f"✅ <b>{name}</b> যোগ হয়েছে ({len(mcqs)} টি MCQ সংরক্ষিত আছে)।\n\n"
                        "কয়টি MCQ practice করতে চান, সংখ্যা লিখে পাঠান:"
                    )
                    MENU_COUNT_PENDING[uid] = {"item_id": item_id, "max": len(mcqs)}
                except Exception as e:
                    await send_msg(chat_id, f"❌ Error: {e}")
                return True
            return False

        text = msg.get("text", "").strip()
        if not text or text.startswith("/"):
            return False
        parent_id = MENU_ADD_PENDING.pop(uid)
        await _add_item(parent_id, text, uid)
        await send_msg(chat_id, f"✅ যোগ হয়েছে: <b>{text}</b>")
        title, kb = await _render_listing(parent_id)
        await send_msg(chat_id, title, reply_markup=kb)
        return True

    if uid in MENU_COUNT_PENDING:
        text = msg.get("text", "").strip()
        if not text or text.startswith("/"):
            return False
        if not text.isdigit():
            await send_msg(chat_id, "❌ শুধু সংখ্যা পাঠাও, যেমন: 20")
            return True
        count = int(text)
        info = MENU_COUNT_PENDING.pop(uid)
        item_id = info["item_id"]
        max_n = info["max"]
        count = max(1, min(count, max_n))
        kb = {"inline_keyboard": [
            [{"text": "🎯 Quiz বানাও (bot link)", "callback_data": f"mnucnt_{item_id}_{count}_quiz"}],
            [{"text": "📊 Poll পাঠাও (এই চ্যাটে)", "callback_data": f"mnucnt_{item_id}_{count}_poll"}],
            [{"text": "🌐 Website Exam বানাও", "callback_data": f"mnucnt_{item_id}_{count}_exam"}],
        ]}
        await send_msg(chat_id, f"✅ {count} টি MCQ নেওয়া হবে। কোন ফরম্যাটে চান?", reply_markup=kb)
        return True

    return False


async def _generate_from_item(chat_id: int, uid: int, item_id: int, count: int, mode: str, msg_id: int = None):
    item = await _get_item(item_id)
    if not item or not item.get("csv_data"):
        await send_msg(chat_id, "❌ এই item-এ কোনো CSV পাওয়া যায়নি।")
        return
    mcqs = json.loads(item["csv_data"])
    mcqs = mcqs[:count]
    name = item["name"]

    if mode == "quiz" or mode == "exam":
        from quiz import create_quiz_from_mcqs
        quiz_id = await create_quiz_from_mcqs(mcqs, name, uid)
        bot_info = await tg_post("getMe", {})
        bot_username = bot_info.get("result", {}).get("username", "atlasQuizProBot")
        if mode == "quiz":
            link = f"https://t.me/{bot_username}?start={quiz_id}"
            await send_msg(chat_id, f"🎯 <b>Quiz তৈরি হয়েছে!</b>\n\n📝 {name} — {len(mcqs)} প্রশ্ন\n\n🔗 <code>{link}</code>", parse_mode="HTML")
        else:
            web_link = f"https://hamza818483-dotcom.github.io/QuizBot/exam.html?id={quiz_id}"
            await send_msg(chat_id, f"🌐 <b>Website Exam তৈরি হয়েছে!</b>\n\n📝 {name} — {len(mcqs)} প্রশ্ন\n\n🔗 {web_link}", parse_mode="HTML")
        return

    if mode == "poll":
        from core import send_poll
        ans_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        sent = 0
        for m in mcqs:
            try:
                await send_poll(
                    chat_id, m.get("question", ""), m.get("options", []),
                    ans_map.get(m.get("answer", "A"), 0),
                    explanation=m.get("explanation", ""),
                )
                sent += 1
            except Exception:
                continue
        await send_msg(chat_id, f"✅ {sent} টি poll পাঠানো হয়েছে ({name})।")
        return
