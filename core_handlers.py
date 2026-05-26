#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - Core Handlers (/start, /img, /txt, /prompt)"""

import asyncio
import json
import re
import csv
import io
import os
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, gemini_manager
from services import (
    generate_mcqs_from_image, generate_mcqs_from_text,
    mcqs_to_csv, parse_csv_to_mcqs, format_progress
)

# ============================================================
# /start HANDLER
# ============================================================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message with all commands & details"""
    user = update.effective_user
    
    # Save user to DB
    await db.execute(
        'INSERT OR IGNORE INTO bot_users (user_id, username, first_name) VALUES (?, ?, ?)',
        (user.id, user.username, user.first_name)
    )
    
    text = f"""🌟 *ATLAS MCQ BOT-এ স্বাগতম, {user.first_name}!*

📌 *সব কমান্ড ও কাজের বিবরণ:*

🟢 *MCQ GENERATION*
• `/img` — ইমেজ থেকে MCQ বানান (সংখ্যা + টপিক নাম অপশনাল)
• `/txt` — টেক্সট থেকে MCQ বানান (প্রত্যেক লাইন থেকে)
• `/prompt` — ৭টি প্রম্পট ম্যানেজ (Edit/Activate/Delete/New)

🟡 *CSV → POLL*
• `/csv` — CSV/JSON থেকে সাধারণ Telegram Poll
• `/csvS` — সিরিয়াল পোল (ব্যাচ + Part-01, Part-02...)
• `/csvI` — ইনলাইন বাটন কুইজ (A,B,C,D বাটন)
• `/csvIS` — সিরিয়াল ইনলাইন কুইজ (ব্যাচ + Retake/Result)

🟠 *PDF TOOLS*
• `/pdfm` — PDF থেকে MCQ জেনারেট (Image/Topic Mood)
• `/qbm` — PDF থেকে Existing MCQ এক্সট্রাক্ট
• `/sheet` — CSV থেকে Practice Sheet PDF (৫ ফরম্যাট)

🔵 *FILE TOOLS*
• `/split` — বড় ফাইল ছোট ফাইলে ভাগ
• `/merge` — একাধিক ফাইল মার্জ
• `/convert` — CSV ↔ JSON কনভার্ট
• `/rename` — ফাইল রিনেম
• `/watermark` — PDF-এ ওয়াটারমার্ক

🟣 *SETTINGS*
• `/exp` — এক্সপ্লানেশন সেটিংস (Auto/Custom/Tag)
• `/tag` — প্রশ্নে ট্যাগ পজিশন (৪ ধরনের)
• `/thumb` — থাম্বনেইল সেট/রিমুভ

🔴 *ADMIN*
• `/permit` — অ্যাডমিন ম্যানেজ
• `/adminlist` — অ্যাডমিন লিস্ট
• `/broadcast` — সব/নির্দিষ্ট চ্যানেলে মেসেজ
• `/channel` — চ্যানেল ম্যানেজ

⚫ *SYSTEM*
• `/ping` — বট আপটাইম + RAM
• `/error` — বট হেলথ চেক
• `/logs` — লগ ফাইল (Owner)
• `/restart` — বট রিস্টার্ট
• `/pause` / `/resume` — পোল থামানো/চালু

🟤 *COLLECTION*
• `/collect` → `/done` — পোল কালেক্ট করে CSV

⭐ *SPECIAL*
• `.mhtml/.html` ফাইল পাঠালেই অটো CSV!

💬 *Whatsapp:* wa.me/8801999681290
🌟 *Website:* Atlascourses.com
"""
    
    buttons = [
        [InlineKeyboardButton("📸 /img - Image MCQ", callback_data="info_img"),
         InlineKeyboardButton("📝 /txt - Text MCQ", callback_data="info_txt")],
        [InlineKeyboardButton("⚙️ /prompt - Prompts", callback_data="info_prompt"),
         InlineKeyboardButton("💬 /exp - Explanation", callback_data="info_exp")],
        [InlineKeyboardButton("🏷️ /tag - Tag Setup", callback_data="info_tag"),
         InlineKeyboardButton("📊 /sheet - PDF Sheet", callback_data="info_sheet")],
        [InlineKeyboardButton("📤 /csv - CSV Poll", callback_data="info_csv"),
         InlineKeyboardButton("📥 /collect - Collect", callback_data="info_collect")],
        [InlineKeyboardButton("📄 /pdfm - PDF MCQ", callback_data="info_pdfm"),
         InlineKeyboardButton("📋 /qbm - PDF Extract", callback_data="info_qbm")],
        [InlineKeyboardButton("📡 /broadcast", callback_data="info_broadcast"),
         InlineKeyboardButton("👥 /permit - Admin", callback_data="info_permit")],
        [InlineKeyboardButton("🔧 File Tools", callback_data="info_tools"),
         InlineKeyboardButton("📌 /thumb", callback_data="info_thumb")],
        [InlineKeyboardButton("📈 /ping", callback_data="info_ping"),
         InlineKeyboardButton("🛠️ /error", callback_data="info_error")],
    ]
    
    await update.message.reply_text(
        text, 
        parse_mode=ParseMode.MARKDOWN, 
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True
    )


# ============================================================
# /img HANDLER
# ============================================================
async def img_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate MCQs from image"""
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ ইমেজে reply করে `/img` বা `/img 15` বা `/img 15 টপিক` দাও")
        return
    
    # Parse args
    args = context.args
    count = 12  # default
    topic = ''
    
    if args:
        # Try to parse first arg as number
        try:
            count = int(args[0])
            topic = ' '.join(args[1:]) if len(args) > 1 else ''
        except ValueError:
            topic = ' '.join(args)
            count = 15  # default when topic given without number
    
    # Download image
    progress_msg = await update.message.reply_text("⏳ ইমেজ ডাউনলোড হচ্ছে...")
    photo = update.message.reply_to_message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()
    if isinstance(image_bytes, bytearray):
        image_bytes = bytes(image_bytes)
    
    # Get active prompts from DB
    await progress_msg.edit_text("⏳ Active Prompt চেক করা হচ্ছে...")
    prompt_rows = await db.fetchall('SELECT content FROM prompts WHERE is_active = 1')
    if not prompt_rows:
        await progress_msg.edit_text("❌ কোনো Active Prompt নেই! `/prompt` দিয়ে Activate করো।")
        return
    
    active_prompts = [row[0] for row in prompt_rows]
    
    # Generate MCQs
    await progress_msg.edit_text(f"🤖 Gemini AI MCQ তৈরি করছে...\n📝 Target: {count}টি\n⏱️ অনুগ্রহ করে অপেক্ষা করো...")
    
    try:
        mcqs = await generate_mcqs_from_image(image_bytes, active_prompts, count)
    except Exception as e:
        await progress_msg.edit_text(f"❌ MCQ তৈরি করতে ব্যর্থ!\nকারণ: {str(e)[:100]}")
        return
    
    if not mcqs:
        await progress_msg.edit_text("❌ কোনো MCQ পাওয়া যায়নি। অন্য ইমেজ দিয়ে চেষ্টা করো।")
        return
    
    # Create CSV
    csv_bytes = mcqs_to_csv(mcqs)
    
    # Get thumbnail
    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
    thumb = thumb_row[0] if thumb_row else None
    
    # Send CSV
    await progress_msg.delete()
    csv_msg = await update.message.reply_document(
        document=csv_bytes,
        filename=f"mcq_{topic or 'generated'}.csv",
        caption=f"✅ *{len(mcqs)}টি MCQ তৈরি সম্পন্ন!*\n🔥 Topic: {topic or 'N/A'}",
        parse_mode=ParseMode.MARKDOWN,
        thumbnail=thumb
    )
    
    # Save CSV in memory for later use
    context.user_data['last_csv'] = csv_bytes
    context.user_data['last_mcqs'] = mcqs
    context.user_data['last_topic'] = topic
    
    # Show MCQ list view with edit buttons
    await show_mcq_list(update, context, mcqs, topic, 0)


# ============================================================
# /txt HANDLER
# ============================================================
async def txt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate MCQs from text"""
    # Get text
    text = None
    if update.message.reply_to_message and update.message.reply_to_message.text:
        text = update.message.reply_to_message.text
    elif context.args:
        text = ' '.join(context.args)
    
    if not text:
        await update.message.reply_text("❌ টেক্সট মেসেজে reply করে `/txt` বা `/txt 15` দাও")
        return
    
    # Parse args
    args = context.args
    count = 12
    topic = ''
    
    if args:
        try:
            count = int(args[0])
            topic = ' '.join(args[1:]) if len(args) > 1 else ''
        except ValueError:
            topic = ' '.join(args)
            count = 15
    
    progress_msg = await update.message.reply_text("⏳ MCQ তৈরি হচ্ছে...")
    
    # Get active prompts
    prompt_rows = await db.fetchall('SELECT content FROM prompts WHERE is_active = 1')
    if not prompt_rows:
        await progress_msg.edit_text("❌ কোনো Active Prompt নেই!")
        return
    
    active_prompts = [row[0] for row in prompt_rows]
    
    # Generate MCQs
    await progress_msg.edit_text(f"🤖 Gemini AI MCQ তৈরি করছে...\n📝 Target: {count}টি")
    
    try:
        mcqs = await generate_mcqs_from_text(text, active_prompts, count)
    except Exception as e:
        await progress_msg.edit_text(f"❌ MCQ তৈরি করতে ব্যর্থ!\n{str(e)[:100]}")
        return
    
    if not mcqs:
        await progress_msg.edit_text("❌ কোনো MCQ পাওয়া যায়নি।")
        return
    
    # Create CSV
    csv_bytes = mcqs_to_csv(mcqs)
    
    # Get thumbnail
    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
    thumb = thumb_row[0] if thumb_row else None
    
    await progress_msg.delete()
    await update.message.reply_document(
        document=csv_bytes,
        filename=f"mcq_{topic or 'text_generated'}.csv",
        caption=f"✅ *{len(mcqs)}টি MCQ তৈরি সম্পন্ন!*\n🔥 Topic: {topic or 'N/A'}",
        parse_mode=ParseMode.MARKDOWN,
        thumbnail=thumb
    )
    
    # Save in memory
    context.user_data['last_csv'] = csv_bytes
    context.user_data['last_mcqs'] = mcqs
    context.user_data['last_topic'] = topic
    
    # Show MCQ list
    await show_mcq_list(update, context, mcqs, topic, 0)


# ============================================================
# MCQ LIST VIEW & EDIT SYSTEM
# ============================================================
async def show_mcq_list(update, context, mcqs, topic, index):
    """Show MCQ list with edit buttons"""
    if not mcqs:
        await update.message.reply_text("❌ কোনো MCQ নেই!")
        return
    
    total = len(mcqs)
    mcq = mcqs[index]
    
    # Build MCQ display text
    q_text = mcq.get('question', '?')
    opts = mcq.get('options', {})
    ans = mcq.get('answer', '1')
    exp = mcq.get('explanation', '')
    
    display = f"""📝 *MCQ {index + 1}/{total}*

❓ *প্রশ্ন:* {q_text[:200]}

🔹 A. {opts.get('A', '')[:80]}
🔹 B. {opts.get('B', '')[:80]}
🔹 C. {opts.get('C', '')[:80]}
🔹 D. {opts.get('D', '')[:80]}

✅ *উত্তর:* {ans}
💬 *ব্যাখ্যা:* {exp[:150] if exp else 'N/A'}
"""
    
    # Build buttons
    buttons = []
    
    # Navigation row
    nav_buttons = []
    if index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"mcq_prev_{index}"))
    nav_buttons.append(InlineKeyboardButton(f"📋 {index + 1}/{total}", callback_data="mcq_noop"))
    if index < total - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"mcq_next_{index}"))
    buttons.append(nav_buttons)
    
    # Edit row
    edit_buttons = [
        InlineKeyboardButton("✏️ Edit Question", callback_data=f"mcq_editq_{index}"),
        InlineKeyboardButton("✏️ Edit Options", callback_data=f"mcq_edito_{index}")
    ]
    buttons.append(edit_buttons)
    
    edit_buttons2 = [
        InlineKeyboardButton("✏️ Edit Answer", callback_data=f"mcq_edita_{index}"),
        InlineKeyboardButton("✏️ Edit Explanation", callback_data=f"mcq_edite_{index}")
    ]
    buttons.append(edit_buttons2)
    
    # Action row
    action_buttons = [
        InlineKeyboardButton("🗑️ Delete", callback_data=f"mcq_delete_{index}"),
        InlineKeyboardButton("💾 Save CSV", callback_data="mcq_save_csv")
    ]
    buttons.append(action_buttons)
    
    # Send to channel
    buttons.append([InlineKeyboardButton("📢 Send to Channel", callback_data="mcq_send_channel")])
    
    # Store mcqs in context
    context.user_data['edit_mcqs'] = mcqs
    context.user_data['edit_index'] = index
    
    # Send or edit message
    if update.callback_query:
        await update.callback_query.edit_message_text(
            display, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            display, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )


# ============================================================
# /prompt HANDLER
# ============================================================
async def prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage prompts"""
    prompts = await db.fetchall('SELECT name, is_active FROM prompts ORDER BY id')
    
    buttons = []
    for name, is_active in prompts:
        emoji = "✅" if is_active else "💥"
        short_name = name[:30]
        buttons.append([InlineKeyboardButton(f"{emoji} {short_name}", callback_data=f"prompt_view_{name}")])
    
    buttons.append([InlineKeyboardButton("➕ নতুন Prompt যোগ করো", callback_data="prompt_add")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="start_back")])
    
    await update.message.reply_text(
        "⚙️ *Prompt Management*\n\nএক বা একাধিক Active করা যাবে।\nActive Prompts অনুযায়ী MCQ তৈরি হবে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# CORE CALLBACK HANDLER
# ============================================================
async def handle_core_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all core callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Info callbacks
    if data.startswith('info_'):
        info_texts = {
            'info_img': "📸 */img* — ইমেজ থেকে MCQ\n`/img` = 10-15 MCQ\n`/img 20` = 20 MCQ\n`/img 15 টপিক` = 15 MCQ + টপিক",
            'info_txt': "📝 */txt* — টেক্সট থেকে MCQ\nটেক্সটের প্রত্যেক লাইন থেকে MCQ বানাবে।",
            'info_prompt': "⚙️ */prompt* — ৭টি প্রম্পট ম্যানেজ\nEdit, Activate, Delete, New",
            'info_exp': "💬 */exp* — এক্সপ্লানেশন সেটিংস\nAuto / Custom / Tag Name",
            'info_tag': "🏷️ */tag* — প্রশ্নে ট্যাগ পজিশন\ntag1-4: উপরে/নিচে/পাশে/গ্যাপ সহ",
            'info_sheet': "📊 */sheet* — CSV থেকে Practice Sheet PDF\n৫ ফরম্যাট, Chromium PDF",
            'info_csv': "📤 */csv* — CSV থেকে Poll\n`/csv`, `/csvS`, `/csvI`, `/csvIS`",
            'info_collect': "📥 */collect* — পোল কালেক্ট করে CSV",
            'info_pdfm': "📄 */pdfm* — PDF থেকে MCQ জেনারেট\n`-p 1-10 -c @channel -m Title`",
            'info_qbm': "📋 */qbm* — PDF থেকে MCQ এক্সট্রাক্ট",
            'info_broadcast': "📡 */broadcast* — সব/নির্দিষ্ট চ্যানেলে মেসেজ",
            'info_permit': "👥 */permit* — অ্যাডমিন ম্যানেজমেন্ট",
            'info_tools': "🔧 */split, /merge, /convert, /rename, /watermark*",
            'info_thumb': "📌 */thumb* — থাম্বনেইল সেট/রিমুভ",
            'info_ping': "📈 */ping* — বট আপটাইম + RAM",
            'info_error': "🛠️ */error* — বট হেলথ চেক",
        }
        cmd = data.replace('info_', '')
        await query.edit_message_text(info_texts.get(data, f"/{cmd} সম্পর্কে বিস্তারিত"), parse_mode=ParseMode.MARKDOWN)
        return
    
    # MCQ Edit Callbacks
    if data.startswith('mcq_'):
        await handle_mcq_callback(update, context)
        return
    
    # Prompt Callbacks
    if data.startswith('prompt_'):
        await handle_prompt_callback(update, context)
        return
    
    if data == 'start_back':
        await start_handler(update, context)
        return


# ============================================================
# MCQ CALLBACK HANDLER
# ============================================================
async def handle_mcq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle MCQ edit callbacks"""
    query = update.callback_query
    data = query.data
    mcqs = context.user_data.get('edit_mcqs', [])
    
    if not mcqs:
        await query.edit_message_text("❌ সেশন শেষ হয়ে গেছে। আবার /img বা /txt দাও।")
        return
    
    # Navigation
    if data.startswith('mcq_prev_'):
        index = int(data.replace('mcq_prev_', '')) - 1
        await show_mcq_list(update, context, mcqs, context.user_data.get('edit_topic', ''), max(0, index))
    
    elif data.startswith('mcq_next_'):
        index = int(data.replace('mcq_next_', '')) + 1
        await show_mcq_list(update, context, mcqs, context.user_data.get('edit_topic', ''), min(len(mcqs) - 1, index))
    
    elif data.startswith('mcq_delete_'):
        index = int(data.replace('mcq_delete_', ''))
        if 0 <= index < len(mcqs):
            del mcqs[index]
            context.user_data['edit_mcqs'] = mcqs
            new_index = min(index, len(mcqs) - 1)
            if mcqs:
                await show_mcq_list(update, context, mcqs, context.user_data.get('edit_topic', ''), new_index)
            else:
                await query.edit_message_text("❌ সব MCQ ডিলিট হয়ে গেছে!")
    
    elif data.startswith('mcq_editq_'):
        index = int(data.replace('mcq_editq_', ''))
        context.user_data['editing_field'] = ('question', index)
        await query.edit_message_text(f"📝 নতুন প্রশ্ন লিখো (MCQ {index + 1}):\n\nবর্তমান: {mcqs[index].get('question', '')[:200]}")
    
    elif data.startswith('mcq_edito_'):
        index = int(data.replace('mcq_edito_', ''))
        context.user_data['editing_field'] = ('options', index)
        await query.edit_message_text(f"📝 নতুন Options লিখো (MCQ {index + 1}):\n\nফরম্যাট:\nA. অপশন A\nB. অপশন B\nC. অপশন C\nD. অপশন D")
    
    elif data.startswith('mcq_edita_'):
        index = int(data.replace('mcq_edita_', ''))
        context.user_data['editing_field'] = ('answer', index)
        await query.edit_message_text(f"📝 সঠিক উত্তর লিখো (MCQ {index + 1}):\n\nশুধু A, B, C, বা D")
    
    elif data.startswith('mcq_edite_'):
        index = int(data.replace('mcq_edite_', ''))
        context.user_data['editing_field'] = ('explanation', index)
        await query.edit_message_text(f"📝 নতুন ব্যাখ্যা লিখো (MCQ {index + 1}, max 200 chars):\n\nবর্তমান: {mcqs[index].get('explanation', '')[:150]}")
    
    elif data == 'mcq_save_csv':
        csv_bytes = mcqs_to_csv(mcqs)
        thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
        thumb = thumb_row[0] if thumb_row else None
        await query.message.reply_document(
            document=csv_bytes,
            filename="mcq_edited.csv",
            caption=f"✅ {len(mcqs)}টি MCQ সেভ করা হয়েছে!",
            thumbnail=thumb
        )
        await query.answer("✅ CSV Saved!")
    
    elif data == 'mcq_send_channel':
        # Store mcqs and show channel list
        context.user_data['send_mcqs'] = mcqs
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        buttons = []
        for ch_id, ch_name in channels:
            buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"ch_send_{ch_id}")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="ch_cancel")])
        await query.edit_message_text("📢 কোন চ্যানেলে পাঠাবে?", reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data == 'mcq_noop':
        await query.answer("📋 MCQ List View")


# ============================================================
# PROMPT CALLBACK HANDLER
# ============================================================
async def handle_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle prompt callbacks"""
    query = update.callback_query
    data = query.data
    
    if data.startswith('prompt_view_'):
        name = data.replace('prompt_view_', '')
        prompt = await db.fetchone('SELECT content, is_active FROM prompts WHERE name = ?', (name,))
        
        if prompt:
            is_active = "✅ Active" if prompt[1] else "💥 Inactive"
            buttons = [
                [
                    InlineKeyboardButton("✏️ Edit", callback_data=f"prompt_edit_{name}"),
                    InlineKeyboardButton("✅ Activate" if not prompt[1] else "❌ Deactivate", 
                                        callback_data=f"prompt_toggle_{name}")
                ],
                [
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"prompt_delete_{name}"),
                    InlineKeyboardButton("🔙 Back", callback_data="prompt_back")
                ]
            ]
            await query.edit_message_text(
                f"📝 *{name}*\n{is_active}\n\n{prompt[0][:500]}...",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    
    elif data.startswith('prompt_toggle_'):
        name = data.replace('prompt_toggle_', '')
        current = await db.fetchone('SELECT is_active FROM prompts WHERE name = ?', (name,))
        if current:
            new_state = 0 if current[0] else 1
            await db.execute('UPDATE prompts SET is_active = ? WHERE name = ?', (new_state, name))
            await query.answer(f"✅ {name} {'Activated' if new_state else 'Deactivated'}!")
            await prompt_handler(update, context)
    
    elif data.startswith('prompt_edit_'):
        name = data.replace('prompt_edit_', '')
        context.user_data['editing_prompt'] = name
        await query.edit_message_text(f"📝 *{name}* এর নতুন কন্টেন্ট লিখো:", parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith('prompt_delete_'):
        name = data.replace('prompt_delete_', '')
        if "Prompt-01" in name:
            await query.answer("❌ Prompt-01 ডিলিট করা যাবে না!")
        else:
            await db.execute('DELETE FROM prompts WHERE name = ?', (name,))
            await query.answer("🗑️ Deleted!")
            await prompt_handler(update, context)
    
    elif data == 'prompt_back':
        await prompt_handler(update, context)
    
    elif data == 'prompt_add':
        context.user_data['adding_prompt'] = True
        await query.edit_message_text("📝 নতুন Prompt এর নাম লিখো:")


# ============================================================
# MESSAGE HANDLER (for editing MCQs, prompts, etc.)
# ============================================================
async def handle_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages for editing"""
    text = update.message.text
    user_id = update.effective_user.id
    
    # Editing MCQ field
    if 'editing_field' in context.user_data:
        field, index = context.user_data['editing_field']
        mcqs = context.user_data.get('edit_mcqs', [])
        
        if 0 <= index < len(mcqs):
            if field == 'question':
                mcqs[index]['question'] = text
            elif field == 'options':
                opts = {}
                for line in text.split('\n'):
                    match = re.match(r'([A-D])[.)\s]+(.+)', line.strip())
                    if match:
                        opts[match.group(1)] = match.group(2).strip()
                if len(opts) == 4:
                    mcqs[index]['options'] = opts
                else:
                    await update.message.reply_text("❌ ৪টি অপশন দাও (A. B. C. D.)")
                    return
            elif field == 'answer':
                if text.upper() in ['A', 'B', 'C', 'D']:
                    mcqs[index]['answer'] = text.upper()
                else:
                    await update.message.reply_text("❌ শুধু A, B, C, বা D লিখো!")
                    return
            elif field == 'explanation':
                mcqs[index]['explanation'] = text[:200]
            
            context.user_data['edit_mcqs'] = mcqs
            del context.user_data['editing_field']
            await update.message.reply_text("✅ আপডেট সম্পন্ন!")
            await show_mcq_list(update, context, mcqs, context.user_data.get('edit_topic', ''), index)
        return
    
    # Adding new prompt
    if context.user_data.get('adding_prompt'):
        name = text.strip()
        await db.execute('INSERT OR IGNORE INTO prompts (name, content, is_active) VALUES (?, ?, 0)', (name, ''))
        context.user_data['adding_prompt'] = False
        context.user_data['editing_prompt'] = name
        await update.message.reply_text(f"✅ Prompt '{name}' তৈরি! এখন কন্টেন্ট লিখো:")
        return
    
    # Editing prompt content
    if 'editing_prompt' in context.user_data:
        name = context.user_data['editing_prompt']
        await db.execute('UPDATE prompts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?', (text, name))
        del context.user_data['editing_prompt']
        await update.message.reply_text(f"✅ Prompt '{name}' আপডেট সম্পন্ন!")
        return
