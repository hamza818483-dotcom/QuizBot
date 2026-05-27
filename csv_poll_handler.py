from global_state import GLOBAL_PAUSE
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - CSV to Poll Handler (/csv, /csvS, /csvI, /csvIS)"""

import asyncio
import json
import csv
import io
import re
import time
import hashlib
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, Config
from services import parse_csv_to_mcqs

# ============================================================
# PRE MESSAGE
# ============================================================
def get_pre_message(topic: str, count: int) -> str:
    """Generate pre-message for polls"""
    topic_text = f'"{topic}"' if topic else ""
    return f"""🌟Important Poll Solve By ATLAS
🔥Topic Name: {topic_text}

✅প্রশ্ন সংখ্যা: {count}"""


# ============================================================
# ENDING MESSAGE
# ============================================================
def get_ending_message(topic: str, count: int, first_link: str = "") -> str:
    """Generate ending message for polls"""
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


# ============================================================
# MASTER SUMMARY
# ============================================================
def get_master_summary(topic: str, total: int, total_batches: int, batch_links: list) -> str:
    """Generate master summary for multi-batch polls"""
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


# ============================================================
# GET POLL EXPLANATION
# ============================================================
async def get_explanation(mcq: dict) -> str:
    """Get explanation based on /exp settings"""
    exp_row = await db.fetchone('SELECT mode, custom_text, tag_name FROM exp_settings WHERE id = 1')
    mode = exp_row[0] if exp_row else 'auto'
    custom_text = exp_row[1] if exp_row else ''
    tag_name = exp_row[2] if exp_row else ''
    
    if mode == 'custom' and custom_text:
        explanation = custom_text
    else:
        explanation = mcq.get('explanation', '')
    
    # Add tag name after explanation
    if tag_name:
        explanation = f"{explanation}\n{tag_name}" if explanation else tag_name
    
    return explanation[:200] if explanation else None


# ============================================================
# GET QUESTION WITH TAGS
# ============================================================
async def get_question_with_tags(question: str) -> str:
    """Add tags to question based on /tag settings"""
    tags = await db.fetchall('SELECT tag_name, position FROM tag_settings WHERE is_active = 1')
    
    result = question
    for tag_name, position in tags:
        if position == 'tag1':
            result = f"{tag_name}\n\n{result}"
        elif position == 'tag2':
            result = f"{result}\n\n{tag_name}"
        elif position == 'tag3':
            result = f"{result} {tag_name}"
        elif position == 'tag4':
            result = f"{tag_name}\n{result}"
    
    return result[:300]


# ============================================================
# SEND SINGLE POLL
# ============================================================
async def send_single_poll(bot, chat_id: int, mcq: dict, reply_to: int = None):
    """Send a single Telegram quiz poll"""
    question = await get_question_with_tags(mcq.get('question', '?'))
    opts = mcq.get('options', {})
    options_list = [
        opts.get('A', 'Option A'),
        opts.get('B', 'Option B'),
        opts.get('C', 'Option C'),
        opts.get('D', 'Option D')
    ]
    
    ans_str = str(mcq.get('answer', '1')).upper()
    ans_map = {'1': 0, '2': 1, '3': 2, '4': 3, 'A': 0, 'B': 1, 'C': 2, 'D': 3}
    correct_idx = ans_map.get(ans_str, 0)
    
    explanation = await get_explanation(mcq)
    
    try:
        poll_msg = await bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options_list,
            type='quiz',
            correct_option_id=correct_idx,
            explanation=explanation,
            is_anonymous=True,
            reply_to_message_id=reply_to
        )
        return poll_msg.message_id, True
    except Exception as e:
        return None, False


# ============================================================
# GET MESSAGE LINK
# ============================================================
async def get_message_link(bot, chat_id: int, message_id: int) -> str:
    """Get Telegram message link"""
    try:
        chat = await bot.get_chat(chat_id)
        if chat.username:
            return f"https://t.me/{chat.username}/{message_id}"
        else:
            chat_id_str = str(chat_id).replace('-100', '')
            return f"https://t.me/c/{chat_id_str}/{message_id}"
    except:
        return ""


# ============================================================
# /csv HANDLER
# ============================================================
async def csv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send CSV/JSON as regular Telegram Quiz Polls"""
    user_id = update.effective_user.id
    
    # Check admin
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনরা ব্যবহার করতে পারবে!")
        return
    
    # Get topic from args
    topic = ' '.join(context.args) if context.args else ''
    
    # Get file (reply or stored)
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content = await file.download_as_bytearray()
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else csv_bytes)
    
    if not mcqs:
        await update.message.reply_text("❌ CSV/JSON ফাইলে reply করে `/csv` দাও, অথবা আগে `/img` বা `/txt` দিয়ে MCQ বানাও!")
        return
    
    # Store for later
    context.user_data['poll_mcqs'] = mcqs
    context.user_data['poll_topic'] = topic
    
    # Show channel list
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"poll_send_{ch_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="poll_cancel")])
    
    await update.message.reply_text(
        f"✅ *{len(mcqs)}টি MCQ* | 🔥 {topic or 'N/A'}\n\nকোন চ্যানেলে পাঠাবে?",
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /csvS HANDLER
# ============================================================
async def csvs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send CSV as serial batch polls - supports @username, -100id, t.me link, name"""
    user_id = update.effective_user.id
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ Admin only!")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("❌ /csvS <batch> or /csvS <batch> <topic> or /csvS <batch> <channel> <topic>\nChannel: @name, -100id, https://t.me/name")
        return
    try: batch_size = int(args[0])
    except: await update.message.reply_text("❌ Invalid batch size!"); return
    
    # Parse: /csvS 5 | /csvS 5 Topic | /csvS 5 @Ch Topic | /csvS 5 -100id Topic | /csvS 5 link Topic
    channel_name, topic = None, "MCQ"
    if len(args) >= 3:
        arg2 = args[1]
        if arg2.startswith('@') or arg2.startswith('-100') or 't.me/' in arg2:
            channel_name = arg2
            topic = ' '.join(args[2:]) if len(args) > 2 else "MCQ"
        else:
            topic = ' '.join(args[1:])
    elif len(args) == 2:
        topic = args[1]
    
    # Get MCQs
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content_bytes = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content_bytes.decode('utf-8-sig'))
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes))
    if not mcqs:
        await update.message.reply_text("❌ CSV file reply kore /csvS daw!"); return
    
    # If channel specified, find matching
    if channel_name:
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        matched = None
        for ch_id, ch_name in channels:
            if channel_name == ch_id or channel_name == ch_name or channel_name.lower() in ch_name.lower():
                matched = ch_id; break
        if matched:
            await update.message.reply_text(f"📤 {len(mcqs)} MCQ → Batch: {batch_size}")
            asyncio.create_task(send_serial_polls(update, context, matched, mcqs, batch_size, topic))
            return
        else:
            await update.message.reply_text(f"❌ Channel not found: {channel_name}"); return
    
    # Show channel list
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    if not channels: await update.message.reply_text("❌ No channels!"); return
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"csvs_ch_{ch_id}_{batch_size}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="poll_cancel")])
    context.user_data['csvs_mcqs'] = mcqs
    context.user_data['csvs_topic'] = topic
    context.user_data['csvs_batch'] = batch_size
    await update.message.reply_text(f"📊 {len(mcqs)} MCQ | Batch: {batch_size}\nSelect Channel:", reply_markup=InlineKeyboardMarkup(buttons))

async def csvi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send CSV as Inline Button Quiz"""
    user_id = update.effective_user.id
    
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনরা ব্যবহার করতে পারবে!")
        return
    
    topic = ' '.join(context.args) if context.args else ''
    
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content = await file.download_as_bytearray()
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else csv_bytes)
    
    if not mcqs:
        await update.message.reply_text("❌ CSV ফাইলে reply করে `/csvI` দাও!")
        return
    
    context.user_data['poll_mcqs'] = mcqs
    context.user_data['poll_topic'] = topic
    context.user_data['inline_mode'] = True
    
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"inline_send_{ch_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="poll_cancel")])
    
    await update.message.reply_text(
        f"🎯 *Inline Quiz Mode*\n✅ {len(mcqs)}টি MCQ\n\nকোন চ্যানেলে?",
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /csvIS HANDLER (Serial Inline Quiz)
# ============================================================
async def csvis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send CSV as Serial Inline Button Quiz"""
    user_id = update.effective_user.id
    
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনরা ব্যবহার করতে পারবে!")
        return
    
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("❌ `/csvIS <ব্যাচ> <চ্যানেল> <টপিক>`")
        return
    
    try:
        batch_size = int(args[0])
    except:
        await update.message.reply_text("❌ ব্যাচ সংখ্যা সঠিক নয়!")
        return
    
    channel_name = args[1] if len(args) >= 3 else None
    topic = ' '.join(args[2:])
    
    mcqs = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        file = await update.message.reply_to_message.document.get_file()
        content = await file.download_as_bytearray()
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else csv_bytes)
    
    if not mcqs:
        await update.message.reply_text("❌ CSV ফাইলে reply করে `/csvIS` দাও!")
        return
    
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    matching = [(ch_id, ch_name) for ch_id, ch_name in channels if channel_name and channel_name and channel_name.lower() in ch_name.lower()]
    
    if not matching:
        await update.message.reply_text(f"❌ '{channel_name}' চ্যানেল পাওয়া যায়নি!")
        return
    
    if len(matching) == 1:
        ch_id, ch_name = matching[0]
        await update.message.reply_text(f"🎯 Inline Serial Quiz → {ch_name}")
        asyncio.create_task(send_serial_inline(update, context, ch_id, mcqs, batch_size, topic))
    else:
        context.user_data['csvis_mcqs'] = mcqs
        context.user_data['csvis_topic'] = topic
        context.user_data['csvis_batch'] = batch_size
        
        buttons = [[InlineKeyboardButton(f"📢 {n}", callback_data=f"csvis_ch_{id}_{batch_size}")] for id, n in matching]
        await update.message.reply_text("চ্যানেল সিলেক্ট:", reply_markup=InlineKeyboardMarkup(buttons))


# ============================================================
# SEND SERIAL POLLS (/csvS)
# ============================================================
async def send_serial_polls(update, context, channel_id, mcqs, batch_size, topic):
    bot = context.bot
    total = len(mcqs)
    batches = [mcqs[i:i+batch_size] for i in range(0, total, batch_size)]
    total_batches = len(batches)
    
    batch_links = []
    
    for b_idx, batch in enumerate(batches, 1):
        batch_topic = f"{topic} (Part-{b_idx:02d})"
        pre_text = get_pre_message(batch_topic, len(batch))
        pre_msg = await bot.send_message(chat_id=channel_id, text=pre_text)
        reply_to = pre_msg.message_id
        
        first_poll_id = None
        sent = 0
        
        for mcq in batch:
            uid = update.effective_user.id if hasattr(update, "effective_user") else query.from_user.id if query else 0
            while GLOBAL_PAUSE.get(uid, False):
                await asyncio.sleep(1.5)
            poll_id, success = await send_single_poll(bot, channel_id, mcq, reply_to)
            if success and first_poll_id is None:
                first_poll_id = poll_id
            if success:
                sent += 1
            await asyncio.sleep(1.5)
        
        first_link = await get_message_link(bot, channel_id, first_poll_id) if first_poll_id else ""
        ending = get_ending_message(batch_topic, sent, first_link)
        await bot.send_message(chat_id=channel_id, text=ending, disable_web_page_preview=True)
        batch_links.append((b_idx, first_link, len(batch)))
        await asyncio.sleep(1.5)
    
    if total_batches > 1:
        summary = get_master_summary(topic, total, total_batches, batch_links)
        await bot.send_message(chat_id=channel_id, text=summary, disable_web_page_preview=True)

async def send_inline_quiz(bot, chat_id, mcqs, topic=""):
    """Send inline button quiz"""
    batch_id = hashlib.md5(f"{topic}_{datetime.now().timestamp()}".encode()).hexdigest()[:8]
    first_msg_id = None
    
    # Pre-message
    pre_text = get_pre_message(topic, len(mcqs))
    await bot.send_message(chat_id=chat_id, text=pre_text)
    
    for idx, mcq in enumerate(mcqs):
        q_text = mcq.get('question', '?')[:300]
        opts = mcq.get('options', {})
        opt_list = [opts.get('A', ''), opts.get('B', ''), opts.get('C', ''), opts.get('D', '')]
        
        q_id = f"{batch_id}_{idx}"
        
        # Store quiz data
        from config import db as config_db
        await config_db.execute(
            'INSERT OR REPLACE INTO quiz_meta (q_id, correct_idx, explanation, options_json, batch_id) VALUES (?, ?, ?, ?, ?)',
            (q_id, {'A':0,'B':1,'C':2,'D':3}.get(str(mcq.get('answer','A')).upper(), 0),
             mcq.get('explanation', '')[:200], json.dumps(opt_list), batch_id)
        )
        
        buttons = [[InlineKeyboardButton(f"{chr(65+i)}. {opt[:40]}", callback_data=f"iq_{i}_{q_id}")] 
                   for i, opt in enumerate(opt_list) if opt]
        
        msg = await bot.send_message(chat_id=chat_id, text=q_text, reply_markup=InlineKeyboardMarkup(buttons))
        
        if first_msg_id is None:
            first_msg_id = msg.message_id
        
        await asyncio.sleep(1.5)
    
    # Ending with Retake/Result
    first_link = await get_message_link(bot, chat_id, first_msg_id) if first_msg_id else ""
    ending = get_ending_message(topic, len(mcqs), first_link)
    
    kb = [[InlineKeyboardButton("🔄 Retake", callback_data=f"retake_{batch_id}"),
           InlineKeyboardButton("📊 Result", callback_data=f"result_{batch_id}")]]
    
    await bot.send_message(chat_id=chat_id, text=ending, reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)


# ============================================================
# SEND SERIAL INLINE
# ============================================================
async def send_serial_inline(update, context, channel_id, mcqs, batch_size, topic):
    """Send serial inline quizzes"""
    bot = context.bot
    total = len(mcqs)
    batches = [mcqs[i:i+batch_size] for i in range(0, total, batch_size)]
    
    msg_target = update.message if update.message else update.callback_query.message if hasattr(update, 'callback_query') else None
    progress = await msg_target.reply_text(f"🎯 Inline Serial → {len(batches)} ব্যাচ")
    
    for b_idx, batch in enumerate(batches, 1):
        batch_topic = f"{topic} (Part-{b_idx:02d})"
        await send_inline_quiz(bot, channel_id, batch, batch_topic)
        await progress.edit_text(f"🎯 Part-{b_idx:02d}/{len(batches)} সম্পন্ন!")
        await asyncio.sleep(5)
    
    await progress.edit_text(f"✅ সব ব্যাচ সম্পন্ন! {total}টি MCQ → {channel_id}")


# ============================================================
# CSV POLL CALLBACK HANDLER
# ============================================================
async def handle_csv_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle CSV poll callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'poll_cancel':
        await query.edit_message_text("❌ বাতিল!")
        return
    
    # Regular poll channel select
    if data.startswith('poll_send_'):
        channel_id = data.replace('poll_send_', '')
        mcqs = context.user_data.get('poll_mcqs', [])
        topic = context.user_data.get('poll_topic', '')
        
        if not mcqs:
            # Try to get from last_csv
            csv_bytes = context.user_data.get('last_csv')
            if csv_bytes:
                from services import parse_csv_to_mcqs
                content_str = csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes)
                mcqs = parse_csv_to_mcqs(content_str)
        if not mcqs:
            await query.edit_message_text("❌ MCQ সেশন শেষ! আবার /img বা /txt দাও।")
            return
        
        await query.edit_message_text(f"📤 {len(mcqs)}টি পোল পাঠানো শুরু...")
        
        # Send polls
        bot = context.bot
        pre_text = get_pre_message(topic, len(mcqs))
        pre_msg = await bot.send_message(chat_id=channel_id, text=pre_text)
        
        first_poll_id = None
        sent = 0
        
        for mcq in mcqs:
            uid = update.effective_user.id if hasattr(update, "effective_user") else query.from_user.id if query else 0
            while GLOBAL_PAUSE.get(uid, False):
                await asyncio.sleep(1.5)
            
            poll_id, success = await send_single_poll(bot, channel_id, mcq, pre_msg.message_id)
            if success and first_poll_id is None:
                first_poll_id = poll_id
            if success:
                sent += 1
            await asyncio.sleep(1.5)
        
        first_link = await get_message_link(bot, channel_id, first_poll_id) if first_poll_id else ""
        ending = get_ending_message(topic, sent, first_link)
        await bot.send_message(chat_id=channel_id, text=ending, disable_web_page_preview=True)
        
        await query.message.reply_text(f"✅ {sent}টি পোল পাঠানো সম্পন্ন!\n📢 {channel_id}")
    
    # Serial poll channel select
    elif data.startswith('csvs_ch_'):
        parts = data.split('_')
        channel_id = parts[2]
        batch_size = int(parts[3])
        
        mcqs = context.user_data.get('csvs_mcqs', [])
        topic = context.user_data.get('csvs_topic', '')
        
        if mcqs:
            await query.edit_message_text(f"📤 সিরিয়াল পোল শুরু...")
            await send_serial_polls(update, context, channel_id, mcqs, batch_size, topic)
    
    # Inline quiz channel select
    elif data.startswith('inline_send_'):
        channel_id = data.replace('inline_send_', '')
        mcqs = context.user_data.get('poll_mcqs', [])
        topic = context.user_data.get('poll_topic', '')
        
        if mcqs:
            await query.edit_message_text(f"🎯 Inline Quiz পাঠানো হচ্ছে...")
            await send_inline_quiz(context.bot, channel_id, mcqs, topic)
            await query.message.reply_text(f"✅ Inline Quiz → {channel_id}")
    
    # Serial inline channel select
    elif data.startswith('csvis_ch_'):
        parts = data.split('_')
        channel_id = parts[2]
        batch_size = int(parts[3])
        
        mcqs = context.user_data.get('csvis_mcqs', [])
        topic = context.user_data.get('csvis_topic', '')
        
        if mcqs:
            await query.edit_message_text("🎯 Serial Inline শুরু...")
            await send_serial_inline(update, context, channel_id, mcqs, batch_size, topic)
    
    # Inline quiz answer click
    elif data.startswith('iq_'):
        await handle_inline_answer(update, context)
    
    # Retake
    elif data.startswith('retake_'):
        await handle_retake(update, context)
    
    # Result
    elif data.startswith('result_'):
        await handle_result(update, context)


# ============================================================
# INLINE QUIZ ANSWER HANDLER
# ============================================================
async def handle_inline_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline quiz answer selection"""
    query = update.callback_query
    data = query.data  # iq_0_q_id
    
    try:
        parts = data.split('_')
        sel_idx = int(parts[1])
        q_id = parts[2]
    except:
        await query.answer("❌ Error!")
        return
    
    # Check if already answered
    from config import db as config_db
    user_id = query.from_user.id
    
    # Get correct answer
    meta = await config_db.fetchone('SELECT correct_idx, explanation, options_json FROM quiz_meta WHERE q_id = ?', (q_id,))
    if not meta:
        await query.answer("❌ Quiz not found!", show_alert=True)
        return
    
    correct_idx, explanation, opts_json = meta
    opts = json.loads(opts_json)
    
    # Update buttons
    buttons = []
    for i, opt in enumerate(opts):
        icon = " ✅" if i == correct_idx else " ❌" if i == sel_idx else ""
        buttons.append([InlineKeyboardButton(f"{chr(65+i)}. {opt[:40]}{icon}", callback_data="done")])
    
    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    except:
        pass
    
    # Show result
    if sel_idx == correct_idx:
        result = "✅ সঠিক!"
    else:
        result = f"❌ ভুল! সঠিক: {chr(65+correct_idx)}"
    
    await query.answer(f"{result}\n\n📖 {explanation[:150]}", show_alert=True)


# ============================================================
# RETAKE HANDLER
# ============================================================
async def handle_retake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset inline quiz for retake"""
    query = update.callback_query
    batch_id = query.data.replace('retake_', '')
    
    from config import db as config_db
    
    # Get all quiz messages for this batch
    msgs = await config_db.fetchall('SELECT msg_id, chat_id, q_id FROM batch_tracking WHERE batch_id = ?', (batch_id,))
    
    for msg_id, chat_id, q_id in msgs:
        meta = await config_db.fetchone('SELECT options_json FROM quiz_meta WHERE q_id = ?', (q_id,))
        if meta:
            opts = json.loads(meta[0])
            buttons = [[InlineKeyboardButton(f"{chr(65+i)}. {opt[:40]}", callback_data=f"iq_{i}_{q_id}")] 
                       for i, opt in enumerate(opts) if opt]
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, 
                                                            reply_markup=InlineKeyboardMarkup(buttons))
            except:
                pass
    
    await query.answer("🔄 সব পোল রিফ্রেশ হয়েছে! আবার চেষ্টা করো।", show_alert=True)


# ============================================================
# RESULT HANDLER
# ============================================================
async def handle_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show inline quiz result"""
    query = update.callback_query
    batch_id = query.data.replace('result_', '')
    user_id = query.from_user.id
    
    await query.answer("📊 Result দেখানো হচ্ছে...", show_alert=True)
    
    # Placeholder - full implementation with DB tracking
    await query.message.reply_text(
        f"📊 *Quiz Result*\n\n✅ সঠিক: ?\n❌ ভুল: ?\n🎯 অ্যাকুরেসি: ?%\n\n🔄 Retake দিয়ে আবার চেষ্টা করো!",
        parse_mode=None
    )
