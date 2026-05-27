#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - CSV to Poll Handler (/csv, /csvS, /csvI, /csvIS) - FULLY FIXED"""
from global_state import GLOBAL_PAUSE
import asyncio, json, csv, io, re, time, hashlib
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, Config
from services import parse_csv_to_mcqs

def get_pre_message(topic: str, count: int) -> str:
    topic_text = f'"{topic}"' if topic else ""
    return f"""🌟Important Poll Solve By ATLAS
🔥Topic Name: {topic_text}

✅প্রশ্ন সংখ্যা: {count}"""

def get_ending_message(topic: str, count: int, first_link: str = "") -> str:
    topic_text = f'"{topic}"' if topic else ""
    if first_link:
        return f"""🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!
👉এটলাস আয়োজিত {topic_text} পোল সলভে অংশগ্রহণ করার জন্য। 😊

📊 মোট পোল: {count}

⁉️তোমার স্কোর কত? 🤔
( ? / {count} )

নিচে লিখো! 👇

✅পোল যেখান থেকে শুরু হয়েছে:
{first_link}"""
    else:
        return f"""🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!
👉এটলাস আয়োজিত {topic_text} পোল সলভে অংশগ্রহণ করার জন্য। 😊

📊 মোট পোল: {count}

⁉️তোমার স্কোর কত? 🤔
( ? / {count} )

নিচে লিখো! 👇"""

def get_master_summary(topic: str, total: int, total_batches: int, batch_links: list) -> str:
    text = f"""🟥Poll Topic: "{topic}"
🌟মোট প্রশ্ন: {total}
📦 মোট ব্যাচ: {total_batches}

"""
    for part_n, link, count in batch_links:
        text += f"📍Part-{part_n:02d}: ({count}টি প্রশ্ন)\n{link}\n\n"
    text += """📌 *এটলাসের Exam Batch* এ অসংখ্য প্রশ্ন প্রাক্টিসের সুযোগ আছে।
💬 *Whatsapp:* wa.me/8801999681290
🌟 *Website:* Atlascourses.com"""
    return text

async def get_explanation(mcq: dict) -> str:
    exp_row = await db.fetchone('SELECT mode, custom_text, tag_name FROM exp_settings WHERE id = 1')
    mode = exp_row[0] if exp_row else 'auto'
    custom_text = exp_row[1] if exp_row else ''
    tag_name = exp_row[2] if exp_row else ''
    if mode == 'custom' and custom_text: explanation = custom_text
    else: explanation = mcq.get('explanation', '')
    if tag_name: explanation = f"{explanation}\n{tag_name}" if explanation else tag_name
    return explanation[:200] if explanation else None

async def get_question_with_tags(question: str) -> str:
    tags = await db.fetchall('SELECT tag_name, position FROM tag_settings WHERE is_active = 1')
    result = question
    for tag_name, position in tags:
        if position == 'tag1': result = f"{tag_name}\n\n{result}"
        elif position == 'tag2': result = f"{result}\n\n{tag_name}"
        elif position == 'tag3': result = f"{result} {tag_name}"
        elif position == 'tag4': result = f"{tag_name}\n{result}"
    return result[:300]

async def send_single_poll(bot, chat_id: int, mcq: dict, reply_to: int = None):
    question = await get_question_with_tags(mcq.get('question', '?'))
    opts = mcq.get('options', {})
    options_list = [opts.get('A', 'Option A'), opts.get('B', 'Option B'), opts.get('C', 'Option C'), opts.get('D', 'Option D')]
    ans_str = str(mcq.get('answer', '1')).upper()
    ans_map = {'1': 0, '2': 1, '3': 2, '4': 3, 'A': 0, 'B': 1, 'C': 2, 'D': 3}
    correct_idx = ans_map.get(ans_str, 0)
    explanation = await get_explanation(mcq)
    try:
        poll_msg = await bot.send_poll(chat_id=chat_id, question=question, options=options_list,
            type='quiz', correct_option_id=correct_idx, explanation=explanation,
            is_anonymous=True, reply_to_message_id=reply_to)
        return poll_msg.message_id, True
    except: return None, False

async def get_message_link(bot, chat_id: int, message_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        if chat.username: return f"https://t.me/{chat.username}/{message_id}"
        else: return f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message_id}"
    except: return ""

# ============ /csv HANDLER ============
async def csv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ Admin only!"); return
    topic = ' '.join(context.args) if context.args else ''
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content.decode('utf-8-sig'))
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes))
    if not mcqs:
        await update.message.reply_text("❌ CSV file reply kore /csv daw!"); return
    context.user_data['poll_mcqs'] = mcqs; context.user_data['poll_topic'] = topic
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"poll_send_{ch_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="poll_cancel")])
    await update.message.reply_text(f"✅ {len(mcqs)} MCQ | 🔥 {topic or 'N/A'}\n\nSelect Channel:", reply_markup=InlineKeyboardMarkup(buttons))

# ============ /csvS HANDLER ============
async def csvs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ Admin only!"); return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("❌ /csvS <batch> or /csvS <batch> <topic> or /csvS <batch> <channel> <topic>"); return
    try: batch_size = int(args[0])
    except: await update.message.reply_text("❌ Invalid batch size!"); return
    channel_name, topic = None, "MCQ"
    if len(args) >= 3:
        arg2 = args[1]
        if arg2.startswith('@') or arg2.startswith('-100') or 't.me/' in arg2:
            channel_name = arg2; topic = ' '.join(args[2:]) if len(args) > 2 else "MCQ"
        else: topic = ' '.join(args[1:])
    elif len(args) == 2: topic = args[1]
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content_bytes = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content_bytes.decode('utf-8-sig'))
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes))
    if not mcqs: await update.message.reply_text("❌ CSV file reply kore /csvS daw!"); return
    if channel_name:
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        matched = None
        for ch_id, ch_name in channels:
            if channel_name == ch_id or channel_name == ch_name or channel_name.lower() in ch_name.lower():
                matched = ch_id; break
        if matched:
            await update.message.reply_text(f"📤 {len(mcqs)} MCQ → Batch: {batch_size}")
            asyncio.create_task(send_serial_polls(update, context, matched, mcqs, batch_size, topic))
        else: await update.message.reply_text(f"❌ Channel not found: {channel_name}")
        return
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    if not channels: await update.message.reply_text("❌ No channels!"); return
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"csvs_ch_{ch_id}_{batch_size}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="poll_cancel")])
    context.user_data['csvs_mcqs'] = mcqs; context.user_data['csvs_topic'] = topic; context.user_data['csvs_batch'] = batch_size
    await update.message.reply_text(f"📊 {len(mcqs)} MCQ | Batch: {batch_size}\nSelect Channel:", reply_markup=InlineKeyboardMarkup(buttons))

# ============ SERIAL POLL SENDER ============
async def send_serial_polls(update, context, channel_id, mcqs, batch_size, topic):
    bot = context.bot; total = len(mcqs)
    batches = [mcqs[i:i+batch_size] for i in range(0, total, batch_size)]; total_batches = len(batches)
    batch_links = []
    for b_idx, batch in enumerate(batches, 1):
        batch_topic = f"{topic} (Part-{b_idx:02d})"
        pre_text = get_pre_message(batch_topic, len(batch))
        pre_msg = await bot.send_message(chat_id=channel_id, text=pre_text)
        first_poll_id, sent = None, 0
        for mcq in batch:
            uid = update.effective_user.id if hasattr(update, "effective_user") else 0
            while GLOBAL_PAUSE.get(uid, False): await asyncio.sleep(1.5)
            poll_id, success = await send_single_poll(bot, channel_id, mcq, pre_msg.message_id)
            if success and first_poll_id is None: first_poll_id = poll_id
            if success: sent += 1
            await asyncio.sleep(1.5)
        first_link = await get_message_link(bot, channel_id, first_poll_id) if first_poll_id else ""
        ending = get_ending_message(batch_topic, sent, first_link)
        await bot.send_message(chat_id=channel_id, text=ending, disable_web_page_preview=True)
        batch_links.append((b_idx, first_link, len(batch)))
        await asyncio.sleep(1.5)
    if total_batches > 1:
        summary = get_master_summary(topic, total, total_batches, batch_links)
        await bot.send_message(chat_id=channel_id, text=summary, disable_web_page_preview=True)

# ============ CALLBACK HANDLER ============
async def handle_csv_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    if data == 'poll_cancel': await query.edit_message_text("❌ Cancelled!"); return
    if data.startswith('poll_send_'):
        channel_id = data.replace('poll_send_', '')
        mcqs = context.user_data.get('poll_mcqs', [])
        topic = context.user_data.get('poll_topic', '')
        if not mcqs:
            csv_bytes = context.user_data.get('last_csv')
            if csv_bytes:
                content_str = csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes)
                mcqs = parse_csv_to_mcqs(content_str)
        if not mcqs: await query.edit_message_text("❌ MCQ session ended!"); return
        await query.edit_message_text(f"📤 Sending {len(mcqs)} polls...")
        bot = context.bot
        pre_text = get_pre_message(topic, len(mcqs)); pre_msg = await bot.send_message(chat_id=channel_id, text=pre_text)
        first_poll_id, sent = None, 0
        for mcq in mcqs:
            uid = update.effective_user.id if hasattr(update, "effective_user") else query.from_user.id
            while GLOBAL_PAUSE.get(uid, False): await asyncio.sleep(1.5)
            poll_id, success = await send_single_poll(bot, channel_id, mcq, pre_msg.message_id)
            if success and first_poll_id is None: first_poll_id = poll_id
            if success: sent += 1
            await asyncio.sleep(1.5)
        first_link = await get_message_link(bot, channel_id, first_poll_id) if first_poll_id else ""
        ending = get_ending_message(topic, sent, first_link)
        await bot.send_message(chat_id=channel_id, text=ending, disable_web_page_preview=True)
        await query.message.reply_text(f"✅ {sent} polls sent!")
    elif data.startswith('csvs_ch_'):
        parts = data.split('_'); channel_id = parts[2]; batch_size = int(parts[3])
        mcqs = context.user_data.get('csvs_mcqs', []); topic = context.user_data.get('csvs_topic', '')
        if mcqs:
            await query.edit_message_text("📤 Serial poll starting...")
            await send_serial_polls(update, context, channel_id, mcqs, batch_size, topic)


# ============ /csvI HANDLER ============
async def csvi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ Admin only!"); return
    topic = ' '.join(context.args) if context.args else ''
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content.decode('utf-8-sig'))
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes))
    if not mcqs: await update.message.reply_text("❌ CSV file reply kore /csvI daw!"); return
    context.user_data['poll_mcqs'] = mcqs; context.user_data['poll_topic'] = topic
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"inline_send_{ch_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="poll_cancel")])
    await update.message.reply_text(f"🎯 Inline Quiz Mode\n✅ {len(mcqs)} MCQ\n\nSelect Channel:", reply_markup=InlineKeyboardMarkup(buttons))

# ============ /csvIS HANDLER ============
async def csvis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ Admin only!"); return
    args = context.args
    if len(args) < 1: await update.message.reply_text("❌ /csvIS <batch> <channel> <topic>"); return
    try: batch_size = int(args[0])
    except: await update.message.reply_text("❌ Invalid batch size!"); return
    channel_name = args[1] if len(args) >= 3 else None
    topic = ' '.join(args[2:]) if len(args) >= 3 else ' '.join(args[1:])
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content.decode('utf-8-sig'))
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes))
    if not mcqs: await update.message.reply_text("❌ CSV file reply kore /csvIS daw!"); return
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    matching = [(ch_id, ch_name) for ch_id, ch_name in channels if channel_name and channel_name.lower() in ch_name.lower()]
    if not matching: await update.message.reply_text(f"❌ Channel not found!"); return
    if len(matching) == 1:
        ch_id, ch_name = matching[0]
        await update.message.reply_text(f"🎯 Inline Serial Quiz → {ch_name}")
        asyncio.create_task(send_inline_quiz(context.bot, ch_id, mcqs, batch_size, topic))
    else:
        buttons = [[InlineKeyboardButton(f"📢 {n}", callback_data=f"csvis_ch_{id}_{batch_size}")] for id, n in matching]
        await update.message.reply_text("Select Channel:", reply_markup=InlineKeyboardMarkup(buttons))

async def send_inline_quiz(bot, chat_id, mcqs, topic="", batch_size=10):
    batch_id = hashlib.md5(f"{topic}_{datetime.now().timestamp()}".encode()).hexdigest()[:8]
    pre_text = get_pre_message(topic, len(mcqs))
    await bot.send_message(chat_id=chat_id, text=pre_text)
    for idx, mcq in enumerate(mcqs):
        q_text = mcq.get('question', '?')[:300]
        opts = mcq.get('options', {})
        opt_list = [opts.get('A', ''), opts.get('B', ''), opts.get('C', ''), opts.get('D', '')]
        q_id = f"{batch_id}_{idx}"
        from config import db as config_db
        await config_db.execute('INSERT OR REPLACE INTO quiz_meta (q_id, correct_idx, explanation, options_json, batch_id) VALUES (?, ?, ?, ?, ?)',
            (q_id, {'A':0,'B':1,'C':2,'D':3}.get(str(mcq.get('answer','A')).upper(), 0), mcq.get('explanation','')[:200], json.dumps(opt_list), batch_id))
        buttons = [[InlineKeyboardButton(f"{chr(65+i)}. {opt[:40]}", callback_data=f"iq_{i}_{q_id}")] for i, opt in enumerate(opt_list) if opt]
        await bot.send_message(chat_id=chat_id, text=q_text, reply_markup=InlineKeyboardMarkup(buttons))
        await asyncio.sleep(1.5)
    first_link = await get_message_link(bot, chat_id, 0)
    ending = get_ending_message(topic, len(mcqs), first_link)
    kb = [[InlineKeyboardButton("🔄 Retake", callback_data=f"retake_{batch_id}"), InlineKeyboardButton("📊 Result", callback_data=f"result_{batch_id}")]]
    await bot.send_message(chat_id=chat_id, text=ending, reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)
