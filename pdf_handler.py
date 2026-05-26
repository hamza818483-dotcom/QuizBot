#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - PDF Handlers (/pdfm, /qbm) with Image/Topic Mood"""

import os
import re
import io
import json
import time
import asyncio
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, gemini_manager
from services import (
    pdf_processor, generate_mcqs_from_image, mcqs_to_csv,
    format_progress, LargePDFHandler, AsyncPDFExporter,
    SHEET_TEMPLATES, parse_csv_to_mcqs
)

# ============================================================
# /pdfm HANDLER
# ============================================================
async def pdfm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate MCQs from PDF"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF ফাইলে reply করে `/pdfm` দাও")
        return
    
    doc = update.message.reply_to_message.document
    if not doc.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("❌ শুধু PDF ফাইল সাপোর্টেড!")
        return
    
    # Parse args: -p 1-10 -c @channel -m "Title" [15]
    args = context.args if context.args else []
    page_range = None
    channel_id = None
    title = "MCQ Practice"
    mcq_count = None  # Highest possible if not set
    
    i = 0
    while i < len(args):
        if args[i] == '-p' and i + 1 < len(args):
            page_range = args[i + 1]
            i += 2
        elif args[i] == '-c' and i + 1 < len(args):
            channel_id = args[i + 1]
            i += 2
        elif args[i] == '-m' and i + 1 < len(args):
            title = args[i + 1]
            i += 2
        else:
            # Check for [number] format
            match = re.match(r'\[(\d+)\]', args[i])
            if match:
                mcq_count = int(match.group(1))
            i += 1
    
    # Save context
    context.user_data['pdf_title'] = title
    context.user_data['pdf_channel'] = channel_id
    context.user_data['pdf_mcq_count'] = mcq_count
    context.user_data['pdf_page_range'] = page_range
    context.user_data['pdf_doc'] = doc.file_id
    
    # Show Mood selection
    buttons = [
        [InlineKeyboardButton("📸 Image Mood", callback_data="pdfm_mood_image")],
        [InlineKeyboardButton("📝 Topic Name Mood", callback_data="pdfm_mood_topic")],
        [InlineKeyboardButton("❌ Cancel", callback_data="pdfm_cancel")]
    ]
    
    await update.message.reply_text(
        f"""📄 *PDF MCQ Generation*

📁 File: `{doc.file_name}`
📄 Pages: {page_range or '1-10 (default)'}
📝 Title: {title}
🎯 MCQ/Page: {mcq_count or 'Highest Possible'}

*Select Mood:*""",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /qbm HANDLER
# ============================================================
async def qbm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract existing MCQs from PDF (no new generation)"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF ফাইলে reply করে `/qbm` দাও")
        return
    
    doc = update.message.reply_to_message.document
    if not doc.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("❌ শুধু PDF ফাইল সাপোর্টেড!")
        return
    
    # Parse args (same as /pdfm)
    args = context.args if context.args else []
    page_range = None
    channel_id = None
    title = "MCQ Extract"
    mcq_count = None
    
    i = 0
    while i < len(args):
        if args[i] == '-p' and i + 1 < len(args):
            page_range = args[i + 1]
            i += 2
        elif args[i] == '-c' and i + 1 < len(args):
            channel_id = args[i + 1]
            i += 2
        elif args[i] == '-m' and i + 1 < len(args):
            title = args[i + 1]
            i += 2
        else:
            match = re.match(r'\[(\d+)\]', args[i])
            if match:
                mcq_count = int(match.group(1))
            i += 1
    
    context.user_data['qbm_title'] = title
    context.user_data['qbm_channel'] = channel_id
    context.user_data['qbm_mcq_count'] = mcq_count
    context.user_data['qbm_page_range'] = page_range
    context.user_data['qbm_doc'] = doc.file_id
    
    buttons = [
        [InlineKeyboardButton("📸 Image Mood", callback_data="qbm_mood_image")],
        [InlineKeyboardButton("📝 Topic Name Mood", callback_data="qbm_mood_topic")],
        [InlineKeyboardButton("❌ Cancel", callback_data="qbm_cancel")]
    ]
    
    await update.message.reply_text(
        f"""📋 *PDF MCQ Extraction*

📁 File: `{doc.file_name}`
📄 Pages: {page_range or '1-10 (default)'}
📝 Title: {title}

*শুধু Existing MCQ Extract হবে, নতুন বানাবে না।*

*Select Mood:*""",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# PDF PROCESSING CORE
# ============================================================
async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                      is_qbm: bool = False, mood: str = 'topic'):
    """Core PDF processing pipeline"""
    query = update.callback_query
    
    prefix = 'qbm' if is_qbm else 'pdfm'
    title = context.user_data.get(f'{prefix}_title', 'MCQ')
    channel_id = context.user_data.get(f'{prefix}_channel')
    mcq_count = context.user_data.get(f'{prefix}_mcq_count')
    page_range_str = context.user_data.get(f'{prefix}_page_range')
    doc_id = context.user_data.get(f'{prefix}_doc')
    
    if not doc_id:
        await query.edit_message_text("❌ PDF ডকুমেন্ট পাওয়া যায়নি!")
        return
    
    # Parse page range
    start_page, end_page = 1, 10
    if page_range_str:
        try:
            if '-' in page_range_str:
                parts = page_range_str.split('-')
                start_page = int(parts[0])
                end_page = int(parts[1])
            else:
                start_page = end_page = int(page_range_str)
        except:
            pass
    
    # Download PDF
    progress_msg = await query.message.reply_text("⏳ PDF ডাউনলোড হচ্ছে...")
    
    try:
        file = await context.bot.get_file(doc_id)
        pdf_bytes = await file.download_as_bytearray()
        if isinstance(pdf_bytes, bytearray):
            pdf_bytes = bytes(pdf_bytes)
    except Exception as e:
        # Try Pyrogram for large files
        try:
            await progress_msg.edit_text("📥 Large PDF — Pyrogram দিয়ে ডাউনলোড...")
            chat_id = update.effective_chat.id
            msg_id = update.message.message_id
            path = await LargePDFHandler.download_large_file(chat_id, msg_id - 1)
            if path:
                with open(path, 'rb') as f:
                    pdf_bytes = f.read()
                os.remove(path)
            else:
                raise e
        except:
            await progress_msg.edit_text(f"❌ PDF ডাউনলোড ব্যর্থ!\n{str(e)[:100]}")
            return
    
    # Save temp PDF
    pdf_path = f"data/temp/pdf_{int(time.time())}.pdf"
    with open(pdf_path, 'wb') as f:
        f.write(pdf_bytes)
    
    # Get total pages
    total_pages = pdf_processor.get_page_count(pdf_path)
    end_page = min(end_page, total_pages)
    
    await progress_msg.edit_text(f"📄 PDF → ইমেজে কনভার্ট হচ্ছে...\n📊 Pages: {start_page}-{end_page}/{total_pages}")
    
    # Convert to images
    images = pdf_processor.pdf_to_images(pdf_path, start_page, end_page)
    
    # Get active prompts
    if is_qbm:
        active_prompts = ["EXTRACT only existing MCQs from the image. Do NOT generate new questions. Output the exact questions, options, and answers as they appear in the image."]
    else:
        prompt_rows = await db.fetchall('SELECT content FROM prompts WHERE is_active = 1')
        if not prompt_rows:
            await progress_msg.edit_text("❌ কোনো Active Prompt নেই!")
            return
        active_prompts = [row[0] for row in prompt_rows]
    
    # Process each page
    all_mcqs = []
    page_links = {}  # For summary
    
    for idx, (page_num, img_bytes) in enumerate(images):
        pg_progress = format_progress(idx + 1, len(images), f"📄 পৃষ্ঠা {page_num}/{end_page}")
        await progress_msg.edit_text(f"{pg_progress}\n✅ MCQ পাওয়া: {len(all_mcqs)}")
        
        try:
            if mcq_count:
                page_mcqs = await generate_mcqs_from_image(img_bytes, active_prompts, mcq_count)
            else:
                # Highest possible without garbage
                page_mcqs = await generate_mcqs_from_image(img_bytes, active_prompts, 15)
            
            all_mcqs.extend(page_mcqs)
            
            # Store page-wise for summary
            if page_mcqs:
                page_links[page_num] = len(page_mcqs)
        except Exception as e:
            await progress_msg.edit_text(f"⚠️ পৃষ্ঠা {page_num} প্রসেসিং ব্যর্থ! পরবর্তীতে যাচ্ছি...")
            continue
    
    # Cleanup temp file
    try:
        os.remove(pdf_path)
    except:
        pass
    
    if not all_mcqs:
        await progress_msg.edit_text("❌ কোনো MCQ পাওয়া যায়নি!")
        return
    
    # Create CSV
    csv_bytes = mcqs_to_csv(all_mcqs)
    
    # Create Practice Sheet (Format-01)
    await progress_msg.edit_text("📊 CSV + Practice Sheet তৈরি হচ্ছে...")
    
    from jinja2 import Template
    template = Template(SHEET_TEMPLATES['format_01'])
    html = template.render(title=title, mcqs=all_mcqs)
    
    sheet_path = f"data/temp/sheet_{int(time.time())}.pdf"
    await AsyncPDFExporter.html_to_pdf(html, sheet_path)
    
    # Get thumbnail
    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
    thumb = thumb_row[0] if thumb_row else None
    
    # Send CSV
    await progress_msg.delete()
    await update.effective_message.reply_document(
        document=csv_bytes,
        filename=f"{title}.csv",
        caption=f"✅ *{len(all_mcqs)}টি MCQ*\n📄 {len(images)} পৃষ্ঠা থেকে",
        parse_mode=ParseMode.MARKDOWN,
        thumbnail=thumb
    )
    
    # Send Practice Sheet
    if os.path.exists(sheet_path):
        with open(sheet_path, 'rb') as f:
            await update.effective_message.reply_document(
                document=f.read(),
                filename=f"{title}_Practice_Sheet.pdf",
                thumbnail=thumb
            )
        os.remove(sheet_path)
    
    # Save for later use
    context.user_data['last_csv'] = csv_bytes
    context.user_data['last_mcqs'] = all_mcqs
    context.user_data['last_topic'] = title
    
    # If channel specified, ask for confirm or show channel list
    if channel_id:
        buttons = [
            [InlineKeyboardButton(f"📢 Send to {channel_id}", callback_data=f"pdf_send_{channel_id}")],
            [InlineKeyboardButton("📋 MCQ List View", callback_data="pdf_show_list")],
        ]
        await update.effective_message.reply_text(
            f"✅ *{len(all_mcqs)}টি MCQ প্রস্তুত!*\n\nকী করতে চাও?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        # Show channel list
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        buttons = []
        for ch_id, ch_name in channels:
            buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"pdf_send_{ch_id}")])
        buttons.append([InlineKeyboardButton("📋 MCQ List View", callback_data="pdf_show_list")])
        
        await update.effective_message.reply_text(
            f"✅ *{len(all_mcqs)}টি MCQ প্রস্তুত!*\n\nকোন চ্যানেলে পাঠাবে?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    # Store for channel sending
    context.user_data['send_mcqs'] = all_mcqs
    context.user_data['send_topic'] = title
    context.user_data['send_mood'] = mood
    context.user_data['page_links'] = page_links


# ============================================================
# SEND POLLS TO CHANNEL
# ============================================================
async def send_polls_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                channel_id: str, mcqs: list, topic: str, mood: str = 'topic',
                                images: list = None):
    """Send polls to channel with pre/ending messages"""
    query = update.callback_query
    
    total = len(mcqs)
    
    # Get exp settings
    exp_row = await db.fetchone('SELECT mode, custom_text, tag_name FROM exp_settings WHERE id = 1')
    exp_mode = exp_row[0] if exp_row else 'auto'
    custom_exp = exp_row[1] if exp_row else ''
    tag_name = exp_row[2] if exp_row else ''
    
    # Get tag settings
    tags = await db.fetchall('SELECT tag_name, position, is_active FROM tag_settings WHERE is_active = 1')
    
    # Send pre-message
    if mood == 'image' and images:
        # Image Mood - send images first
        image_msgs = []
        for img_bytes in images:
            img_msg = await context.bot.send_photo(
                chat_id=channel_id,
                photo=io.BytesIO(img_bytes)
            )
            image_msgs.append(img_msg.message_id)
        
        # Send header
        header = f"""🌟ATLAS Master Poll Solve

📋মোট পোল: {total}

⁉️তোমার স্কোর কত?
👉(?/{total})

✅কমেন্টে জানিয়ে দাও"""
        
        header_msg = await context.bot.send_message(chat_id=channel_id, text=header)
        reply_to = header_msg.message_id
    else:
        # Topic Name Mood
        pre_text = f"""🌟Important Poll Solve By ATLAS
🔥Topic Name: "{topic}"{" " if topic else ""}

✅প্রশ্ন সংখ্যা: {total}"""
        
        pre_msg = await context.bot.send_message(chat_id=channel_id, text=pre_text)
        reply_to = pre_msg.message_id
    
    # Send polls
    first_poll_link = None
    sent_count = 0
    
    for idx, mcq in enumerate(mcqs):
        # Check pause
        while context.user_data.get('paused', False):
            await asyncio.sleep(1)
        
        # Build question with tags
        q_text = mcq.get('question', '?')
        for tag in tags:
            tag_name_val = tag[0]
            position = tag[1]
            if position == 'tag1':
                q_text = f"{tag_name_val}\n\n{q_text}"
            elif position == 'tag2':
                q_text = f"{q_text}\n\n{tag_name_val}"
            elif position == 'tag3':
                q_text = f"{q_text} {tag_name_val}"
            elif position == 'tag4':
                q_text = f"{tag_name_val}\n{q_text}"
        
        # Build explanation
        if exp_mode == 'custom' and custom_exp:
            explanation = custom_exp
        elif exp_mode == 'auto':
            explanation = mcq.get('explanation', '')
        else:
            explanation = mcq.get('explanation', '')
        
        if tag_name and exp_mode != 'custom':
            explanation = f"{explanation}\n{tag_name}" if explanation else tag_name
        
        explanation = explanation[:200] if explanation else None
        
        # Options
        opts = mcq.get('options', {})
        option_list = [opts.get('A', ''), opts.get('B', ''), opts.get('C', ''), opts.get('D', '')]
        
        ans_str = mcq.get('answer', '1')
        ans_map = {'1': 0, '2': 1, '3': 2, '4': 3, 'A': 0, 'B': 1, 'C': 2, 'D': 3}
        correct_idx = ans_map.get(ans_str, 0)
        
        try:
            poll_msg = await context.bot.send_poll(
                chat_id=channel_id,
                question=q_text[:300],
                options=option_list,
                type='quiz',
                correct_option_id=correct_idx,
                explanation=explanation,
                is_anonymous=False,
                reply_to_message_id=reply_to
            )
            
            if idx == 0:
                first_poll_link = f"https://t.me/c/{str(channel_id).replace('-100', '')}/{poll_msg.message_id}"
            
            sent_count += 1
        except Exception as e:
            continue
        
        await asyncio.sleep(2)
    
    # Send ending message
    if first_poll_link:
        ending = f"""🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!
👉এটলাস আয়োজিত "{topic}" পোল সলভে অংশগ্রহণ করার জন্য। 😊

📊 মোট পোল: {sent_count}

⁉️তোমার স্কোর কত? 🤔
( ? / {sent_count} )

নিচে লিখো! 👇

✅পোল যেখান থেকে শুরু হয়েছে:
{first_poll_link}"""
    else:
        ending = f"""🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!

📊 মোট পোল: {sent_count}

⁉️তোমার স্কোর কত? 🤔"""
    
    await context.bot.send_message(chat_id=channel_id, text=ending, disable_web_page_preview=True)
    
    # Send page-wise summary if multiple pages
    page_links = context.user_data.get('page_links', {})
    if len(page_links) > 1:
        summary = "🟥পেইজভিত্তিক Important Poll Solve By ATLAS\n\n✅নিচে সিরিয়ালী সাজিয়ে দেওয়া হলো:\n\n"
        for pg, count in page_links.items():
            summary += f"📍Page-{pg}: ({count}টি প্রশ্ন)\n"
        
        await context.bot.send_message(chat_id=channel_id, text=summary)
    
    await query.edit_message_text(f"✅ {sent_count}টি পোল পাঠানো সম্পন্ন!\n📢 {channel_id}")


# ============================================================
# PDF CALLBACK HANDLER
# ============================================================
async def handle_pdf_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Mood selection
    if data.startswith('pdfm_mood_') or data.startswith('qbm_mood_'):
        is_qbm = data.startswith('qbm')
        mood = data.split('_')[-1]
        
        if mood == 'cancel':
            await query.edit_message_text("❌ বাতিল করা হয়েছে!")
            return
        
        await query.edit_message_text(f"⏳ PDF প্রসেসিং শুরু...\n📝 Mood: {'Image' if mood == 'image' else 'Topic Name'}")
        await process_pdf(update, context, is_qbm, mood)
    
    elif data == 'pdfm_cancel' or data == 'qbm_cancel':
        await query.edit_message_text("❌ বাতিল করা হয়েছে!")
    
    # Send to channel
    elif data.startswith('pdf_send_'):
        channel_id = data.replace('pdf_send_', '')
        mcqs = context.user_data.get('send_mcqs', [])
        topic = context.user_data.get('send_topic', 'MCQ')
        mood = context.user_data.get('send_mood', 'topic')
        
        if not mcqs:
            await query.edit_message_text("❌ MCQ সেশন শেষ!")
            return
        
        await query.edit_message_text(f"📤 {len(mcqs)}টি পোল পাঠানো শুরু...")
        await send_polls_to_channel(update, context, channel_id, mcqs, topic, mood)
    
    elif data == 'pdf_show_list':
        mcqs = context.user_data.get('send_mcqs', [])
        if mcqs:
            from core_handlers import show_mcq_list
            await show_mcq_list(update, context, mcqs, context.user_data.get('send_topic', ''), 0)
