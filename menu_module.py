# ============================================================
# Custom Nested Menu System (/menu) — QuizBot
# Box-icon (bottom persistent reply-keyboard) shows ONLY the item list,
# always available to all users — NO Add/Delete/Edit buttons inside it.
# Management (Add/Delete/Edit) is admin-only, via inline buttons under /menu.
#   /menu                    -> shows current list (box-icon + inline admin controls for admin)
#   item tap (box-icon)      -> drill-down navigation; CSV items trigger practice flow directly
#   "➕ Add more" (admin)     -> naya item er naam / CSV file pathao
#   "🗑 Delete" (admin)       -> item + tar shob sub-item delete (confirm shoho)
#   "✏️ Edit" (admin)         -> item rename
# Storage: D1 table menu_items (self-referencing parent_id)
# ============================================================
import json
import time
import csv as _csv_mod
from io import StringIO

from core import d1_run, d1_select, send_msg, tg_post, download_tg_file, db_is_owner_or_admin

_TABLE_READY = False

# uid -> parent_id (0 = root) jekhane "Add more" chaper por naya item add hobe
MENU_ADD_PENDING = {}
# uid -> {"item_id": int, "max": int} jekhane CSV shobe save hoyeche, ekhon count jiggesh kora hocche
MENU_COUNT_PENDING = {}
# uid -> {"item_id": int, "parent_id": int} — awaiting new name from inline "Edit"
MENU_EDIT_PENDING = {}
# uid -> current parent_id (0 = root) — tracks where user is navigating in box-icon
MENU_NAV_STATE = {}

BACK_LABEL = "🔙 Back"
MAIN_MENU_LABEL = "🏠 Main Menu"


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


async def _get_children_fresh(parent_id: int, expect_name: str = None) -> list:
    """Like _get_children but retries briefly if a just-inserted row (expect_name)
    isn't visible yet — works around D1 read-replica lag."""
    import asyncio
    for attempt in range(4):
        children = await _get_children(parent_id)
        if not expect_name or any(c["name"] == expect_name for c in children):
            return children
        await asyncio.sleep(0.3 * (attempt + 1))
    return children


async def _get_item(item_id: int) -> dict:
    await _ensure_table()
    rows = await d1_select("SELECT id, parent_id, name, csv_data FROM menu_items WHERE id = ?", [item_id])
    return rows[0] if rows else None


async def _get_item_by_name(parent_id: int, name: str) -> dict:
    await _ensure_table()
    rows = await d1_select(
        "SELECT id, parent_id, name, csv_data FROM menu_items WHERE parent_id = ? AND name = ? LIMIT 1",
        [parent_id, name],
    )
    return rows[0] if rows else None


async def _delete_item_recursive(item_id: int):
    """Delete item + all nested children/grandchildren."""
    children = await _get_children(item_id)
    for ch in children:
        await _delete_item_recursive(ch["id"])
    await d1_run("DELETE FROM menu_items WHERE id = ?", [item_id])


async def _rename_item(item_id: int, new_name: str):
    await _ensure_table()
    await d1_run("UPDATE menu_items SET name = ? WHERE id = ?", [new_name, item_id])


async def _build_reply_keyboard(parent_id: int = 0, expect_name: str = None) -> dict:
    """Bottom keyboard (box-icon area) — items at the current level, drill-down.
    Back + Main Menu appear together in the same row when nested."""
    children = await _get_children_fresh(parent_id, expect_name) if expect_name else await _get_children(parent_id)
    names = [ch["name"] for ch in children]
    if not names:
        rows = [[{"text": "📋 Menu খালি — Admin /menu দিয়ে যোগ করবে"}]]
    else:
        rows = [[{"text": n} for n in names[i:i + 2]] for i in range(0, len(names), 2)]
    if parent_id:
        rows.append([{"text": BACK_LABEL}, {"text": MAIN_MENU_LABEL}])
    return {"keyboard": rows, "resize_keyboard": True}


async def _render_listing(parent_id: int = 0) -> tuple:
    """Returns (text, reply_markup) for a menu level (root or a specific item) — inline admin view."""
    children = await _get_children(parent_id)
    flat = [{"text": f"📁 {ch['name']}", "callback_data": f"mnuopen_{ch['id']}"} for ch in children]
    rows = [flat[i:i + 3] for i in range(0, len(flat), 3)]
    action_row = [{"text": "➕ Add more", "callback_data": f"mnuadd_{parent_id}"}]
    if children:
        action_row.append({"text": "🗑 Delete", "callback_data": f"mnudelpick_{parent_id}"})
        action_row.append({"text": "✏️ Edit", "callback_data": f"mnueditpick_{parent_id}"})
    rows.append(action_row)
    if parent_id:
        item = await _get_item(parent_id)
        back_target = f"mnuopen_{item['parent_id']}" if item and item["parent_id"] else "mnuroot"
        rows.append([{"text": "🔙 Back", "callback_data": back_target}])
        title = f"📁 <b>{item['name']}</b>" if item else "📋 <b>Menu</b>"
    else:
        title = "📋 <b>Main Menu</b>"
    return title, {"inline_keyboard": rows}


async def cmd_menu(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if not await db_is_owner_or_admin(uid):
        await send_msg(chat_id, "❌ এই কমান্ড শুধু Admin-এর জন্য।")
        return
    MENU_NAV_STATE[uid] = 0
    kb_reply = await _build_reply_keyboard(0)
    await send_msg(chat_id, "📋 Menu (box-icon)", reply_markup=kb_reply)
    title, kb_inline = await _render_listing(0)
    await send_msg(chat_id, title, reply_markup=kb_inline)


async def handle_menu_reply_keyboard(msg: dict) -> bool:
    """Handles taps on the persistent bottom keyboard items — works for ALL users, drill-down nested. Returns True if consumed."""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    current_parent = MENU_NAV_STATE.get(uid, 0)

    if text == BACK_LABEL:
        item = await _get_item(current_parent) if current_parent else None
        new_parent = item["parent_id"] if item else 0
        MENU_NAV_STATE[uid] = new_parent
        kb = await _build_reply_keyboard(new_parent)
        await send_msg(chat_id, "📋 Menu", reply_markup=kb)
        return True

    if text == MAIN_MENU_LABEL:
        MENU_NAV_STATE[uid] = 0
        kb = await _build_reply_keyboard(0)
        await send_msg(chat_id, "📋 Main Menu", reply_markup=kb)
        return True

    match = await _get_item_by_name(current_parent, text)
    if not match:
        return False

    if match.get("csv_data"):
        mcqs = json.loads(match["csv_data"])
        MENU_COUNT_PENDING[uid] = {"item_id": match["id"], "max": len(mcqs)}
        await send_msg(chat_id,
            f"📁 <b>{match['name']}</b> — {len(mcqs)} টি MCQ সংরক্ষিত আছে।\n\n"
            "কয়টি MCQ practice করতে চান, সংখ্যা লিখে পাঠান:")
        return True

    children = await _get_children(match["id"])
    if children:
        # jodi ei level-e CSV-item thake, taar naam box-icon-e na dekhiye direct output dao
        csv_child = next((c for c in children if c.get("csv_data")), None)
        if csv_child:
            mcqs = json.loads(csv_child["csv_data"])
            MENU_COUNT_PENDING[uid] = {"item_id": csv_child["id"], "max": len(mcqs)}
            await send_msg(chat_id,
                f"📁 <b>{match['name']}</b> — {len(mcqs)} টি MCQ সংরক্ষিত আছে।\n\n"
                "কয়টি MCQ practice করতে চান, সংখ্যা লিখে পাঠান:")
            return True
        MENU_NAV_STATE[uid] = match["id"]
        kb = await _build_reply_keyboard(match["id"])
        await send_msg(chat_id, f"📁 <b>{match['name']}</b>", reply_markup=kb)
        return True

    await send_msg(chat_id, f"📁 <b>{match['name']}</b> — এখনো কিছু যোগ করা হয়নি।")
    return True


async def _safe_edit(chat_id, msg_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return await tg_post("editMessageText", data)


async def handle_menu_callback(query: dict) -> bool:
    """Returns True if handled."""
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    msg_id = query["message"]["message_id"]
    uid = query["from"]["id"]

    MANAGEMENT_PREFIXES = ("mnuadd_", "mnudelpick_", "mnudelask_", "mnudelyes_", "mnueditpick_", "mnueditask_")
    if data.startswith(MANAGEMENT_PREFIXES) or data == "mnuroot":
        if not await db_is_owner_or_admin(uid):
            await tg_post("answerCallbackQuery", {
                "callback_query_id": query["id"],
                "text": "❌ শুধু Admin ব্যবহার করতে পারবে।", "show_alert": True,
            })
            return True

    if data.startswith("mnuadd_"):
        parent_id = int(data[len("mnuadd_"):])
        MENU_ADD_PENDING[uid] = parent_id
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _safe_edit(chat_id, msg_id, "✏️ নতুন item-এর নাম লিখে পাঠাও।\n📎 অথবা CSV ফাইল পাঠাও (MCQ practice item হিসেবে)।")
        return True

    if data.startswith("mnudelpick_"):
        parent_id = int(data[len("mnudelpick_"):])
        children = await _get_children(parent_id)
        if not children:
            await tg_post("answerCallbackQuery", {"callback_query_id": query["id"], "text": "❌ Delete করার মতো কিছু নেই।", "show_alert": True})
            return True
        flat = [{"text": f"🗑 {ch['name']}", "callback_data": f"mnudelask_{ch['id']}_{parent_id}"} for ch in children]
        rows = [flat[i:i + 2] for i in range(0, len(flat), 2)]
        back_target = f"mnuopen_{parent_id}" if parent_id else "mnuroot"
        rows.append([{"text": "🔙 Back", "callback_data": back_target}])
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _safe_edit(chat_id, msg_id, "🗑 কোনটা Delete করবে?", reply_markup={"inline_keyboard": rows})
        return True

    if data.startswith("mnudelask_"):
        rest = data[len("mnudelask_"):]
        item_id_s, parent_id_s = rest.split("_")
        item_id, parent_id = int(item_id_s), int(parent_id_s)
        item = await _get_item(item_id)
        if not item:
            await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
            return True
        back_target = f"mnuopen_{parent_id}" if parent_id else "mnuroot"
        kb = {"inline_keyboard": [[
            {"text": "✅ হ্যাঁ, Delete করো", "callback_data": f"mnudelyes_{item_id}_{parent_id}"},
            {"text": "❌ না", "callback_data": back_target},
        ]]}
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _safe_edit(chat_id, msg_id, f"🗑 <b>{item['name']}</b> এবং এর ভেতরের সব কিছু delete করবে?", reply_markup=kb)
        return True

    if data.startswith("mnudelyes_"):
        rest = data[len("mnudelyes_"):]
        item_id_s, parent_id_s = rest.split("_")
        item_id, parent_id = int(item_id_s), int(parent_id_s)
        await _delete_item_recursive(item_id)
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"], "text": "✅ Delete হয়েছে"})
        import asyncio as _asyncio
        children = await _get_children(parent_id)
        for _attempt in range(4):
            if not any(c["id"] == item_id for c in children):
                break
            await _asyncio.sleep(0.3 * (_attempt + 1))
            children = await _get_children(parent_id)
        title, kb = await _render_listing(parent_id)
        await _safe_edit(chat_id, msg_id, title, reply_markup=kb)
        if parent_id == 0:
            await send_msg(chat_id, "📋 Menu (box-icon)", reply_markup=await _build_reply_keyboard(0))
        return True

    if data.startswith("mnueditpick_"):
        parent_id = int(data[len("mnueditpick_"):])
        children = await _get_children(parent_id)
        if not children:
            await tg_post("answerCallbackQuery", {"callback_query_id": query["id"], "text": "❌ Edit করার মতো কিছু নেই।", "show_alert": True})
            return True
        flat = [{"text": f"✏️ {ch['name']}", "callback_data": f"mnueditask_{ch['id']}_{parent_id}"} for ch in children]
        rows = [flat[i:i + 2] for i in range(0, len(flat), 2)]
        back_target = f"mnuopen_{parent_id}" if parent_id else "mnuroot"
        rows.append([{"text": "🔙 Back", "callback_data": back_target}])
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _safe_edit(chat_id, msg_id, "✏️ কোনটা Edit করবে?", reply_markup={"inline_keyboard": rows})
        return True

    if data.startswith("mnueditask_"):
        rest = data[len("mnueditask_"):]
        item_id_s, parent_id_s = rest.split("_")
        item_id, parent_id = int(item_id_s), int(parent_id_s)
        MENU_EDIT_PENDING[uid] = {"item_id": item_id, "parent_id": parent_id}
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _safe_edit(chat_id, msg_id, "✏️ নতুন নাম লিখে পাঠাও।")
        return True

    if data == "mnuroot":
        title, kb = await _render_listing(0)
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        await _safe_edit(chat_id, msg_id, title, reply_markup=kb)
        return True

    if data.startswith("mnuopen_"):
        item_id = int(data[len("mnuopen_"):])
        item = await _get_item(item_id)
        await tg_post("answerCallbackQuery", {"callback_query_id": query["id"]})
        if not item:
            return True
        if item.get("csv_data"):
            mcqs = json.loads(item["csv_data"])
            MENU_COUNT_PENDING[uid] = {"item_id": item_id, "max": len(mcqs)}
            await _safe_edit(chat_id, msg_id,
                f"📁 <b>{item['name']}</b> — {len(mcqs)} টি MCQ সংরক্ষিত আছে।\n\n"
                "কয়টি MCQ practice করতে চান, সংখ্যা লিখে পাঠান:")
            return True
        if await db_is_owner_or_admin(uid):
            title, kb = await _render_listing(item_id)
            await _safe_edit(chat_id, msg_id, title, reply_markup=kb)
        else:
            children = await _get_children(item_id)
            if not children:
                await _safe_edit(chat_id, msg_id, f"📁 <b>{item['name']}</b> — এখনো কিছু যোগ করা হয়নি।")
            else:
                flat = [{"text": f"📁 {ch['name']}", "callback_data": f"mnuopen_{ch['id']}"} for ch in children]
                rows = [flat[i:i + 3] for i in range(0, len(flat), 3)]
                back_target = f"mnuopen_{item['parent_id']}" if item["parent_id"] else "mnuroot"
                rows.append([{"text": "🔙 Back", "callback_data": back_target}])
                await _safe_edit(chat_id, msg_id, f"📁 <b>{item['name']}</b>", reply_markup={"inline_keyboard": rows})
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
    """Returns True if consumed (uid was awaiting a menu-add name/csv / edit-name / CSV count)."""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]

    if uid in MENU_ADD_PENDING:
        parent_id = MENU_ADD_PENDING[uid]

        if msg.get("document"):
            fname = msg["document"].get("file_name", "")
            if not fname.lower().endswith(".csv"):
                return False
            MENU_ADD_PENDING.pop(uid)
            await send_msg(chat_id, "⏳ CSV পড়া হচ্ছে...")
            try:
                csv_bytes = await download_tg_file(msg["document"]["file_id"])
                try:
                    reader = _csv_mod.DictReader(StringIO(csv_bytes.decode("utf-8-sig")))
                    mcqs = []
                    for raw_row in reader:
                        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
                        q = row.get("question") or row.get("questions") or ""
                        if not q:
                            continue
                        opts = [row.get(f"option{i}") or "" for i in range(1, 5)]
                        opts = [o for o in opts if o]
                        ans_raw = (row.get("answer") or "A").strip().upper()
                        expl = row.get("explanation") or ""
                        mcqs.append({"question": q, "options": opts, "answer": ans_raw, "explanation": expl})
                except Exception as e:
                    await send_msg(chat_id, f"❌ CSV পড়তে সমস্যা হয়েছে: {e}")
                    return True
                if not mcqs:
                    try:
                        headers = reader.fieldnames
                    except Exception:
                        headers = None
                    await send_msg(chat_id,
                        f"❌ CSV-তে কোনো valid MCQ পাওয়া যায়নি।\n"
                        f"প্রয়োজনীয় কলাম: question(s), option1-4, answer, explanation\n"
                        f"পাওয়া গেছে: {headers}")
                    return True
                name = fname.rsplit(".", 1)[0]
                await _add_item(parent_id, name, uid, csv_data=json.dumps(mcqs))
                await send_msg(chat_id, f"✅ যোগ হয়েছে: <b>{name}</b> ({len(mcqs)} টি MCQ)")
                title, kb = await _render_listing(parent_id)
                await send_msg(chat_id, title, reply_markup=kb)
                if parent_id == 0:
                    await send_msg(chat_id, "📋 Menu (box-icon)", reply_markup=await _build_reply_keyboard(0, expect_name=name))
            except Exception as e:
                await send_msg(chat_id, f"❌ Error: {e}")
            return True

        text = (msg.get("text") or "").strip()
        if not text or text.startswith("/"):
            return False
        MENU_ADD_PENDING.pop(uid)
        await _add_item(parent_id, text, uid)
        await send_msg(chat_id, f"✅ যোগ হয়েছে: <b>{text}</b>")
        title, kb = await _render_listing(parent_id)
        await send_msg(chat_id, title, reply_markup=kb)
        if parent_id == 0:
            await send_msg(chat_id, "📋 Menu (box-icon)", reply_markup=await _build_reply_keyboard(0, expect_name=text))
        return True

    if uid in MENU_EDIT_PENDING:
        text = (msg.get("text") or "").strip()
        if not text or text.startswith("/"):
            return False
        info = MENU_EDIT_PENDING.pop(uid)
        item_id, parent_id = info["item_id"], info["parent_id"]
        await _rename_item(item_id, text)
        await send_msg(chat_id, f"✅ নতুন নাম সেভ হয়েছে: <b>{text}</b>")
        title, kb = await _render_listing(parent_id)
        await send_msg(chat_id, title, reply_markup=kb)
        if parent_id == 0:
            await send_msg(chat_id, "📋 Menu (box-icon)", reply_markup=await _build_reply_keyboard(0))
        return True

    if uid in MENU_COUNT_PENDING:
        text = (msg.get("text") or "").strip()
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
            await send_msg(chat_id, f"🎯 <b>Quiz তৈরি হয়েছে!</b>\n\n📝 {name} — {len(mcqs)} প্রশ্ন\n\n🔗 <code>{link}</code>")
        else:
            from core import GH_PAGES_EXAM_URL
            web_link = f"{GH_PAGES_EXAM_URL}?id={quiz_id}"
            await send_msg(chat_id, f"🌐 <b>Website Exam তৈরি হয়েছে!</b>\n\n📝 {name} — {len(mcqs)} প্রশ্ন\n\n🔗 {web_link}")
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
