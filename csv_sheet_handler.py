#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - CSV to Practice Sheet PDF Handler"""

import os
import io
import csv
import time
import asyncio
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from jinja2 import Template
from config import db
from services import AsyncPDFExporter, parse_csv_to_mcqs

# ============================================================
# 5 FORMAT HTML TEMPLATES (All in one file for easy editing)
# ============================================================

TEMPLATE_FORMAT_01 = '''<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:15mm;line-height:1.8;background:#fff}
h1{text-align:center;color:#1B4F72;margin-bottom:10mm;font-size:24pt;border-bottom:3px solid #3498db;padding-bottom:5mm;background:linear-gradient(135deg,#1B4F72,#2c3e50);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.mcq{margin-bottom:8mm;page-break-inside:avoid;border:1px solid #FBBF24;border-radius:8px;padding:4mm;background:#FFFBF0;box-shadow:0 2px 4px rgba(0,0,0,0.05)}
.question{font-weight:700;font-size:11pt;margin-bottom:2mm;color:#34495e}
.options{display:grid;grid-template-columns:1fr 1fr;gap:2mm;margin-left:4mm}
.option{padding:2mm 3mm;background:#fff;border:1px solid #D1D5DB;border-radius:4px;font-size:10pt;transition:all 0.2s}
.answer{margin-top:3mm;padding:2mm 3mm;background:#DCFCE7;border-left:4px solid #4ADE80;font-size:9pt;color:#14532D;border-radius:0 4px 4px 0}
.answer strong{color:#059669}
.exp{margin-top:2mm;padding:2mm 3mm;background:#EFF6FF;border-left:4px solid #4299E1;font-size:9pt;color:#1E40AF;border-radius:0 4px 4px 0}
</style>
</head>
<body data-ready="true">
<h1>{{ title }}</h1>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option"><strong>{{ key }}.</strong> {{ val }}</div>
{% endfor %}
</div>
<div class="answer"><strong>উত্তর:</strong> {{ mcq.answer }}</div>
{% if mcq.explanation %}<div class="exp"><strong>ব্যাখ্যা:</strong> {{ mcq.explanation }}</div>{% endif %}
</div>
{% endfor %}
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>'''

TEMPLATE_FORMAT_02 = '''<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:15mm;line-height:1.8}
h1{text-align:center;color:#1B4F72;margin-bottom:5mm;font-size:24pt}
h2{text-align:center;color:#e74c3c;margin:10mm 0 5mm;font-size:18pt;page-break-before:always;border-bottom:2px solid #e74c3c;padding-bottom:3mm}
.mcq{margin-bottom:8mm;page-break-inside:avoid}
.question{font-weight:700;font-size:11pt;margin-bottom:2mm}
.options{margin-left:6mm}
.option{margin:1.5mm 0;font-size:10pt;padding:1mm 2mm}
.answer-page{margin-top:10mm}
.answer-item{margin-bottom:5mm;padding:4mm;background:#FFF3E0;border-left:4px solid #FF9800;border-radius:0 4px 4px 0}
.answer-item strong{color:#E65100}
</style>
</head>
<body data-ready="true">
<h1>{{ title }}</h1>
<h2>📝 প্রশ্নপত্র</h2>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
</div>
{% endfor %}
<h2>✅ উত্তরপত্র</h2>
<div class="answer-page">
{% for mcq in mcqs %}
<div class="answer-item"><strong>{{ loop.index }}.</strong> উত্তর: {{ mcq.answer }}{% if mcq.explanation %} — <em>{{ mcq.explanation }}</em>{% endif %}</div>
{% endfor %}
</div>
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>'''

TEMPLATE_FORMAT_03 = '''<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:12mm;line-height:1.6;font-size:10pt}
h1{text-align:center;color:#1B4F72;margin-bottom:8mm;font-size:20pt;border-bottom:2px solid #3498db;padding-bottom:3mm}
.mcq{margin-bottom:5mm;page-break-inside:avoid}
.question{font-weight:700;font-size:10pt;margin-bottom:1.5mm}
.options{margin-left:5mm;display:grid;grid-template-columns:1fr 1fr;gap:1.5mm}
.option{font-size:9pt;padding:1mm}
.answer-table{width:100%;border-collapse:collapse;margin-top:8mm;font-size:9pt;page-break-before:always}
.answer-table caption{font-size:14pt;font-weight:bold;margin-bottom:3mm;color:#1B4F72}
.answer-table th,.answer-table td{border:1px solid #555;padding:2mm 3mm;text-align:center}
.answer-table th{background:#1B4F72;color:#fff}
.answer-table tr:nth-child(even){background:#EFF6FF}
</style>
</head>
<body data-ready="true">
<h1>{{ title }}</h1>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option"><strong>{{ key }}.</strong> {{ val }}</div>
{% endfor %}
</div>
</div>
{% endfor %}
<div class="answer-table">
<table>
<caption>📋 Answer Key</caption>
<tr><th>Q.No</th><th>উত্তর</th><th>ব্যাখ্যা</th></tr>
{% for mcq in mcqs %}
<tr><td>{{ loop.index }}</td><td><strong>{{ mcq.answer }}</strong></td><td>{{ mcq.explanation[:80] if mcq.explanation else '-' }}</td></tr>
{% endfor %}
</table>
</div>
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>'''

TEMPLATE_FORMAT_04 = '''<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:12mm;line-height:1.6}
h1{text-align:center;color:#1B4F72;margin-bottom:8mm;font-size:20pt}
.mcq{margin-bottom:5mm;page-break-inside:avoid;border:1px solid #D1D5DB;border-radius:6px;padding:3mm;background:#FAFAFA}
.question{font-weight:700;font-size:10pt;margin-bottom:1.5mm}
.options{margin-left:5mm;display:grid;grid-template-columns:1fr 1fr;gap:1mm}
.option{margin:1mm 0;font-size:9pt;padding:1mm 2mm;background:#fff;border-radius:3px}
.ans-inline{font-weight:700;color:#059669;margin-left:3mm;background:#DCFCE7;padding:0 3mm;border-radius:3px;font-size:9pt}
</style>
</head>
<body data-ready="true">
<h1>{{ title }}</h1>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }} <span class="ans-inline">✓ {{ mcq.answer }}</span></p>
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
</div>
{% endfor %}
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>'''

TEMPLATE_FORMAT_05 = '''<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:12mm;line-height:1.6}
h1{text-align:center;color:#1B4F72;margin-bottom:8mm;font-size:20pt}
.answer-key{display:grid;grid-template-columns:repeat(5,1fr);gap:3mm;margin-bottom:10mm}
.answer-item{padding:3mm;background:#ecf0f1;border-radius:4mm;text-align:center;font-size:10pt;box-shadow:0 2px 4px rgba(0,0,0,0.1)}
.answer-item strong{color:#e74c3c;font-size:14pt}
.summary-table{width:100%;border-collapse:collapse;margin-top:8mm;font-size:9pt}
.summary-table caption{font-size:14pt;font-weight:bold;margin-bottom:3mm;color:#1B4F72}
.summary-table th,.summary-table td{border:1px solid #555;padding:2mm 3mm;text-align:center}
.summary-table th{background:#1B4F72;color:#fff}
.summary-table tr:nth-child(even){background:#f9f9f9}
</style>
</head>
<body data-ready="true">
<h1>{{ title }} — Answer Key</h1>
<div class="answer-key">
{% for mcq in mcqs %}
<div class="answer-item">{{ loop.index }}. <strong>{{ mcq.answer }}</strong></div>
{% endfor %}
</div>
<table class="summary-table">
<caption>📋 সম্পূর্ণ তালিকা</caption>
<tr><th>Q.No</th><th>উত্তর</th><th>ব্যাখ্যা</th></tr>
{% for mcq in mcqs %}
<tr><td>{{ loop.index }}</td><td><strong>{{ mcq.answer }}</strong></td><td>{{ mcq.explanation[:100] if mcq.explanation else '-' }}</td></tr>
{% endfor %}
</table>
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>'''


# ============================================================
# FORMAT NAME MAP
# ============================================================
FORMAT_NAMES = {
    'format_01': 'Practice Sheet (প্রশ্ন + উত্তর + ব্যাখ্যা)',
    'format_02': 'Solve Sheet (আলাদা প্রশ্নপত্র ও উত্তরপত্র)',
    'format_03': 'Exam Style (Answer টেবিল সহ)',
    'format_04': 'Mixed Style (ইনলাইন উত্তর)',
    'format_05': 'Summary (Answer Key)'
}

SHEET_TEMPLATES = {
    'format_01': TEMPLATE_FORMAT_01,
    'format_02': TEMPLATE_FORMAT_02,
    'format_03': TEMPLATE_FORMAT_03,
    'format_04': TEMPLATE_FORMAT_04,
    'format_05': TEMPLATE_FORMAT_05
}


# ============================================================
# /sheet HANDLER
# ============================================================
async def sheet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate Practice Sheet PDF from CSV — 5 formats with checkbox select"""
    
    # Get CSV from reply or stored
    mcqs = None
    filename = "practice"
    
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        filename = doc.file_name.rsplit('.', 1)[0] if '.' in doc.file_name else doc.file_name
        
        progress = await update.message.reply_text("⏳ ফাইল প্রসেসিং...")
        file = await doc.get_file()
        content = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content.decode('utf-8-sig'))
        await progress.delete()
    
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        content_str = csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes)
        mcqs = parse_csv_to_mcqs(content_str)
        filename = context.user_data.get('last_topic', 'practice')
    
    if not mcqs:
        await update.message.reply_text("❌ CSV ফাইলে reply করে `/sheet` দাও, অথবা আগে `/img` বা `/txt` দিয়ে MCQ বানাও!")
        return
    
    # Store MCQs for later
    context.user_data['sheet_mcqs'] = mcqs
    context.user_data['sheet_filename'] = filename
    context.user_data['sheet_selected'] = []
    
    # Get active formats from DB
    formats = await db.fetchall('SELECT format_id, format_name, is_active FROM sheet_formats ORDER BY format_id')
    
    if not formats:
        # Insert defaults if not exist
        for fid, fname in FORMAT_NAMES.items():
            await db.execute(
                'INSERT OR IGNORE INTO sheet_formats (format_id, format_name, is_active) VALUES (?, ?, 1)',
                (fid, fname)
            )
        formats = [(fid, fname, 1) for fid, fname in FORMAT_NAMES.items()]
    
    # Build buttons — checkbox style
    buttons = []
    for fid, fname, is_active in formats:
        icon = "☑️" if is_active else "☐"
        buttons.append([InlineKeyboardButton(
            f"{icon} {fname}", 
            callback_data=f"sheet_toggle_{fid}"
        )])
    
    buttons.append([InlineKeyboardButton("✅ Done — Generate PDF", callback_data="sheet_generate")])
    buttons.append([InlineKeyboardButton("📚 Select All Active", callback_data="sheet_select_all")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="sheet_cancel")])
    
    # Pre-select active formats
    context.user_data['sheet_selected'] = [fid for fid, _, is_active in formats if is_active]
    
    await update.message.reply_text(
        f"📊 *{len(mcqs)}টি MCQ পাওয়া গেছে!*\n📁 `{filename}`\n\n*ফরম্যাট সিলেক্ট করো (টগল):*\n(Active ☑️ | Inactive ☐)",
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# GENERATE PDF FROM SELECTED FORMATS
# ============================================================
async def generate_sheet_pdfs(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                               title: str, selected_formats: list):
    """Generate PDFs for selected formats"""
    query = update.callback_query
    mcqs = context.user_data.get('sheet_mcqs', [])
    
    if not mcqs:
        await query.edit_message_text("❌ MCQ সেশন শেষ! আবার `/sheet` দাও।")
        return
    
    # Get thumbnail
    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
    thumb = thumb_row[0] if thumb_row else None
    
    total = len(selected_formats)
    progress_msg = await query.message.reply_text(f"🖨️ PDF তৈরি হচ্ছে... 0/{total}")
    
    for idx, fid in enumerate(selected_formats, 1):
        template_str = SHEET_TEMPLATES.get(fid)
        if not template_str:
            continue
        
        # Update progress
        bar = '█' * int((idx / total) * 10) + '░' * (10 - int((idx / total) * 10))
        await progress_msg.edit_text(f"🖨️ PDF তৈরি হচ্ছে... {idx}/{total}\n[{bar}] {int(idx/total*100)}%\n📄 Format: {FORMAT_NAMES.get(fid, fid)[:40]}")
        
        # Render HTML
        template = Template(template_str)
        html = template.render(title=title, mcqs=mcqs)
        
        # Generate PDF
        pdf_path = f"data/temp/sheet_{fid}_{int(time.time())}.pdf"
        success = await AsyncPDFExporter.html_to_pdf(html, pdf_path)
        
        if success and os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
            
            await query.message.reply_document(
                document=pdf_bytes,
                filename=f"{title}_{fid}.pdf",
                caption=f"📄 {FORMAT_NAMES.get(fid, fid)}\n📊 {len(mcqs)}টি MCQ",
                thumbnail=thumb
            )
            os.remove(pdf_path)
    
    await progress_msg.delete()
    await query.message.reply_text(f"✅ *{len(selected_formats)}টি ফরম্যাটে PDF তৈরি সম্পন্ন!*\n📊 মোট MCQ: {len(mcqs)}", parse_mode=None)


# ============================================================
# SHEET CALLBACK HANDLER
# ============================================================
async def handle_sheet_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sheet format selection callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'sheet_cancel':
        await query.edit_message_text("❌ বাতিল করা হয়েছে!")
        context.user_data.pop('sheet_mcqs', None)
        context.user_data.pop('sheet_selected', None)
        return
    
    elif data == 'sheet_select_all':
        formats = await db.fetchall('SELECT format_id FROM sheet_formats WHERE is_active = 1')
        context.user_data['sheet_selected'] = [f[0] for f in formats]
        await query.answer(f"✅ {len(formats)}টি Active ফরম্যাট সিলেক্টেড!\nDone চাপো।", show_alert=True)
    
    elif data.startswith('sheet_toggle_'):
        fid = data.replace('sheet_toggle_', '')
        selected = context.user_data.get('sheet_selected', [])
        
        if fid in selected:
            selected.remove(fid)
            await query.answer(f"❌ {FORMAT_NAMES.get(fid, fid)[:30]} — Removed")
        else:
            selected.append(fid)
            await query.answer(f"✅ {FORMAT_NAMES.get(fid, fid)[:30]} — Added")
        
        context.user_data['sheet_selected'] = selected
    
    elif data == 'sheet_generate':
        selected = context.user_data.get('sheet_selected', [])
        mcqs = context.user_data.get('sheet_mcqs', [])
        
        if not selected:
            await query.answer("❌ কোনো ফরম্যাট সিলেক্ট করোনি!", show_alert=True)
            return
        
        if not mcqs:
            await query.edit_message_text("❌ MCQ সেশন শেষ!")
            return
        
        # Ask for title
        context.user_data['sheet_formats_to_generate'] = selected
        context.user_data['waiting_sheet_title'] = True
        
        filename = context.user_data.get('sheet_filename', 'Practice')
        await query.edit_message_text(f"📝 PDF এর *Title* লিখো:\n\nDefault: `{filename}`", parse_mode=None)


# ============================================================
# HANDLE SHEET TITLE INPUT
# ============================================================
async def handle_sheet_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle title text input for sheet generation"""
    if not context.user_data.get('waiting_sheet_title'):
        return False
    
    title = update.message.text.strip()
    selected = context.user_data.get('sheet_formats_to_generate', [])
    
    context.user_data.pop('waiting_sheet_title', None)
    context.user_data.pop('sheet_formats_to_generate', None)
    
    if not title:
        title = context.user_data.get('sheet_filename', 'Practice Sheet')
    
    await update.message.reply_text(f"🖨️ *{title}* — {len(selected)}টি ফরম্যাটে PDF তৈরি হচ্ছে...", parse_mode=None)
    
    # Create a fake callback_query for generate function
    # Actually we call directly
    mcqs = context.user_data.get('sheet_mcqs', [])
    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
    thumb = thumb_row[0] if thumb_row else None
    
    total = len(selected)
    progress_msg = await update.message.reply_text(f"⏳ 0/{total}")
    
    for idx, fid in enumerate(selected, 1):
        template_str = SHEET_TEMPLATES.get(fid)
        if not template_str:
            continue
        
        bar = '█' * int((idx / total) * 10) + '░' * (10 - int((idx / total) * 10))
        await progress_msg.edit_text(f"🖨️ {idx}/{total} [{bar}] {int(idx/total*100)}%")
        
        template = Template(template_str)
        html = template.render(title=title, mcqs=mcqs)
        
        pdf_path = f"data/temp/sheet_{fid}_{int(time.time())}.pdf"
        success = await AsyncPDFExporter.html_to_pdf(html, pdf_path)
        
        if success and os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                await update.message.reply_document(
                    document=f.read(),
                    filename=f"{title}_{fid}.pdf",
                    caption=f"📄 {FORMAT_NAMES.get(fid, fid)}\n📊 {len(mcqs)}টি MCQ",
                    thumbnail=thumb
                )
            os.remove(pdf_path)
    
    await progress_msg.delete()
    await update.message.reply_text(f"✅ *{len(selected)}টি ফরম্যাটে PDF তৈরি সম্পন্ন!*", parse_mode=None)
    return True
