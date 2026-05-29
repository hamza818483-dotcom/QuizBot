#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - CSV to Practice Sheet PDF - 100% Sheet Bot Code"""

import os, io, csv, time, asyncio, tempfile, re, base64, requests
from PIL import Image as PILImage
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import db
from services import parse_csv_to_mcqs

# ============================================================
# COLOR CONFIG (From Sheet Bot - 100% COPY)
# ============================================================
COLORS = {
    "header_bg": "#1B4F72", "header_text": "#FFFFFF",
    "question_bg": "#FFFBF0", "question_border": "#FBBF24",
    "qnum_bg": "#FEF3C7", "qnum_text": "#92400E",
    "option_bg": "#FFFFFF", "option_border": "#D1D5DB", "option_text": "#1F2937",
    "correct_bg": "#DCFCE7", "correct_border": "#4ADE80", "correct_text": "#14532D",
    "explanation_bg": "#EFF6FF", "explanation_border": "#4299E1", "explanation_text": "#1E40AF",
    "footer_text": "#6B7280",
}

FORMAT_NAMES = {
    'format_01': '📖 Practice Sheet (প্রশ্ন + উত্তর + ব্যাখ্যা)',
    'format_02': '📖 Solve Sheet (প্রশ্নপত্র + উত্তরপত্র)',
    'format_03': '📖 Exam Style (Answer টেবিল)',
    'format_04': '📖 Mixed Style (ইনলাইন উত্তর)',
    'format_05': '📖 Summary (Answer Key)',
}

# ============================================================
# BENGALI FIX (From Sheet Bot - 100% COPY)
# ============================================================
def fix_bn(text):
    if not text: return ""
    fixes = [('\u09C7\u09D7','\u09CC'),('\u09C7\u09BE','\u09CB'),('\u09BE\u09C7','\u09CB'),('\u09AF\u09BC','\u09DF'),('\u09A1\u09BC','\u09DC'),('\u09A2\u09BC','\u09DD')]
    for b,g in fixes: text=text.replace(b,g)
    return text

# ============================================================
# CHEMICAL FORMULA (From Sheet Bot - 100% COPY)
# ============================================================
def fix_chemical(text):
    if not text: return ""
    sub_map={'₀':'<sub>0</sub>','₁':'<sub>1</sub>','₂':'<sub>2</sub>','₃':'<sub>3</sub>','₄':'<sub>4</sub>','₅':'<sub>5</sub>','₆':'<sub>6</sub>','₇':'<sub>7</sub>','₈':'<sub>8</sub>','₉':'<sub>9</sub>'}
    sup_map={'⁰':'<sup>0</sup>','¹':'<sup>1</sup>','²':'<sup>2</sup>','³':'<sup>3</sup>','⁴':'<sup>4</sup>','⁵':'<sup>5</sup>','⁶':'<sup>6</sup>','⁷':'<sup>7</sup>','⁸':'<sup>8</sup>','⁹':'<sup>9</sup>','⁺':'<sup>+</sup>','⁻':'<sup>-</sup>'}
    for u,h in sub_map.items(): text=text.replace(u,h)
    for u,h in sup_map.items(): text=text.replace(u,h)
    return text

# ============================================================
# IMAGE SUPPORT (From Sheet Bot - 100% COPY)
# ============================================================
def extract_images(text):
    return re.findall(r'src=["\'](https?://[^\s>"\']+)["\']', text)

def download_image(url):
    try:
        resp=requests.get(url,timeout=10)
        if resp.status_code==200:
            img=PILImage.open(BytesIO(resp.content))
            buf=BytesIO();img.save(buf,format='PNG')
            b64=base64.b64encode(buf.getvalue()).decode()
            return f'<img src="data:image/png;base64,{b64}" style="max-width:100px;height:auto;display:block;margin:3px 0;">'
    except: pass
    return ""

def get_clean(text):
    if not text: return "",[]
    text=str(text); imgs=extract_images(text)
    text=re.sub(r'<img[^>]+>','',text); text=re.sub(r'<[^>]+>','',text)
    for s,d in [('&amp;','&'),('&lt;','<'),('&gt;','>'),('&nbsp;',' ')]: text=text.replace(s,d)
    text=fix_chemical(text); text=fix_bn(text.strip())
    return text,imgs

# ============================================================
# CSS BUILDER (From Sheet Bot - 100% COPY)
# ============================================================
def get_css(watermark=""):
    wm=""
    if watermark: wm=f'body::before{{content:"{watermark}";position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-30deg);font-size:60pt;color:rgba(0,0,0,0.03);white-space:nowrap;z-index:999;pointer-events:none;font-weight:bold;letter-spacing:5px;}}'
    return f'''<style>
@page{{size:A4;margin:8mm;@bottom-right{{content:counter(page);font-size:7pt;color:{COLORS["footer_text"]}}}}}
body{{font-family:sans-serif;font-size:6.8pt;line-height:1.25;}}
.topic-bar-first{{text-align:center;background:linear-gradient(135deg,rgba(27,79,114,0.97),rgba(27,79,114,0.88));color:#fff;padding:10px 8px;font-size:16pt;font-weight:bold;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 6px 20px rgba(0,0,0,0.25);margin-bottom:8px;border-radius:0 0 8px 8px;letter-spacing:1.5px;text-shadow:0 2px 4px rgba(0,0,0,0.3);}}
.footer-pg{{position:fixed;bottom:0;left:0;right:0;text-align:center;font-size:5.5pt;color:{COLORS["footer_text"]};padding:2px;z-index:100;background:#fff;border-top:1px solid #ddd;}}
.columns{{column-count:2;column-gap:6px;}}
.mcq{{break-inside:avoid;border:1px solid {COLORS["question_border"]};border-radius:5px;padding:3px;margin-bottom:2px;background:{COLORS["question_bg"]}}}
.qnum{{font-weight:bold;color:{COLORS["qnum_text"]};background:{COLORS["qnum_bg"]};padding:1px 3px;border-radius:3px;display:inline-block;margin-bottom:1px;font-size:6pt}}
.question{{font-weight:bold;margin-bottom:1px;font-size:7pt}}
.opt{{padding:1px 2px;margin:1px;border-radius:8px;background:{COLORS["option_bg"]};border:1px solid {COLORS["option_border"]};font-size:6.5pt;display:inline-block;color:{COLORS["option_text"]}}}
.opt-c{{background:{COLORS["correct_bg"]};border-color:{COLORS["correct_border"]};color:{COLORS["correct_text"]};font-weight:bold}}
.exp{{margin-top:1px;padding:2px;background:{COLORS["explanation_bg"]};border-left:2px solid {COLORS["explanation_border"]};font-size:6pt;color:{COLORS["explanation_text"]}}}
.ans-inline{{font-weight:bold;color:{COLORS["correct_text"]};font-size:7pt}}
table.at{{width:100%;border-collapse:collapse;margin-top:4px;font-size:6.5pt}}
table.at th,table.at td{{border:1px solid #555;padding:1px 2px;text-align:center}}
table.at th{{background:#f0f0f0}}
.answer-sidebar{{position:fixed;right:2mm;top:14mm;width:28mm;border:1px solid #333;padding:2px;font-size:5pt;background:#fff;z-index:10;max-height:80%;overflow-y:auto;box-shadow:0 2px 8px rgba(0,0,0,0.15);}}
.exp-table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:6.5pt;page-break-before:always;}}
.exp-table th,.exp-table td{{border:1px solid #555;padding:2px;text-align:center;}}
.exp-table th{{background:#EFF6FF;}}
sub,sup{{font-size:0.65em}}
img{{max-width:80px;height:auto;display:block;margin:1px 0}}
{wm}
</style>'''

# ============================================================
# BUILD HTML (From Sheet Bot - 100% COPY - pandas replaced)
# ============================================================
def build_mcq_data(mcqs):
    """Convert MCQs to Sheet Bot format (pandas-free)"""
    data = []
    BN = ['A','B','C','D','E']
    for qi, mcq in enumerate(mcqs):
        q, qi_ = get_clean(mcq.get('question',''))
        e, ei_ = get_clean(mcq.get('explanation',''))
        opts, oimgs = [], []
        for i, k in enumerate(['A','B','C','D']):
            v = mcq.get('options',{}).get(k,'')
            if v and v.strip():
                ct, ci = get_clean(str(v))
                opts.append(ct)
                oimgs.append(''.join([download_image(u) for u in ci]))
            else:
                opts.append('')
                oimgs.append('')
        ans = str(mcq.get('answer','1')).strip()
        ai = int(ans)-1 if ans.isdigit() else -1
        data.append({
            'n': qi+1,
            'q': q,
            'qi': ''.join([download_image(u) for u in qi_]),
            'opts': opts[:4],
            'oimgs': oimgs[:4],
            'exp': e,
            'ei': ''.join([download_image(u) for u in ei_]),
            'ai': ai,
            'al': BN[ai] if ai>=0 else '?'
        })
    return data

def build_html(data, heading, fmt, hdr_txt="", ftr_txt=""):
    """Build HTML from MCQ data - 100% Sheet Bot logic"""
    css = get_css()
    BN = ['A','B','C','D']
    body = ""
    tbl = ""
    
    # F1: Practice Sheet
    if fmt == 1:
        body = f'<div class="topic-bar-first">{hdr_txt or heading}</div><div class="columns">'
        for d in data:
            body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
            for oi in range(4):
                if d['opts'][oi]:
                    c = ['①','②','③','④'][oi]
                    cl = 'opt opt-c' if oi == d['ai'] else 'opt'
                    body += f'<span class="{cl}">{c} {d["opts"][oi]}{d["oimgs"][oi]}</span> '
            if d['ai'] >= 0:
                body += f' <span class="ans-inline">✓ {BN[d["ai"]]}</span>'
            if d['exp']:
                body += f'<div class="exp">{d["exp"]}{d["ei"]}</div>'
            body += '</div>'
        body += '</div>'
    
    # F2: Solve Sheet
    elif fmt == 2:
        per_page = 15
        pages = [data[i:i+per_page] for i in range(0, len(data), per_page)]
        body = ''
        for pg_idx, page_data in enumerate(pages):
            sidetbl = '<div class="answer-sidebar"><b>Ans</b><table style="font-size:5pt;width:100%;">'
            cols = min(len(page_data), 15)
            for i in range(0, cols, 2):
                sidetbl += '<tr>'
                sidetbl += f'<td>Q{page_data[i]["n"]}:{page_data[i]["al"]}</td>'
                if i+1 < cols:
                    sidetbl += f'<td>Q{page_data[i+1]["n"]}:{page_data[i+1]["al"]}</td>'
                sidetbl += '</tr>'
            sidetbl += '</table></div>'
            body += sidetbl + f'<div style="margin-right:30mm;">'
            for d in page_data:
                body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
                for oi in range(4):
                    if d['opts'][oi]:
                        body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
                body += '</div>'
            body += '</div>'
            if pg_idx < len(pages)-1:
                body += '<div style="page-break-after:always;"></div>'
        tbl = '<div class="exp-table"><h3>📋 ব্যাখ্যা</h3><table><tr><th>Q.No</th><th>ব্যাখ্যা</th></tr>'
        for d in data:
            if d['exp']:
                tbl += f'<tr><td>Q{d["n"]}</td><td>{d["exp"]}{d["ei"]}</td></tr>'
        tbl += '</table></div>'
    
    # F3: Exam Style
    elif fmt == 3:
        per_page = 15
        pages = [data[i:i+per_page] for i in range(0, len(data), per_page)]
        body = ''
        for pg_idx, page_data in enumerate(pages):
            body += '<div class="columns">'
            for d in page_data:
                body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
                for oi in range(4):
                    if d['opts'][oi]:
                        body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
                body += '</div>'
            body += '</div>'
            body += '<div style="margin-top:2px"><b>Ans:</b><table class="at"><tr>'
            for d in page_data:
                body += f'<td>Q{d["n"]}</td>'
            body += '</tr><tr>'
            for d in page_data:
                body += f'<td><b>{d["al"]}</b></td>'
            body += '</tr></table></div>'
            if pg_idx < len(pages)-1:
                body += '<div style="page-break-after:always;"></div>'
    
    # F4: Mixed
    elif fmt == 4:
        body = '<div class="columns">'
        for d in data:
            body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
            for oi in range(4):
                if d['opts'][oi]:
                    body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
            body += f'<span class="ans-inline"> Ans: {d["al"]}</span>'
            if d['exp']:
                body += f'<div class="exp">{d["exp"]}{d["ei"]}</div>'
            body += '</div>'
        body += '</div>'
    
    # F5: Summary
    elif fmt == 5:
        body = '<div class="columns">'
        for d in data:
            body += f'<div class="mcq"><span class="qnum">Q.{d["n"]}</span><div class="question">{d["q"]}{d["qi"]}</div>'
            for oi in range(4):
                if d['opts'][oi]:
                    body += f'<span class="opt">({BN[oi]}) {d["opts"][oi]}{d["oimgs"][oi]}</span> '
            body += '</div>'
        body += '</div>'
        tbl = '<div style="border:2px solid #1B4F72;padding:8px;margin-top:10px"><h3>📋 Answer Key</h3>'
        tbl += '<table class="at"><tr><th>Q.No</th><th>Ans</th><th>ব্যাখ্যা</th></tr>'
        for d in data:
            tbl += f'<tr><td>{d["n"]}</td><td><b>{d["al"]}</b></td><td>{d["exp"]}{d["ei"]}</td></tr>'
        tbl += '</table></div>'
    
    f = f'<div class="footer-pg">{ftr_txt or "Practice makes perfect"}</div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{css}</head><body>{body}{tbl}{f}</body></html>'

# ============================================================
# /sheet HANDLER
# ============================================================
async def sheet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mcqs = None; filename = "practice"
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        filename = doc.file_name.rsplit('.', 1)[0] if '.' in doc.file_name else doc.file_name
        progress = await update.message.reply_text("⏳ ফাইল প্রসেসিং...")
        file = await doc.get_file(); content = await file.download_as_bytearray()
        mcqs = parse_csv_to_mcqs(content.decode('utf-8-sig'))
        await progress.delete()
    elif 'last_csv' in context.user_data:
        csv_bytes = context.user_data['last_csv']
        mcqs = parse_csv_to_mcqs(csv_bytes.decode('utf-8-sig') if isinstance(csv_bytes, bytes) else str(csv_bytes))
        filename = context.user_data.get('last_topic', 'practice')
    if not mcqs: await update.message.reply_text("❌ CSV ফাইলে reply করে `/sheet` দাও"); return
    
    context.user_data['sheet_mcqs'] = mcqs; context.user_data['sheet_filename'] = filename
    context.user_data['sheet_selected'] = []
    print(f"DEBUG sheet_selected: {context.user_data.get("sheet_selected")}")
    print(f"DEBUG sheet_selected: {context.user_data.get("sheet_selected")}")
    formats = await db.fetchall('SELECT format_id, format_name, is_active FROM sheet_formats ORDER BY format_id')
    if not formats:
        for fid, fname in FORMAT_NAMES.items():
            await db.execute('INSERT OR IGNORE INTO sheet_formats (format_id, format_name, is_active) VALUES (?, ?, 1)', (fid, fname))
        formats = [(fid, fname, 1) for fid, fname in FORMAT_NAMES.items()]
    buttons = []
    reading_header_added = False
    print_header_added = False
    for fid, fname, is_active in formats:
        # Add Reading Style header
        if not reading_header_added and fid.startswith('format_'):
            buttons.append([InlineKeyboardButton("📖 READING STYLE (Soft Copy)", callback_data="noop")])
            reading_header_added = True
        # Add Print Style header
        if not print_header_added and fid.startswith('print_'):
            buttons.append([InlineKeyboardButton("🖨️ PRINT STYLE-01 (Hard Copy)", callback_data="noop")])
            print_header_added = True
        
        icon = "☐"  # Always unchecked initially
        buttons.append([InlineKeyboardButton(f"{icon} {fname}", callback_data=f"sheet_toggle_{fid}")])
    buttons.append([InlineKeyboardButton("✅ Done — Generate PDF", callback_data="sheet_generate")])
    buttons.append([InlineKeyboardButton("📚 Select All Active", callback_data="sheet_select_all")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="sheet_cancel")])
    context.user_data['sheet_selected'] = []  # All unchecked by default
    await update.message.reply_text(
        f"📊 *{len(mcqs)}টি MCQ পাওয়া গেছে!*\n📁 `{filename}`\n\n*📖 Reading Style — ফরম্যাট সিলেক্ট করো:*",
        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

# ============================================================
# HANDLE SHEET TITLE
# ============================================================
async def handle_sheet_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_sheet_title'): return False
    title = update.message.text.strip()
    selected = context.user_data.get('sheet_formats_to_generate', [])
    mcqs = context.user_data.get('sheet_mcqs', [])
    context.user_data.pop('waiting_sheet_title', None)
    context.user_data.pop('sheet_formats_to_generate', None)
    if not title: title = context.user_data.get('sheet_filename', 'Practice Sheet')
    progress_msg = await update.message.reply_text(f"🖨️ PDF তৈরি হচ্ছে... 0/{len(selected)}")
    data = build_mcq_data(mcqs)
    for idx, fid in enumerate(selected, 1):
        await progress_msg.edit_text(f"🖨️ PDF তৈরি হচ্ছে... {idx}/{len(selected)}")
        fmt_num = int(fid.split('_')[-1])
        html = build_html(data, title, fmt_num, title)
        pdf_path = f"data/temp/sheet_{fid}_{int(time.time())}.pdf"
        os.makedirs("data/temp", exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as tmp:
                tmp.write(html); tmp_path = tmp.name
            import subprocess
            subprocess.run(['/data/data/com.termux/files/usr/bin/chromium-browser', '--headless', '--disable-gpu', '--no-sandbox', f'--print-to-pdf={pdf_path}', tmp_path], timeout=60, check=True)
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    await update.message.reply_document(document=f.read(), filename=f"{title}_{fid}.pdf", caption=f"📄 {FORMAT_NAMES.get(fid, fid)} | 📊 {len(mcqs)} MCQ")
                os.remove(pdf_path)
            os.remove(tmp_path)
        except Exception as e:
            await update.message.reply_text(f"❌ {fid}: {str(e)[:50]}")
    await progress_msg.delete()
    await update.message.reply_text(f"✅ *{len(selected)}টি ফরম্যাটে PDF তৈরি সম্পন্ন!*", parse_mode='Markdown')
    return True

# ============================================================
# CALLBACK HANDLER
# ============================================================
async def handle_sheet_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    if data == 'noop': await query.answer(); return
    elif data == 'sheet_cancel': await query.edit_message_text("❌ বাতিল!"); return
    elif data == 'sheet_select_all':
        formats = await db.fetchall('SELECT format_id FROM sheet_formats WHERE is_active = 1')
        context.user_data['sheet_selected'] = [f[0] for f in formats]
        await query.answer(f"✅ {len(formats)} টি সিলেক্টেড!", show_alert=True)
    elif data.startswith('sheet_toggle_'):
        fid = data.replace('sheet_toggle_', '')
        selected = context.user_data.get('sheet_selected', [])
        if fid in selected: selected.remove(fid)
        else: selected.append(fid)
        context.user_data['sheet_selected'] = selected
        formats = await db.fetchall('SELECT format_id, format_name, is_active FROM sheet_formats ORDER BY format_id')
        buttons = []
        for ffid, ffname, _ in formats:
            icon = "☑️" if ffid in selected else "☐"
            buttons.append([InlineKeyboardButton(f"{icon} {ffname}", callback_data=f"sheet_toggle_{ffid}")])
        buttons.append([InlineKeyboardButton("✅ Done — Generate PDF", callback_data="sheet_generate")])
        buttons.append([InlineKeyboardButton("📚 Select All Active", callback_data="sheet_select_all")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="sheet_cancel")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    elif data == 'sheet_generate':
        selected = context.user_data.get('sheet_selected', [])
        mcqs = context.user_data.get('sheet_mcqs', [])
        if not selected: await query.answer("❌ কোনো ফরম্যাট সিলেক্ট করোনি!", show_alert=True); return
        if not mcqs: await query.edit_message_text("❌ MCQ সেশন শেষ!"); return
        context.user_data['sheet_formats_to_generate'] = selected
        context.user_data['waiting_sheet_title'] = True
        await query.edit_message_text(f"📝 PDF এর *Title* লিখো:", parse_mode='Markdown')

# ============================================================
# PRINT STYLE-01 INTEGRATION (Added safely at end)
# ============================================================
from print_style_handler01 import PRINT_BUILDERS, PRINT_FORMAT_NAMES
from print_style_handler02 import PRINT2_BUILDERS, PRINT2_FORMAT_NAMES

# Merge print formats into FORMAT_NAMES
FORMAT_NAMES.update(PRINT_FORMAT_NAMES)
FORMAT_NAMES.update(PRINT2_FORMAT_NAMES)

# Override handle_sheet_title to support print formats
_original_handle_sheet_title = handle_sheet_title

async def handle_sheet_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extended handler - supports print formats"""
    if not context.user_data.get('waiting_sheet_title'): return False
    title = update.message.text.strip()
    selected = context.user_data.get('sheet_formats_to_generate', [])
    mcqs = context.user_data.get('sheet_mcqs', [])
    context.user_data.pop('waiting_sheet_title', None)
    context.user_data.pop('sheet_formats_to_generate', None)
    if not title: title = context.user_data.get('sheet_filename', 'Practice Sheet')
    progress_msg = await update.message.reply_text(f"🖨️ PDF তৈরি হচ্ছে... 0/{len(selected)}")
    data = build_mcq_data(mcqs)
    for idx, fid in enumerate(selected, 1):
        await progress_msg.edit_text(f"🖨️ PDF তৈরি হচ্ছে... {idx}/{len(selected)}")
        
        # Check if print format
        if fid.startswith('print_') and fid in PRINT_BUILDERS:
            html = PRINT_BUILDERS[fid](data, title)
        elif fid.startswith("print2_") and fid in PRINT2_BUILDERS:
            html = PRINT2_BUILDERS[fid](data, title)
        else:
            fmt_num = int(fid.split('_')[-1])
            html = build_html(data, title, fmt_num, title)
        
        pdf_path = f"data/temp/sheet_{fid}_{int(time.time())}.pdf"
        os.makedirs("data/temp", exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as tmp:
                tmp.write(html); tmp_path = tmp.name
            import subprocess
            subprocess.run(['/data/data/com.termux/files/usr/bin/chromium-browser', '--headless', '--disable-gpu', '--no-sandbox', f'--print-to-pdf={pdf_path}', tmp_path], timeout=60, check=True)
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    await update.message.reply_document(document=f.read(), filename=f"{title}_{fid}.pdf", caption=f"📄 {FORMAT_NAMES.get(fid, fid)} | 📊 {len(mcqs)} MCQ")
                os.remove(pdf_path)
            os.remove(tmp_path)
        except Exception as e:
            await update.message.reply_text(f"❌ {fid}: {str(e)[:50]}")
    await progress_msg.delete()
    await update.message.reply_text(f"✅ *{len(selected)}টি ফরম্যাটে PDF তৈরি সম্পন্ন!*", parse_mode='Markdown')
    return True
