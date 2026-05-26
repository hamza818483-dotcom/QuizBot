#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core Handlers - /start, /img, /txt, /prompt, /exp, /tag"""

import asyncio
import json
import re
import csv
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, gemini_manager, imgbb_manager

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start command"""
    buttons = [
        [
            InlineKeyboardButton("📸 /img - Image MCQ", callback_data="start_img"),
            InlineKeyboardButton("📝 /txt - Text MCQ", callback_data="start_txt")
        ],
        [
            InlineKeyboardButton("⚙️ /prompt - Prompts", callback_data="start_prompt"),
            InlineKeyboardButton("💬 /exp - Explanation", callback_data="start_exp")
        ],
        [
            InlineKeyboardButton("🏷️ /tag - Tag Setup", callback_data="start_tag"),
            InlineKeyboardButton("📊 /sheet - PDF Sheet", callback_data="start_sheet")
        ],
        [
            InlineKeyboardButton("📤 /csv - CSV Poll", callback_data="start_csv"),
            InlineKeyboardButton("📥 /collect - Collect", callback_data="start_collect")
        ],
        [
            InlineKeyboardButton("📄 /pdfm - PDF MCQ", callback_data="start_pdfm"),
            InlineKeyboardButton("📋 /qbm - PDF Extract", callback_data="start_qbm")
        ],
        [
            InlineKeyboardButton("📡 /broadcast", callback_data="start_broadcast"),
            InlineKeyboardButton("👥 /permit - Admin", callback_data="start_permit")
        ],
        [
            InlineKeyboardButton("🔧 /split", callback_data="start_split"),
            InlineKeyboardButton("🔄 /convert", callback_data="start_convert")
        ],
        [
            InlineKeyboardButton("🌐 MHTML CSV", callback_data="start_mhtml"),
            InlineKeyboardButton("📌 /thumb", callback_data="start_thumb")
        ],
        [
            InlineKeyboardButton("📈 /ping", callback_data="start_ping"),
            InlineKeyboardButton("🛠️ /error", callback_data="start_error")
        ]
    ]
    
    await update.message.reply_text(
        "🌟 ATLAS MCQ Bot-এ স্বাগতম!\nনিচের বাটন থেকে যা দরকার বেছে নাও 👇",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def img_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/img - Generate MCQs from image"""
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ Image-এ reply করে /img দাও")
        return
    
    # Parse args
    args = context.args
    count = 12
    topic = ''
    
    if args:
        try:
            count = int(args[0])
        except:
            topic = ' '.join(args)
    
    # Download image
    progress = await update.message.reply_text("⏳ MCQ তৈরি হচ্ছে... (0%)")
    photo = update.message.reply_to_message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()
    
    # Get active prompt
    prompt_row = await db.fetchone('SELECT content FROM prompts WHERE is_active = 1')
    if not prompt_row:
        await progress.edit_text("❌ কোন active prompt নেই")
        return
    
    base_prompt = prompt_row[0]
    
    # Build full prompt
    full_prompt = f"""{base_prompt}

Generate exactly {count} MCQs.
{f"Topic: {topic}" if topic else ""}

Output ONLY valid JSON array:
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A","explanation":"..."}}]"""
    
    # Call Gemini
    await progress.edit_text("⏳ MCQ তৈরি হচ্ছে... (50%)")
    
    # Upload image to ImgBB first
    img_url = imgbb_manager.upload(bytes(image_bytes))
    
    response = await gemini_manager.call(full_prompt, image=image_bytes)
    
    # Parse JSON
    json_match = re.search(r'\[.*\]', response, re.DOTALL)
    if not json_match:
        await progress.edit_text("❌ MCQ generate করতে পারিনি")
        return
    
    mcqs = json.loads(json_match.group())
    
    # Create CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['question', 'A', 'B', 'C', 'D', 'answer', 'explanation'])
    writer.writeheader()
    
    for mcq in mcqs:
        writer.writerow({
            'question': mcq['question'],
            'A': mcq['options']['A'],
            'B': mcq['options']['B'],
            'C': mcq['options']['C'],
            'D': mcq['options']['D'],
            'answer': mcq['answer'],
            'explanation': mcq.get('explanation', '')
        })
    
    # Get channels
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"img_send_{ch_id}")])
    buttons.append([InlineKeyboardButton("📤 শুধু CSV", callback_data="img_csv")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="img_cancel")])
    
    # Store in context
    context.user_data['img_mcqs'] = mcqs
    context.user_data['img_csv'] = output.getvalue()
    
    await progress.edit_text(
        f"✅ {len(mcqs)}টি MCQ তৈরি সম্পন্ন!\nChannel select করো:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def txt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/txt - Generate MCQs from text"""
    # Get text
    text = None
    if update.message.reply_to_message:
        text = update.message.reply_to_message.text
    elif context.args:
        text = ' '.join(context.args)
    
    if not text:
        await update.message.reply_text("❌ Text message-এ reply করো বা text লেখো")
        return
    
    progress = await update.message.reply_text("⏳ MCQ তৈরি হচ্ছে...")
    
    # Get active prompt
    prompt_row = await db.fetchone('SELECT content FROM prompts WHERE is_active = 1')
    if not prompt_row:
        await progress.edit_text("❌ কোন active prompt নেই")
        return
    
    base_prompt = prompt_row[0]
    
    # Build prompt
    full_prompt = f"""{base_prompt}

Generate 10-15 MCQs from this text:
{text[:3000]}

Output ONLY valid JSON array:
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A","explanation":"..."}}]"""
    
    response = await gemini_manager.call(full_prompt)
    
    # Parse JSON
    json_match = re.search(r'\[.*\]', response, re.DOTALL)
    if not json_match:
        await progress.edit_text("❌ MCQ generate করতে পারিনি")
        return
    
    mcqs = json.loads(json_match.group())
    
    # Create CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['question', 'A', 'B', 'C', 'D', 'answer', 'explanation'])
    writer.writeheader()
    
    for mcq in mcqs:
        writer.writerow({
            'question': mcq['question'],
            'A': mcq['options']['A'],
            'B': mcq['options']['B'],
            'C': mcq['options']['C'],
            'D': mcq['options']['D'],
            'answer': mcq['answer'],
            'explanation': mcq.get('explanation', '')
        })
    
    # Send CSV
    await update.message.reply_document(
        document=output.getvalue().encode('utf-8'),
        filename='mcqs.csv',
        caption=f"✅ {len(mcqs)}টি MCQ তৈরি সম্পন্ন!"
    )
    await progress.delete()


async def prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/prompt - Manage prompts"""
    prompts = await db.fetchall('SELECT name, is_active FROM prompts')
    
    buttons = []
    for name, is_active in prompts:
        emoji = "✅" if is_active else "💥"
        buttons.append([InlineKeyboardButton(f"{emoji} {name}", callback_data=f"prompt_view_{name}")])
    
    buttons.append([InlineKeyboardButton("➕ নতুন Prompt যোগ করো", callback_data="prompt_add")])
    
    await update.message.reply_text(
        "⚙️ Prompt Management:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def exp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/exp - Explanation settings"""
    buttons = [
        [InlineKeyboardButton("💥 Custom Exp", callback_data="exp_custom")],
        [InlineKeyboardButton("💥 Auto", callback_data="exp_auto")],
        [InlineKeyboardButton("🏷️ Tag Name", callback_data="exp_tag")]
    ]
    
    await update.message.reply_text(
        "💬 Explanation Mode:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def tag_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tag - Tag settings"""
    buttons = [
        [InlineKeyboardButton("/tag1 — প্রশ্নের এক লাইন নিচে", callback_data="tag_type1")],
        [InlineKeyboardButton("/tag2 — প্রশ্নের এক লাইন উপরে", callback_data="tag_type2")],
        [InlineKeyboardButton("/tag3 — প্রশ্নের পরপরই inline", callback_data="tag_type3")]
    ]
    
    await update.message.reply_text(
        "🏷️ Tag Position:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def handle_core_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle core callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith('start_'):
        await query.edit_message_text(f"ℹ️ Use the command: /{data.replace('start_', '')}")
    
    elif data.startswith('prompt_view_'):
        name = data.replace('prompt_view_', '')
        prompt = await db.fetchone('SELECT content, is_active FROM prompts WHERE name = ?', (name,))
        
        if prompt:
            buttons = [
                [
                    InlineKeyboardButton("✏️ Edit", callback_data=f"prompt_edit_{name}"),
                    InlineKeyboardButton("✅ Activate", callback_data=f"prompt_activate_{name}")
                ],
                [
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"prompt_delete_{name}"),
                    InlineKeyboardButton("🔙 Back", callback_data="prompt_back")
                ]
            ]
            
            await query.edit_message_text(
                f"📝 {name}\n\n{prompt[0][:500]}...",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    
    elif data.startswith('prompt_activate_'):
        name = data.replace('prompt_activate_', '')
        await db.execute('UPDATE prompts SET is_active = 0')
        await db.execute('UPDATE prompts SET is_active = 1 WHERE name = ?', (name,))
        await query.edit_message_text(f"✅ {name} activated!")
    
    elif data.startswith('prompt_edit_'):
        name = data.replace('prompt_edit_', '')
        await query.edit_message_text(f"📝 নতুন content লেখো {name} এর জন্য:")
        context.user_data['editing_prompt'] = name
    
    elif data.startswith('prompt_delete_'):
        name = data.replace('prompt_delete_', '')
        await db.execute('DELETE FROM prompts WHERE name = ?', (name,))
        await query.edit_message_text(f"🗑️ {name} deleted!")
    
    elif data == 'prompt_back':
        await prompt_handler(update, context)
    
    elif data == 'prompt_add':
        await query.edit_message_text("📝 নতুন prompt এর name লেখো:")
        context.user_data['adding_prompt'] = True
    
    elif data.startswith('exp_'):
        mode = data.replace('exp_', '')
        await db.execute('UPDATE exp_settings SET mode = ? WHERE id = 1', (mode,))
        await query.edit_message_text(f"✅ Explanation mode set to: {mode}")
    
    elif data.startswith('tag_'):
        await query.edit_message_text("🏷️ Tag settings updated")
    
    elif data == 'img_cancel':
        await query.edit_message_text("❌ Cancelled")
    
    elif data == 'img_csv':
        csv_data = context.user_data.get('img_csv', '')
        await query.message.reply_document(
            document=csv_data.encode('utf-8'),
            filename='mcqs.csv'
        )
        await query.edit_message_text("✅ CSV পাঠানো হয়েছে")
