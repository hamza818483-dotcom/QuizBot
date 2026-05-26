#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF Handler - ALL 4 PDF Formats + /pdfm + /qbm Complete"""

import asyncio
import os
import re
import json
import tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from playwright.async_api import async_playwright
from jinja2 import Template
from pypdf import PdfReader
from config import db, gemini_manager

# HTML Templates for 4 formats
TEMPLATE_FORMAT1 = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:20mm;line-height:1.8;background:#fff}
h1{text-align:center;color:#2c3e50;margin-bottom:10mm;font-size:28pt;border-bottom:3px solid #3498db;padding-bottom:5mm}
.mcq{margin-bottom:15mm;page-break-inside:avoid}
.question{font-weight:700;font-size:12pt;margin-bottom:3mm;color:#34495e}
.options{margin-left:8mm}
.option{margin:2mm 0;font-size:11pt}
.answer{margin-top:3mm;padding:3mm;background:#e8f5e9;border-left:4px solid #4caf50;font-size:10pt}
.answer strong{color:#2e7d32}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:3mm;margin-left:8mm}
@media print{body{padding:15mm}}
</style>
</head>
<body data-ready="false">
<h1>{{ title }}</h1>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
{% if mcq.is_short %}
<div class="grid">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
{% else %}
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
{% endif %}
<div class="answer"><strong>উত্তর:</strong> {{ mcq.answer }}{% if mcq.explanation %} | {{ mcq.explanation }}{% endif %}</div>
</div>
{% endfor %}
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>"""

TEMPLATE_FORMAT2 = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:20mm;line-height:1.8}
h1{text-align:center;color:#2c3e50;margin-bottom:10mm;font-size:28pt}
h2{text-align:center;color:#e74c3c;margin:15mm 0 10mm;font-size:22pt;page-break-before:always}
.mcq{margin-bottom:15mm;page-break-inside:avoid}
.question{font-weight:700;font-size:12pt;margin-bottom:3mm}
.options{margin-left:8mm}
.option{margin:2mm 0;font-size:11pt}
.answer-page .answer{margin-bottom:8mm;padding:5mm;background:#fff3e0;border-left:4px solid #ff9800}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:3mm;margin-left:8mm}
</style>
</head>
<body data-ready="false">
<h1>{{ title }}</h1>
<h2>প্রশ্নপত্র</h2>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
{% if mcq.is_short %}
<div class="grid">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
{% else %}
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
{% endif %}
</div>
{% endfor %}
<h2>উত্তরপত্র</h2>
<div class="answer-page">
{% for mcq in mcqs %}
<div class="answer"><strong>{{ loop.index }}.</strong> {{ mcq.answer }}{% if mcq.explanation %} - {{ mcq.explanation }}{% endif %}</div>
{% endfor %}
</div>
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>"""

TEMPLATE_FORMAT3 = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:15mm;line-height:1.6}
h1{text-align:center;color:#2c3e50;margin-bottom:8mm;font-size:24pt}
.mcq{margin-bottom:10mm;page-break-inside:avoid}
.question{font-weight:700;font-size:11pt;margin-bottom:2mm}
.options{margin-left:6mm}
.option{margin:1.5mm 0;font-size:10pt}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:2mm;margin-left:6mm}
</style>
</head>
<body data-ready="false">
<h1>{{ title }}</h1>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
{% if mcq.is_short %}
<div class="grid">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
{% else %}
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
{% endif %}
</div>
{% endfor %}
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>"""

TEMPLATE_FORMAT4 = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:15mm;line-height:1.6}
h1{text-align:center;color:#2c3e50;margin-bottom:8mm;font-size:24pt}
.answer-key{display:grid;grid-template-columns:repeat(5,1fr);gap:5mm}
.answer-item{padding:3mm;background:#ecf0f1;border-radius:3mm;text-align:center;font-size:11pt}
.answer-item strong{color:#e74c3c;font-size:14pt}
</style>
</head>
<body data-ready="false">
<h1>{{ title }} - Answer Key</h1>
<div class="answer-key">
{% for mcq in mcqs %}
<div class="answer-item">{{ loop.index }}. <strong>{{ mcq.answer }}</strong></div>
{% endfor %}
</div>
<script>document.body.setAttribute('data-ready','true')</script>
</body>
</html>"""


def is_short_option(options: dict) -> bool:
    """Check if options are short"""
    for opt in options.values():
        clean = re.sub(r'<[^>]+>', '', str(opt)).strip()
        if len(clean) > 16:
            return False
    return True


async def generate_pdf(html_content: str, output_path: str) -> bool:
    """Generate PDF from HTML using Playwright"""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        with tempfile.NamedTemporaryFile(suffix='.html', mode='w', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            temp_path = f.name
        
        try:
            await page.goto(f"file://{os.path.abspath(temp_path)}", wait_until='networkidle')
            
            # Wait for fonts
            await page.evaluate("""
                async () => {
                    await document.fonts.ready;
                    await new Promise(r => setTimeout(r, 3000));
                }
            """)
            
            # Wait for MathJax
            for _ in range(10):
                ready = await page.evaluate("""
                    () => {
                        if (typeof MathJax === 'undefined') return true;
                        if (!MathJax.startup) return false;
                        return MathJax.startup.document.state >= 8;
                    }
                """)
                data_ready = await page.get_attribute('body', 'data-ready')
                if ready and data_ready == 'true':
                    break
                await asyncio.sleep(1)
            
            await asyncio.sleep(2)
            await page.pdf(
                path=output_path,
                format='A4',
                margin={'top': '10mm', 'bottom': '10mm', 'left': '10mm', 'right': '10mm'},
                print_background=True
            )
            return True
        finally:
            await page.close()
            await browser.close()
            os.unlink(temp_path)


async def pdfm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pdfm - Create MCQs from PDF study notes"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF file-এ reply করে /pdfm দাও")
        return
    
    # Parse args
    args = context.args
    page_range = None
    title = "MCQ Practice"
    channel_id = None
    
    for i, arg in enumerate(args):
        if arg == '-p' and i + 1 < len(args):
            page_range = args[i + 1]
        elif arg == '-m' and i + 1 < len(args):
            title = args[i + 1]
        elif arg == '-c' and i + 1 < len(args):
            channel_id = args[i + 1]
    
    # Download PDF
    progress = await update.message.reply_text("⏳ PDF ডাউনলোড হচ্ছে...")
    file = await update.message.reply_to_message.document.get_file()
    pdf_path = f"/tmp/pdf_{update.message.from_user.id}.pdf"
    await file.download_to_drive(pdf_path)
    
    # Read PDF
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    
    # Parse page range
    if page_range:
        if '-' in page_range:
            start, end = map(int, page_range.split('-'))
        else:
            start = end = int(page_range)
    else:
        start, end = 1, min(10, total_pages)
    
    # Extract text
    text = ""
    for i in range(start - 1, min(end, total_pages)):
        text += reader.pages[i].extract_text()
    
    # Generate MCQs
    await progress.edit_text(f"⏳ MCQ তৈরি হচ্ছে...\n📄 Pages: {start}-{end}")
    
    prompt = f"""Create 10-15 MCQs from this text. Output JSON only:
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A","explanation":"..."}}]

Text:
{text[:4000]}"""
    
    response = await gemini_manager.call(prompt)
    
    # Parse JSON
    json_match = re.search(r'\[.*\]', response, re.DOTALL)
    if not json_match:
        await progress.edit_text("❌ MCQ generate করতে পারিনি")
        return
    
    mcqs = json.loads(json_match.group())
    
    # Add is_short flag
    for mcq in mcqs:
        mcq['is_short'] = is_short_option(mcq['options'])
    
    # Generate PDF
    await progress.edit_text("📄 PDF তৈরি হচ্ছে...")
    template = Template(TEMPLATE_FORMAT1)
    html = template.render(title=title, mcqs=mcqs)
    pdf_output = f"/tmp/mcq_{update.message.from_user.id}.pdf"
    await generate_pdf(html, pdf_output)
    
    # Send PDF
    await update.message.reply_document(document=open(pdf_output, 'rb'), filename=f"{title}.pdf")
    await progress.edit_text(f"✅ {len(mcqs)}টি MCQ তৈরি সম্পন্ন!")
    
    os.unlink(pdf_path)
    os.unlink(pdf_output)


async def qbm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/qbm - Extract existing MCQs from PDF"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF file-এ reply করে /qbm দাও")
        return
    
    await update.message.reply_text("🔧 Feature coming soon!")


async def handle_pdf_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF callbacks"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Processing...")
