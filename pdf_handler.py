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
    context.chat_data['pdf_title'] = title
    context.chat_data['pdf_channel'] = channel_id
    context.chat_data['pdf_mcq_count'] = mcq_count
    context.chat_data['pdf_page_range'] = page_range
    context.chat_data['pdf_doc'] = doc.file_id
    
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
        parse_mode=None,
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
    
    context.chat_data['qbm_title'] = title
    context.chat_data['qbm_channel'] = channel_id
    context.chat_data['qbm_mcq_count'] = mcq_count
    context.chat_data['qbm_page_range'] = page_range
    context.chat_data['qbm_doc'] = doc.file_id
    
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
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# PDF PROCESSING CORE
# ============================================================

async def update_dashboard(msg, pdf_name, total_pages, current_page, mcq_count, start_time):
    """Update live progress dashboard"""
    elapsed = int(time.time() - start_time)
    pct = int((current_page / total_pages) * 100) if total_pages > 0 else 0
    bar_len = 20
    filled = int(bar_len * current_page / total_pages) if total_pages > 0 else 0
    bar = '█' * filled + '░' * (bar_len - filled)
    
    if current_page > 0:
        rate = current_page / elapsed if elapsed > 0 else 0
        remaining = (total_pages - current_page) / rate if rate > 0 else 0
        eta_text = f"{int(remaining // 60)}m {int(remaining % 60)}s"
        from datetime import datetime, timedelta
        done_time = datetime.now() + timedelta(seconds=remaining)
        done_text = done_time.strftime('%I:%M %p')
    else:
        eta_text = "calculating..."
        done_text = "..."

    dashboard = f"""╔══════════════════════════════════╗
║     📊 ATLAS PDF PROCESSOR      ║
╠══════════════════════════════════╣
║ 📁 {pdf_name[:30]:<30} ║
║ 📄 Total Pages: {total_pages:<16} ║
║ ⏳ Processed: {current_page}/{total_pages} ({pct}%){'':<3} ║
║ [{bar}] {pct}%{'':<5} ║
║                                 ║
║ 📝 MCQ Found: {mcq_count:<18} ║
║ ⏱️ Elapsed: {elapsed}s{'':<19} ║
║ ⏳ Remaining: {eta_text:<16} ║
║ 🕐 Est. Done: {done_text:<16} ║
║                                 ║
║ 🔄 Processing page {current_page}...{'':<7} ║
╚══════════════════════════════════╝"""
    
    try:
        await msg.edit_text(f"```{dashboard}```", parse_mode='Markdown')
    except:
        await msg.edit_text(dashboard)


async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                      is_qbm: bool = False, mood: str = 'topic'):
    """Core PDF processing pipeline"""
    query = update.callback_query
    
    prefix = 'qbm' if is_qbm else 'pdf'
    title = context.chat_data.get('pdf_title', 'MCQ')
    channel_id = context.chat_data.get('pdf_channel')
    mcq_count = context.chat_data.get('pdf_mcq_count')
    page_range_str = context.chat_data.get(f"{prefix}_page_range") or context.chat_data.get("pdf_page_range")
    doc_id = context.chat_data.get(f"{prefix}_doc") or context.chat_data.get("pdf_doc")
    
    if not doc_id:
        await query.edit_message_text(f"❌ PDF doc not found! Keys: {list(context.chat_data.keys())}")
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
        file_size = file.file_size or 0
        
        if file_size > 0:
            import time
            dl_start = time.time()
            await progress_msg.edit_text(f"📥 PDF Downloading...\n📊 Size: {file_size/1024/1024:.1f} MB\n⏳ 0%")
            
        pdf_bytes = await file.download_as_bytearray()
        
        if file_size > 0:
            dl_time = time.time() - dl_start
            await progress_msg.edit_text(f"✅ PDF Downloaded!\n📊 Size: {file_size/1024/1024:.1f} MB\n⏱️ Time: {dl_time:.1f}s")
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
        active_prompts = ["""EXTRACT ALL existing MCQs from this image.
- Find every question with options (A/B/C/D)
- Detect correct answers from markings (circle/tick/answer key)
- Output EXACT question text, options, and answer
- DO NOT create new questions
- Output ONLY valid JSON array"""]
    else:
        prompt_rows = await db.fetchall('SELECT content FROM prompts WHERE is_active = 1')
        if not prompt_rows:
            await progress_msg.edit_text("❌ কোনো Active Prompt নেই!")
            return
        active_prompts = [row[0] for row in prompt_rows]
    
    # Process each page
    all_mcqs = []
    page_links = {}  # For summary
    
    dashboard_msg = await update.effective_message.reply_text("⏳ Starting...")
    start_time = time.time()
    for idx, (page_num, img_bytes) in enumerate(images):
        pg_progress = format_progress(idx + 1, len(images), f"📄 পৃষ্ঠা {page_num}/{end_page}")
        await progress_msg.edit_text(f"{pg_progress}\n✅ MCQ পাওয়া: {len(all_mcqs)}")
        await update_dashboard(dashboard_msg, title, len(images), idx + 1, len(all_mcqs), start_time)
        
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
        parse_mode=None,
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
    
    # Send polls to channel if specified
    if channel_id and mood:
        await send_polls_to_channel(update, context, channel_id, all_mcqs, title, mood, [img for _, img in images])
        return
    
    # If channel specified, ask for confirm or show channel list
    if channel_id:
        buttons = [
            [InlineKeyboardButton(f"📢 Send to {channel_id}", callback_data=f"pdf_send_{channel_id}")],
            [InlineKeyboardButton("📋 MCQ List View", callback_data="pdf_show_list")],
        ]
        await update.effective_message.reply_text(
            f"✅ *{len(all_mcqs)}টি MCQ প্রস্তুত!*\n\nকী করতে চাও?",
            parse_mode=None,
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
            parse_mode=None,
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
    from csv_poll_handler import send_single_poll, get_pre_message, get_ending_message, get_message_link
    query = update.callback_query if hasattr(update, 'callback_query') else None
    bot = context.bot
    total = len(mcqs)
    
    if mood == 'image' and images:
        # Image Mood - send per-image polls
        per_image = max(1, total // len(images)) if images else total
        mcq_idx = 0
        page_links = {}
        
        for i, img_data in enumerate(images):
            page_num = i + 1
            img_bytes = img_data[1] if isinstance(img_data, tuple) else img_data
            
            # Send image
            img_msg = await bot.send_photo(chat_id=channel_id, photo=io.BytesIO(img_bytes if isinstance(img_bytes, bytes) else img_bytes))
            
            # Send polls for this page
            page_mcqs = mcqs[mcq_idx:mcq_idx + per_image]
            first_poll_id = None
            sent = 0
            
            for mcq in page_mcqs:
                poll_id, success = await send_single_poll(bot, channel_id, mcq, img_msg.message_id)
                if success and first_poll_id is None:
                    first_poll_id = poll_id
                if success:
                    sent += 1
                await asyncio.sleep(2)
            
            mcq_idx += per_image
            
            # Ending for this page
            first_link = await get_message_link(bot, channel_id, first_poll_id) if first_poll_id else ""
            page_topic = f"{topic} (Page-{page_num:02d})"
            ending = get_ending_message(page_topic, sent, first_link)
            await bot.send_message(chat_id=channel_id, text=ending, reply_to_message_id=img_msg.message_id, disable_web_page_preview=True)
            page_links[page_num] = first_link
        
        # Master summary
        if len(page_links) > 1:
            summary = f"🟥পেইজভিত্তিক Important Poll Solve By ATLAS\n🌟Topic: {topic}\n\n✅নিচে সিরিয়ালী সাজিয়ে দেওয়া হলো:\n\n"
            for pg, link in page_links.items():
                summary += f"📍Page-{pg:02d}:\n{link}\n\n"
            await bot.send_message(chat_id=channel_id, text=summary, disable_web_page_preview=True)
    
    else:
        # Topic Name Mood - single pre-message with polls
        pre_text = get_pre_message(topic, total)
        pre_msg = await bot.send_message(chat_id=channel_id, text=pre_text)
        
        first_poll_id = None
        sent = 0
        
        for mcq in mcqs:
            poll_id, success = await send_single_poll(bot, channel_id, mcq, pre_msg.message_id)
            if success and first_poll_id is None:
                first_poll_id = poll_id
            if success:
                sent += 1
            await asyncio.sleep(2)
        
        first_link = await get_message_link(bot, channel_id, first_poll_id) if first_poll_id else ""
        ending = get_ending_message(topic, sent, first_link)
        await bot.send_message(chat_id=channel_id, text=ending, disable_web_page_preview=True)
    
    if query:
        await query.message.reply_text(f"✅ {total}টি পোল পাঠানো সম্পন্ন!")

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
    
    elif data.startswith('pdf_send_'):
        channel_id = data.replace('pdf_send_', '')
        mcqs = context.user_data.get('send_mcqs', [])
        topic = context.user_data.get('send_topic', 'MCQ')
        mood = context.user_data.get('send_mood', 'topic')
        
        if mcqs:
            await query.edit_message_text(f"📤 {len(mcqs)}টি MCQ চ্যানেলে পাঠানো হচ্ছে...")
            await send_polls_to_channel(update, context, channel_id, mcqs, topic, mood)
        else:
            await query.answer("❌ MCQ সেশন শেষ!", show_alert=True)

    elif data == 'pdf_show_list':
        mcqs = context.user_data.get('send_mcqs', [])
        if mcqs:
            from core_handlers import show_mcq_list
            await show_mcq_list(update, context, mcqs, context.user_data.get('send_topic', ''), 0)
