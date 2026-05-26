#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tools Handlers - /split, /convert, /thumb, /ping, /error, /collect, /sheet"""

import asyncio
import time
import os
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import db, Config

# Bot start time
BOT_START_TIME = datetime.now()


async def generate_pdf_from_mcqs(mcqs, title, format_type):
    """Generate PDF from MCQs"""
    from pdf_handler import generate_pdf, TEMPLATE_FORMAT1, TEMPLATE_FORMAT2, TEMPLATE_FORMAT3, TEMPLATE_FORMAT4, is_short_option
    from jinja2 import Template
    
    # Add is_short flag
    for mcq in mcqs:
        mcq['is_short'] = is_short_option(mcq['options'])
    
    # Select template
    templates = {
        'format1': TEMPLATE_FORMAT1,
        'format2': TEMPLATE_FORMAT2,
        'format3': TEMPLATE_FORMAT3,
        'format4': TEMPLATE_FORMAT4
    }
    
    if format_type == 'all':
        # Generate all 4 formats
        pdfs = []
        for fmt, template_str in templates.items():
            template = Template(template_str)
            html = template.render(title=title, mcqs=mcqs)
            pdf_path = f"/tmp/sheet_{fmt}_{int(time.time())}.pdf"
            await generate_pdf(html, pdf_path)
            pdfs.append((pdf_path, f"{title} - {fmt.upper()}.pdf"))
        return pdfs
    else:
        # Single format
        template = Template(templates[format_type])
        html = template.render(title=title, mcqs=mcqs)
        pdf_path = f"/tmp/sheet_{format_type}_{int(time.time())}.pdf"
        await generate_pdf(html, pdf_path)
        return [(pdf_path, f"{title}.pdf")]

async def split_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/split - Split files"""
    await update.message.reply_text("🔧 Split tool coming soon!")


async def convert_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/convert - Convert files"""
    await update.message.reply_text("🔧 Convert tool coming soon!")


async def thumb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/thumb - Thumbnail management"""
    if context.args and context.args[0] == 'remove':
        await db.execute('DELETE FROM thumbnail WHERE id = 1')
        await update.message.reply_text("✅ Thumbnail removed")
        return
    
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ Image-এ reply করে /thumb দাও")
        return
    
    # Get photo
    photo = update.message.reply_to_message.photo[-1]
    file_id = photo.file_id
    
    # Download and save
    file = await photo.get_file()
    thumb_path = f"data/thumbnails/thumb_{update.message.from_user.id}.jpg"
    await file.download_to_drive(thumb_path)
    
    # Save to DB
    await db.execute(
        'INSERT OR REPLACE INTO thumbnail (id, file_id, file_path) VALUES (1, ?, ?)',
        (file_id, thumb_path)
    )
    
    await update.message.reply_text("✅ Thumbnail set করা হয়েছে!")


async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ping - Bot status"""
    uptime = datetime.now() - BOT_START_TIME
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    # Get RAM usage (approximate)
    try:
        import psutil
        process = psutil.Process()
        ram_mb = process.memory_info().rss / 1024 / 1024
        ram_text = f"🖥️ RAM: {ram_mb:.0f} MB / 2 GB"
    except:
        ram_text = "🖥️ RAM: N/A"
    
    await update.message.reply_text(f"""🟢 Bot চালু আছে!
⏱️ Uptime: {days}d {hours}h {minutes}m {seconds}s
📅 Started: {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
{ram_text}""")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/error - System health check"""
    report = "🔍 Bot Health Check:\n\n"
    
    # Check Gemini
    try:
        from config import gemini_manager
        healthy_keys = sum(1 for v in gemini_manager.keys.values() if v['healthy'])
        total_keys = len(gemini_manager.keys)
        report += f"✅ Gemini API: কাজ করছে ({healthy_keys}/{total_keys} keys active)\n"
    except Exception as e:
        report += f"❌ Gemini API: সমস্যা - {str(e)}\n"
    
    # Check ImgBB
    try:
        from config import imgbb_manager
        total_imgbb = len(imgbb_manager.keys)
        report += f"✅ ImgBB API: কাজ করছে ({total_imgbb} keys)\n"
    except Exception as e:
        report += f"❌ ImgBB API: সমস্যা - {str(e)}\n"
    
    # Check Database
    try:
        await db.fetchone('SELECT 1')
        report += "✅ Database: ঠিক আছে\n"
    except Exception as e:
        report += f"❌ Database: সমস্যা - {str(e)}\n"
    
    # Check Chromium
    try:
        from playwright.async_api import async_playwright
        report += "✅ Chromium: চালু\n"
    except Exception as e:
        report += f"❌ Chromium: সমস্যা - {str(e)}\n"
    
    # Check channels
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    if channels:
        report += f"\n📢 Channels: {len(channels)} connected\n"
    else:
        report += "\n⚠️ কোন channel add করা নেই\n"
    
    await update.message.reply_text(report)


async def sheet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sheet - Generate PDF from CSV"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ CSV file-এ reply করে /sheet দাও")
        return
    
    # Download CSV
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8')
    
    # Parse CSV
    import csv
    import io
    mcqs = []
    reader = csv.DictReader(io.StringIO(content_str))
    for row in reader:
        mcqs.append({
            'question': row.get('question', ''),
            'options': {
                'A': row.get('A', ''),
                'B': row.get('B', ''),
                'C': row.get('C', ''),
                'D': row.get('D', '')
            },
            'answer': row.get('answer', ''),
            'explanation': row.get('explanation', '')
        })
    
    if not mcqs:
        await update.message.reply_text("❌ Valid CSV পাওয়া যায়নি")
        return
    
    # Store in context
    context.user_data['sheet_mcqs'] = mcqs
    
    # Show format buttons
    buttons = [
        [InlineKeyboardButton("Format-01: প্রশ্ন + উত্তর নিচে", callback_data="sheet_format1")],
        [InlineKeyboardButton("Format-02: প্রশ্ন + উত্তর আলাদা পৃষ্ঠায়", callback_data="sheet_format2")],
        [InlineKeyboardButton("Format-03: শুধু প্রশ্ন", callback_data="sheet_format3")],
        [InlineKeyboardButton("Format-04: শুধু উত্তর কী", callback_data="sheet_format4")],
        [InlineKeyboardButton("📚 All Formats", callback_data="sheet_all")]
    ]
    
    await update.message.reply_text(
        f"✅ {len(mcqs)}টি MCQ পাওয়া গেছে!\nPDF format select করো:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def collect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/collect - Start CSV collection"""
    await update.message.reply_text("""📥 CSV Collection শুরু করো!

এখন CSV/JSON files পাঠাও।
শেষ হলে:
/done - Merge করে পাঠাবে
/status - কতটা collected
/cancel - Cancel করবে""")
    
    context.user_data['collecting'] = True
    context.user_data['collected_files'] = []


async def handle_collection_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document during collection"""
    if not context.user_data.get('collecting'):
        return False
    
    doc = update.message.document
    if not (doc.file_name.endswith('.csv') or doc.file_name.endswith('.json')):
        return False
    
    # Download
    file = await doc.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8')
    
    # Store
    context.user_data['collected_files'].append(content_str)
    
    count = len(context.user_data['collected_files'])
    await update.message.reply_text(f"✅ Collected: {count} files")
    
    return True


async def done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/done - Finish CSV collection"""
    if not context.user_data.get('collecting'):
        await update.message.reply_text("❌ কোন collection চলছে না")
        return
    
    files = context.user_data.get('collected_files', [])
    if not files:
        await update.message.reply_text("❌ কোন file collect করা হয়নি")
        return
    
    # Merge all CSVs
    import csv
    import io
    all_mcqs = []
    for content in files:
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            all_mcqs.append(row)
    
    # Create merged CSV
    output = io.StringIO()
    if all_mcqs:
        writer = csv.DictWriter(output, fieldnames=all_mcqs[0].keys())
        writer.writeheader()
        writer.writerows(all_mcqs)
    
    # Send
    await update.message.reply_document(
        document=output.getvalue().encode('utf-8'),
        filename='merged_collection.csv',
        caption=f"✅ {len(all_mcqs)}টি MCQ merged করা হয়েছে!"
    )
    
    # Clear
    context.user_data['collecting'] = False
    context.user_data['collected_files'] = []


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status - Collection status"""
    if not context.user_data.get('collecting'):
        await update.message.reply_text("❌ কোন collection চলছে না")
        return
    
    count = len(context.user_data.get('collected_files', []))
    await update.message.reply_text(f"📊 Collected: {count} files")


async def cancel_collection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel - Cancel collection"""
    context.user_data['collecting'] = False
    context.user_data['collected_files'] = []
    await update.message.reply_text("❌ Collection cancelled")


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pause - Pause ongoing poll sending"""
    context.user_data['paused'] = True
    await update.message.reply_text("⏸️ Poll sending paused")


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resume - Resume poll sending"""
    context.user_data['paused'] = False
    await update.message.reply_text("▶️ Poll sending resumed")


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart - Restart bot (admin only)"""
    if update.message.from_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Owner only!")
        return
    
    await update.message.reply_text("🔄 Bot restarting...")
    import sys
    os.execv(sys.executable, ['python'] + sys.argv)


async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logs - Get last 500 lines of logs"""
    if update.message.from_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Owner only!")
        return
    
    # Get systemd logs
    try:
        import subprocess
        result = subprocess.run(
            ['journalctl', '-u', 'atlas-bot', '-n', '500'],
            capture_output=True,
            text=True
        )
        logs = result.stdout
        
        # Send as file
        await update.message.reply_document(
            document=logs.encode('utf-8'),
            filename='atlas_bot_logs.txt',
            caption="📋 Last 500 lines"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def handle_tools_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tools callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith('sheet_'):
        # Get MCQs from context
        mcqs = context.user_data.get('sheet_mcqs', [])
        if not mcqs:
            await query.edit_message_text("❌ Session expired")
            return
        
        # Ask for title
        await query.edit_message_text("📝 PDF এর title লেখো:")
        context.user_data['sheet_format'] = data.replace('sheet_', '')
        context.user_data['waiting_for_title'] = True
        return
    
    await query.edit_message_text("✅ Processed")
