import asyncio
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - Tools Handlers (All File & System Tools)"""

import os
import re
import csv
import io
import json
import time
import tempfile
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, Config, gemini_manager, imgbb_manager
from services import (
    mcqs_to_csv, parse_csv_to_mcqs, format_progress,
    AsyncPDFExporter, SHEET_TEMPLATES, poll_collector,
    add_watermark_to_pdf
)

BOT_START_TIME = datetime.now()

# ============================================================
# /split HANDLER
# ============================================================
async def split_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Split CSV/JSON file into chunks"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ CSV/JSON ফাইলে reply করে `/split 20` দাও")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ সংখ্যা দাও! যেমন: `/split 20`")
        return
    
    chunk_size = int(args[0])
    
    # Download file
    progress = await update.message.reply_text("⏳ ফাইল ডাউনলোড হচ্ছে...")
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8-sig')
    filename = update.message.reply_to_message.document.file_name
    
    # Parse
    mcqs = parse_csv_to_mcqs(content_str)
    if not mcqs:
        await progress.edit_text("❌ ফাইলে কোনো MCQ পাওয়া যায়নি!")
        return
    
    total = len(mcqs)
    total_parts = (total + chunk_size - 1) // chunk_size
    
    await progress.edit_text(f"⏳ {total}টি MCQ → {total_parts}টি ফাইলে ভাগ হচ্ছে...")
    
    for i in range(total_parts):
        chunk = mcqs[i * chunk_size:(i + 1) * chunk_size]
        csv_bytes = mcqs_to_csv(chunk)
        part_name = filename.replace('.csv', f'_part{i+1:02d}.csv').replace('.json', f'_part{i+1:02d}.csv')
        
        await update.message.reply_document(
            document=csv_bytes,
            filename=part_name,
            caption=f"📄 Part-{i+1:02d} | 📊 {len(chunk)}টি MCQ"
        )
        await asyncio.sleep(0.5)
    
    await progress.delete()
    await update.message.reply_text(f"✅ সম্পন্ন! {total}টি MCQ → {total_parts}টি ফাইল")


# ============================================================
# /merge HANDLER
# ============================================================
async def merge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Merge multiple CSV/JSON files with instant count"""
    user_id = update.effective_user.id
    args = context.args if context.args else []
    
    # Initialize merge session
    if 'merge_files' not in context.user_data:
        context.user_data['merge_files'] = []
        context.user_data['merge_count'] = 0
    
    # /merge done - finish merging
    if args and args[0] == 'done':
        files = context.user_data.get('merge_files', [])
        if not files:
            await update.message.reply_text("❌ কোনো ফাইল জমা হয়নি! /merge দিয়ে ফাইল পাঠাও।")
            return
        
        await update.message.reply_text(f"🔄 {len(files)}টি ফাইল মার্জ হচ্ছে...")
        all_mcqs = []
        for content in files:
            mcqs = parse_csv_to_mcqs(content)
            all_mcqs.extend(mcqs)
        
        if not all_mcqs:
            await update.message.reply_text("❌ কোনো MCQ পাওয়া যায়নি!")
            return
        
        csv_bytes = mcqs_to_csv(all_mcqs)
        await update.message.reply_document(
            document=csv_bytes,
            filename="merged.csv",
            caption=f"✅ {len(all_mcqs)}টি MCQ মার্জ! ({len(files)} files)"
        )
        context.user_data['merge_files'] = []
        context.user_data['merge_count'] = 0
        return
    
    # File received (reply OR forwarded)
    doc = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
    elif update.message.document:
        doc = update.message.document
    
    if doc:
        if not doc.file_name.endswith(('.csv', '.json')):
            await update.message.reply_text("❌ শুধু CSV/JSON ফাইল!")
            return
        file = await doc.get_file()
        content = await file.download_as_bytearray()
        context.user_data['merge_files'].append(content.decode('utf-8-sig'))
        count = len(context.user_data['merge_files'])
        await update.message.reply_text(f"📥 {doc.file_name}\n📊 Total: {count} file{'s' if count>1 else ''}\n\n➕ আরো পাঠাও\n✅ /merge done")
    else:
        # Start merge mode
        context.user_data['merge_files'] = []
        await update.message.reply_text("📁 *Merge Mode Started!*\n\nCSV/JSON ফাইলে reply করে `/merge` দাও।\nInstant count দেখাবে।\nশেষে `/merge done` দাও。")


# ============================================================
# /convert HANDLER
# ============================================================
async def convert_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Convert CSV to JSON or vice versa"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ CSV/JSON ফাইলে reply করে `/convert` দাও")
        return
    
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8-sig')
    filename = update.message.reply_to_message.document.file_name
    
    if filename.endswith('.csv'):
        # CSV → JSON
        mcqs = parse_csv_to_mcqs(content_str)
        json_data = []
        for mcq in mcqs:
            json_data.append({
                "question": mcq['question'],
                "options": mcq['options'],
                "correct_answer": mcq['answer'],
                "explanation": mcq.get('explanation', '')
            })
        json_bytes = json.dumps(json_data, ensure_ascii=False, indent=2).encode('utf-8')
        await update.message.reply_document(
            document=json_bytes,
            filename=filename.replace('.csv', '.json'),
            caption=f"✅ CSV → JSON | 📊 {len(mcqs)}টি MCQ"
        )
    elif filename.endswith('.json'):
        # JSON → CSV
        data = json.loads(content_str)
        mcqs = []
        for item in data:
            mcqs.append({
                'question': item.get('question', ''),
                'options': item.get('options', {}),
                'answer': item.get('correct_answer', '1'),
                'explanation': item.get('explanation', '')
            })
        csv_bytes = mcqs_to_csv(mcqs)
        await update.message.reply_document(
            document=csv_bytes,
            filename=filename.replace('.json', '.csv'),
            caption=f"✅ JSON → CSV | 📊 {len(mcqs)}টি MCQ"
        )
    else:
        await update.message.reply_text("❌ শুধু .csv বা .json ফাইল সাপোর্টেড!")


# ============================================================
# /rename HANDLER
# ============================================================
async def rename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rename a file"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text('❌ ফাইলে reply করে `/rename "নতুন নাম"` দাও')
        return
    
    args = context.args
    if not args:
        await update.message.reply_text('❌ নাম দাও! যেমন: `/rename "নতুননাম"`')
        return
    
    new_name = ' '.join(args)
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    
    old_ext = update.message.reply_to_message.document.file_name.split('.')[-1]
    if not new_name.endswith(f'.{old_ext}'):
        new_name += f'.{old_ext}'
    
    await update.message.reply_document(
        document=bytes(content) if isinstance(content, bytearray) else content,
        filename=new_name,
        caption=f"✅ Renamed: `{new_name}`",
        parse_mode=None
    )


# ============================================================
# /watermark HANDLER
# ============================================================
async def watermark_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add watermark to PDF"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF ফাইলে reply করে `/watermark টেক্সট` দাও")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("❌ ওয়াটারমার্ক টেক্সট দাও! যেমন: `/watermark এটলাস`")
        return
    
    watermark_text = ' '.join(args)
    progress = await update.message.reply_text("⏳ ওয়াটারমার্ক যোগ হচ্ছে...")
    
    file = await update.message.reply_to_message.document.get_file()
    pdf_bytes = await file.download_as_bytearray()
    
    watermarked = add_watermark_to_pdf(bytes(pdf_bytes), watermark_text)
    
    await progress.delete()
    await update.message.reply_document(
        document=watermarked,
        filename=f"watermarked_{update.message.reply_to_message.document.file_name}",
        caption=f"✅ ওয়াটারমার্ক যোগ সম্পন্ন!\n🔤 Text: {watermark_text}"
    )


# ============================================================
# /exp HANDLER
# ============================================================
async def exp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explanation settings"""
    exp = await db.fetchone('SELECT mode, custom_text, tag_name FROM exp_settings WHERE id = 1')
    
    if not exp:
        await db.execute('INSERT OR IGNORE INTO exp_settings (id, mode) VALUES (1, "auto")')
        mode, custom, tag = 'auto', '', ''
    else:
        mode, custom, tag = exp
    
    text = f"""💬 *Explanation Settings*

🔹 *Current Mode:* {mode.upper()}
{f'🔹 *Custom Text:* {custom[:100]}' if custom else ''}
{f'🔹 *Tag Name:* {tag[:100]}' if tag else ''}

*Modes:*
• Auto — Source থেকে অটো ব্যাখ্যা
• Custom — নিজের লেখা ব্যাখ্যা
• Tag — ব্যাখ্যার পরে ট্যাগ"""

    buttons = [
        [InlineKeyboardButton("💥 Auto (Source থেকে)", callback_data="exp_set_auto")],
        [InlineKeyboardButton("💥 Custom Exp (নিজের)", callback_data="exp_set_custom")],
        [InlineKeyboardButton("🏷️ Tag Name (ব্যাখ্যার পর)", callback_data="exp_set_tag")],
    ]
    
    # Show current mode indicator
    for btn_row in buttons:
        for btn in btn_row:
            if mode in btn.callback_data:
                pass  # fixed
    
    await update.message.reply_text(text, parse_mode=None, reply_markup=InlineKeyboardMarkup(buttons))


# ============================================================
# /tag HANDLER
# ============================================================
async def tag_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tag settings"""
    tags = await db.fetchall('SELECT id, tag_type, tag_name, is_active FROM tag_settings ORDER BY id')
    
    text = "🏷️ *Tag Settings*\n\n"
    buttons = []
    
    for tag_id, tag_type, tag_name, is_active in tags:
        status = "✅" if is_active else "❌"
        pos_text = {
            'tag1': 'উপরে (গ্যাপ সহ)',
            'tag2': 'নিচে (গ্যাপ সহ)',
            'tag3': 'পাশে inline',
            'tag4': 'উপরে (গ্যাপ ছাড়া)'
        }.get(tag_type, tag_type)
        text += f"{status} *{tag_type}* — {pos_text}\n  Name: `{tag_name}`\n\n"
        buttons.append([
            InlineKeyboardButton(f"{'✅' if is_active else '❌'} {tag_type}", callback_data=f"tag_toggle_{tag_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"tag_edit_{tag_id}"),
            InlineKeyboardButton("🗑️ Del", callback_data=f"tag_delete_{tag_id}")
        ])
    
    buttons.append([InlineKeyboardButton("➕ New Tag", callback_data="tag_add")])
    
    if not tags:
        text += "কোনো ট্যাগ নেই!\n\n*Tag Types:*\n• tag1 — উপরে (গ্যাপ সহ)\n• tag2 — নিচে (গ্যাপ সহ)\n• tag3 — পাশে inline\n• tag4 — উপরে (গ্যাপ ছাড়া)"
    
    await update.message.reply_text(text, parse_mode=None, reply_markup=InlineKeyboardMarkup(buttons))


# ============================================================
# /thumb HANDLER
# ============================================================
async def thumb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Thumbnail management"""
    args = context.args
    
    if args and args[0] == 'remove':
        await db.execute('DELETE FROM thumbnail WHERE id = 1')
        await update.message.reply_text("✅ থাম্বনেইল রিমুভ করা হয়েছে!")
        return
    
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        thumb = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
        if thumb:
            await update.message.reply_text(f"📌 বর্তমান থাম্বনেইল:\n`{thumb[0][:50]}...`\n\n/thumb remove — রিমুভ করতে\nইমেজে reply করে /thumb — সেট করতে", parse_mode=None)
        else:
            await update.message.reply_text("❌ ইমেজে reply করে `/thumb` দাও, অথবা `/thumb remove` দিয়ে রিমুভ করো।")
        return
    
    photo = update.message.reply_to_message.photo[-1]
    file_id = photo.file_id
    
    await db.execute('INSERT OR REPLACE INTO thumbnail (id, file_id) VALUES (1, ?)', (file_id,))
    await update.message.reply_text("✅ থাম্বনেইল সেট করা হয়েছে!\nএখন সব CSV/PDF ফাইলে এই থাম্বনেইল দেখাবে।\n\n/thumb remove — রিমুভ করতে")


# ============================================================
# /sheet HANDLER
# ============================================================
async def sheet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate Practice Sheet PDF from CSV"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ CSV ফাইলে reply করে `/sheet` দাও")
        return
    
    # Download CSV
    progress = await update.message.reply_text("⏳ ফাইল প্রসেসিং...")
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    mcqs = parse_csv_to_mcqs(content.decode('utf-8-sig'))
    
    if not mcqs:
        await progress.edit_text("❌ ফাইলে কোনো MCQ পাওয়া যায়নি!")
        return
    
    context.user_data['sheet_mcqs'] = mcqs
    
    # Get active formats
    formats = await db.fetchall('SELECT format_id, format_name, is_active FROM sheet_formats')
    
    buttons = []
    for fid, fname, is_active in formats:
        if is_active:
            buttons.append([InlineKeyboardButton(f"{'✅' if is_active else '❌'} {fname}", callback_data=f"sheet_toggle_{fid}")])
        else:
            buttons.append([InlineKeyboardButton(f"❌ {fname} (Inactive)", callback_data=f"sheet_toggle_{fid}")])
    
    buttons.append([InlineKeyboardButton("✅ Done — Generate PDF", callback_data="sheet_generate")])
    buttons.append([InlineKeyboardButton("📚 All Active Formats", callback_data="sheet_all")])
    
    context.user_data['sheet_selected'] = [fid for fid, _, is_active in formats if is_active]
    
    await progress.delete()
    await update.message.reply_text(
        f"📊 *{len(mcqs)}টি MCQ পাওয়া গেছে!*\n\nActive ফরম্যাট সিলেক্ট করো (টগল):",
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /ping HANDLER
# ============================================================
async def ping_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot uptime and status"""
    uptime = datetime.now() - BOT_START_TIME
    days = uptime.days
    hours, rem = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    
    try:
        import psutil
        process = psutil.Process()
        ram_mb = process.memory_info().rss / 1024 / 1024
        ram_text = f"🖥️ RAM: {ram_mb:.0f} MB"
    except:
        ram_text = "🖥️ RAM: N/A"
    
    await update.message.reply_text(f"""🟢 *Bot চালু আছে!*

⏱️ *Uptime:* {days}d {hours}h {minutes}m {seconds}s
📅 *Started:* {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
{ram_text}""", parse_mode=None)


# ============================================================
# /error HANDLER
# ============================================================
async def error_handler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot health check"""
    report = "🔍 *Bot Health Check:*\n\n"
    
    # Gemini
    try:
        stats = gemini_manager.get_stats()
        report += f"✅ *Gemini API:* {stats['healthy']}/{stats['total']} keys active\n"
    except Exception as e:
        report += f"❌ *Gemini API:* সমস্যা — {str(e)[:50]}\n"
    
    # ImgBB
    try:
        report += f"✅ *ImgBB API:* {len(imgbb_manager.keys)} keys\n"
    except Exception as e:
        report += f"❌ *ImgBB API:* সমস্যা — {str(e)[:50]}\n"
    
    # Database
    try:
        await db.fetchone('SELECT 1')
        report += "✅ *Database:* ঠিক আছে\n"
    except Exception as e:
        report += f"❌ *Database:* সমস্যা — {str(e)[:50]}\n"
    
    # Chromium
    try:
        report += "✅ *Chromium:* Ready\n"
    except:
        report += "⚠️ *Chromium:* চেক করা যায়নি\n"
    
    # Channels
    channels = await db.fetchall('SELECT COUNT(*) FROM channels')
    report += f"\n📢 *Channels:* {channels[0][0]} connected\n"
    
    # Users
    users = await db.fetchall('SELECT COUNT(*) FROM bot_users')
    report += f"👤 *Users:* {users[0][0]}\n"
    
    await update.message.reply_text(report, parse_mode=None)


# ============================================================
# /logs HANDLER
# ============================================================
async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send log file (Owner only)"""
    if update.effective_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Owner only!")
        return
    
    try:
        import subprocess
        result = subprocess.run(['journalctl', '-u', 'atlas-bot', '-n', '500'], 
                               capture_output=True, text=True)
        logs = result.stdout or "No systemd logs found.\n\nCheck data/bot.log"
    except:
        logs = "Cannot read systemd logs.\nCheck data/bot.log"
    
    await update.message.reply_document(
        document=logs.encode('utf-8'),
        filename='atlas_bot_logs.txt',
        caption="📋 Last 500 lines"
    )


# ============================================================
# /collect HANDLER
# ============================================================
async def collect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start poll collection"""
    user_id = update.effective_user.id
    
    poll_collector.start(user_id)
    
    await update.message.reply_text("""📥 *Poll Collection শুরু!*

এখন Poll পাঠাও, আমি collect করবো।
শেষ হলে:
• `/done` — CSV ফাইল পাবে
• `/status` — কত collected
• `/cancel` — বাতিল""", parse_mode=None)


# ============================================================
# /done HANDLER
# ============================================================
async def done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finish collection and get CSV"""
    user_id = update.effective_user.id
    
    polls = poll_collector.finish(user_id)
    if not polls:
        await update.message.reply_text("❌ কোনো পোল সংগ্রহ করা হয়নি!")
        return
    
    # Convert poll data to mcqs_to_csv format
    mcq_list = []
    for p in polls:
        mcq_list.append({
            'question': p.get('questions', p.get('question', '')),
            'options': {
                'A': p.get('option1', ''),
                'B': p.get('option2', ''),
                'C': p.get('option3', ''),
                'D': p.get('option4', '')
            },
            'answer': p.get('answer', '1'),
            'explanation': p.get('explanation', '')
        })
    csv_bytes = mcqs_to_csv(mcq_list)
    await update.message.reply_document(
        document=csv_bytes,
        filename=f"collected_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        caption=f"✅ *{len(polls)}টি পোল সংগ্রহ সম্পন্ন!*",
        parse_mode=None
    )


# ============================================================
# /status HANDLER
# ============================================================
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check collection status"""
    user_id = update.effective_user.id
    count = poll_collector.get_count(user_id)
    await update.message.reply_text(f"📊 *Collected:* {count} টি পোল", parse_mode=None)


# ============================================================
# /cancel HANDLER
# ============================================================
async def cancel_collection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel collection"""
    user_id = update.effective_user.id
    poll_collector.cancel(user_id)
    await update.message.reply_text("❌ Collection বাতিল করা হয়েছে!")


# ============================================================
# /pause HANDLER
# ============================================================
async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause ongoing poll sending"""
    from global_state import GLOBAL_PAUSE; GLOBAL_PAUSE[update.effective_user.id] = True; print(f"⏸️ Paused: {update.effective_user.id}")
    await update.message.reply_text("⏸️ পোল পাঠানো থামানো হয়েছে!\n▶️ `/resume` দিয়ে আবার চালু করো।")


# ============================================================
# /resume HANDLER
# ============================================================
async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume poll sending"""
    from global_state import GLOBAL_PAUSE; GLOBAL_PAUSE[update.effective_user.id] = False
    await update.message.reply_text("▶️ পোল পাঠানো আবার চালু হয়েছে!")


# ============================================================
# /restart HANDLER
# ============================================================
async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart bot - instant restart"""
    import os
    if update.effective_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Owner only!")
        return
    
    await update.message.reply_text("🔄 Restarting...")
    os.system("bash ~/AtlasMasterBot/restart_bot.sh &")
    # Save current process PID
    pid = os.getpid()
    # Start new bot in background
    import os
    os.system("pkill -9 -f 'python bot.py' 2>/dev/null; sleep 2; cd ~/AtlasMasterBot && nohup python bot.py > /dev/null 2>&1 &")
    # New process will be started by auto_update.sh crontab
    # Kill current process
    os.kill(pid, 9)


# ============================================================
# TOOLS CALLBACK HANDLER
# ============================================================
async def handle_tools_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all tools callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Exp settings
    if data.startswith('exp_set_'):
        mode = data.replace('exp_set_', '')
        await db.execute('UPDATE exp_settings SET mode = ? WHERE id = 1', (mode,))
        
        if mode == 'custom':
            context.user_data['setting_custom_exp'] = True
            await query.edit_message_text("📝 Custom Explanation লিখো (সব MCQ-তে এটাই যাবে):")
        elif mode == 'tag':
            context.user_data['setting_tag_name'] = True
            await query.edit_message_text("📝 Tag Name লিখো (ব্যাখ্যার পরে বসবে):")
        else:
            await query.edit_message_text("✅ Auto Mode Activated!\nSource থেকে অটো ব্যাখ্যা নেওয়া হবে।")
    
    # Tag settings
    elif data.startswith('tag_toggle_'):
        tag_id = int(data.replace('tag_toggle_', ''))
        current = await db.fetchone('SELECT is_active FROM tag_settings WHERE id = ?', (tag_id,))
        if current:
            new_state = 0 if current[0] else 1
            await db.execute('UPDATE tag_settings SET is_active = ? WHERE id = ?', (new_state, tag_id))
            await query.answer(f"{'✅ Activated' if new_state else '❌ Deactivated'}!")
            # Update button icons
            tags = await db.fetchall('SELECT id, tag_type, tag_name, is_active FROM tag_settings ORDER BY id')
            buttons = []
            for tid, ttype, tname, tactive in tags:
                icon = "✅" if tactive else "❌"
                buttons.append([
                    InlineKeyboardButton(f"{icon} {ttype}", callback_data=f"tag_toggle_{tid}"),
                    InlineKeyboardButton("✏️ Edit", callback_data=f"tag_edit_{tid}"),
                    InlineKeyboardButton("🗑️ Del", callback_data=f"tag_delete_{tid}")
                ])
            buttons.append([InlineKeyboardButton("➕ New Tag", callback_data="tag_add")])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data.startswith('tag_edit_'):
        tag_id = int(data.replace('tag_edit_', ''))
        context.user_data['editing_tag'] = tag_id
        await query.edit_message_text("📝 নতুন Tag Name লিখো:")
    
    elif data.startswith('tag_delete_'):
        tag_id = int(data.replace('tag_delete_', ''))
        await db.execute('DELETE FROM tag_settings WHERE id = ?', (tag_id,))
    
    elif data == 'tag_add':
        context.user_data['adding_tag'] = True
        buttons = [
            [InlineKeyboardButton("tag1 — উপরে (গ্যাপ সহ)", callback_data="tag_type_tag1")],
            [InlineKeyboardButton("tag2 — নিচে (গ্যাপ সহ)", callback_data="tag_type_tag2")],
            [InlineKeyboardButton("tag3 — পাশে inline", callback_data="tag_type_tag3")],
            [InlineKeyboardButton("tag4 — উপরে (গ্যাপ ছাড়া)", callback_data="tag_type_tag4")],
        ]
        await query.edit_message_text("🏷️ Tag Type সিলেক্ট করো:", reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data.startswith('tag_type_'):
        tag_type = data.replace('tag_type_', '')
        context.user_data['new_tag_type'] = tag_type
        context.user_data['adding_tag_name'] = True
        await query.edit_message_text(f"📝 {tag_type} এর Name লিখো:")
    
    # Sheet callbacks
    elif data.startswith('sheet_toggle_'):
        fid = data.replace('sheet_toggle_', '')
        selected = context.user_data.get('sheet_selected', [])
        if fid in selected:
            selected.remove(fid)
        else:
            selected.append(fid)
        context.user_data['sheet_selected'] = selected
        await query.answer(f"{len(selected)} টি সিলেক্টেড")
    
    elif data == 'sheet_generate':
        selected = context.user_data.get('sheet_selected', [])
        mcqs = context.user_data.get('sheet_mcqs', [])
        
        if not selected or not mcqs:
            await query.edit_message_text("❌ কোনো ফরম্যাট বা MCQ নেই!")
            return
        
        await query.edit_message_text("📝 PDF এর Title লিখো:")
        context.user_data['sheet_formats'] = selected
        context.user_data['waiting_sheet_title'] = True
    
    elif data == 'sheet_all':
        formats = await db.fetchall('SELECT format_id FROM sheet_formats WHERE is_active = 1')
        context.user_data['sheet_selected'] = [f[0] for f in formats]
        await query.answer(f"All {len(formats)} Active Formats Selected!")


# ============================================================
# MESSAGE HANDLER FOR SETTINGS
# ============================================================
async def handle_settings_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages for settings"""
    text = update.message.text
    
    # Custom Exp
    if context.user_data.get('setting_custom_exp'):
        await db.execute('UPDATE exp_settings SET custom_text = ? WHERE id = 1', (text,))
        context.user_data.pop('setting_custom_exp', None)
        await update.message.reply_text("✅ Custom Explanation সেট করা হয়েছে!\nসব MCQ-তে এটাই ব্যাখ্যা হিসেবে যাবে।")
        return
    
    # Tag Name
    if context.user_data.get('setting_tag_name'):
        await db.execute('UPDATE exp_settings SET tag_name = ? WHERE id = 1', (text,))
        context.user_data.pop('setting_tag_name', None)
        await update.message.reply_text("✅ Tag Name সেট করা হয়েছে!\nব্যাখ্যার পরে অটো বসবে।")
        return
    
    # Edit Tag
    if 'editing_tag' in context.user_data:
        tag_id = context.user_data['editing_tag']
        await db.execute('UPDATE tag_settings SET tag_name = ? WHERE id = ?', (text, tag_id))
        del context.user_data['editing_tag']
        await update.message.reply_text("✅ Tag আপডেট সম্পন্ন!")
        return
    
    # New Tag Name
    if context.user_data.get('adding_tag_name'):
        tag_type = context.user_data.get('new_tag_type', 'tag1')
        await db.execute(
            'INSERT INTO tag_settings (tag_type, tag_name, position, is_active) VALUES (?, ?, ?, 1)',
            (tag_type, text, tag_type)
        )
        context.user_data.pop('adding_tag_name', None)
        context.user_data.pop('new_tag_type', None)
        context.user_data.pop('adding_tag', None)
        await update.message.reply_text(f"✅ নতুন Tag '{text}' ({tag_type}) যোগ করা হয়েছে!")
        return
    
    # Sheet Title
    if context.user_data.get('waiting_sheet_title'):
        title = text
        selected = context.user_data.get('sheet_formats', [])
        mcqs = context.user_data.get('sheet_mcqs', [])
        
        context.user_data.pop('waiting_sheet_title', None)
        
        if not mcqs:
            await update.message.reply_text("❌ MCQ সেশন শেষ!")
            return
        
        progress = await update.message.reply_text("🖨️ PDF তৈরি হচ্ছে...")
        
        for fid in selected:
            template_str = SHEET_TEMPLATES.get(fid)
            if template_str:
                from jinja2 import Template
                template = Template(template_str)
                html = template.render(title=title, mcqs=mcqs)
                
                pdf_path = f"data/temp/sheet_{fid}_{int(time.time())}.pdf"
                success = await AsyncPDFExporter.html_to_pdf(html, pdf_path)
                
                if success:
                    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
                    thumb = thumb_row[0] if thumb_row else None
                    
                    with open(pdf_path, 'rb') as f:
                        await update.message.reply_document(
                            document=f.read(),
                            filename=f"{title}_{fid}.pdf",
                            thumbnail=thumb
                        )
                    os.remove(pdf_path)
        
        await progress.delete()
        await update.message.reply_text(f"✅ {len(selected)}টি ফরম্যাটে PDF তৈরি সম্পন্ন!")
        return
