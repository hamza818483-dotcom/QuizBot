#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - All Services (Gemini, PDF, Pyrogram, Poll Collector)"""

import os
import re
import csv
import io
import json
import time
import base64
import random
import asyncio
import logging
import tempfile
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any

import aiohttp
import requests
import aiosqlite
from PIL import Image
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from email import policy
from email import message_from_bytes
from jinja2 import Template

from pypdf import PdfReader

load_dotenv()

logger = logging.getLogger(__name__)

def ocr_image(image_bytes: bytes, lang='eng+ben') -> str:
    """Extract text from image using Tesseract OCR with double pass"""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        # First pass - default
        text1 = pytesseract.image_to_string(img, lang=lang)
        # Second pass - with different config for better accuracy
        text2 = pytesseract.image_to_string(img, lang=lang, config='--psm 6')
        # Combine and return the longer/better result
        return text1 if len(text1) > len(text2) else text2
    except Exception as e:
        return ""


# ============================================================
# CONFIG LOADER
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
OWNER_ID = int(os.getenv('OWNER_ID', 0))
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
GEMINI_API_KEYS = [k.strip() for k in os.getenv('GEMINI_API_KEYS', '').replace('\n', ',').split(',') if k.strip()]
IMGBB_API_KEYS = [k.strip() for k in os.getenv('IMGBB_API_KEYS', '').split(',') if k.strip()]
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')

# ============================================================
# GEMINI KEY MANAGER
# ============================================================
class GeminiKeyManager:
    def __init__(self):
        self.keys = {k: {'success': 0, 'fail': 0, 'healthy': True} for k in GEMINI_API_KEYS}
    
    def get_healthy_key(self) -> str:
        healthy = [k for k, v in self.keys.items() if v.get('healthy', True)]
        if not healthy:
            for k in self.keys: self.keys[k]['healthy'] = True
            healthy = list(self.keys.keys())
        key = random.choice(healthy)
        # Quick test before returning
        try:
            import requests
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}'
            r = requests.post(url, json={'contents':[{'parts':[{'text':'test'}]}]}, timeout=5)
            if r.status_code != 200:
                self.keys[key]['healthy'] = False
                return self.get_healthy_key()  # Try another
        except:
            pass
        return key
    
    def record_success(self, key): 
        if key in self.keys: self.keys[key]['success'] += 1; self.keys[key]['healthy'] = True
    
    def record_failure(self, key):
        if key in self.keys:
            self.keys[key]['fail'] += 1
            if self.keys[key]['fail'] >= 3: self.keys[key]['healthy'] = False
    
    def get_stats(self):
        return {
            'total': len(self.keys),
            'healthy': len([k for k,v in self.keys.items() if v['healthy']]),
            'keys': {f'key_{i+1}': {'success': v['success'], 'fail': v['fail'], 'healthy': v['healthy']} 
                     for i, (k,v) in enumerate(self.keys.items())}
        }

gemini_key_mgr = GeminiKeyManager()

# ============================================================
# IMGBB KEY MANAGER
# ============================================================
class ImgBBKeyManager:
    def __init__(self):
        self.keys = IMGBB_API_KEYS
        self.index = 0
    
    def upload(self, image_bytes: bytes, retries: int = 3) -> str:
        b64 = base64.b64encode(image_bytes).decode('utf-8')
        for _ in range(retries):
            key = self.keys[self.index % len(self.keys)]
            self.index += 1
            try:
                resp = requests.post('https://api.imgbb.com/1/upload', 
                                     data={'key': key, 'image': b64}, timeout=30)
                if resp.json().get('success'): return resp.json()['data']['url']
            except: pass
        return ""

imgbb_mgr = ImgBBKeyManager()

# ============================================================
# GEMINI AI SERVICE
# ============================================================
async def generate_mcqs_from_image(image_bytes: bytes, active_prompts: List[str], 
                                    count: int = 12) -> List[Dict]:
    """Generate MCQs from image using active prompts"""
    prompt_text = "\n\n".join(active_prompts)
    full_prompt = f"""{prompt_text}

Generate the MAXIMUM POSSIBLE number of MCQs from this image. Maintain high quality. Do NOT create irrelevant questions. Only use information present in the source.
Follow ALL rules from the prompts above.
Output ONLY valid JSON array:
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A/B/C/D","explanation":"... (max 165 chars Bengali)"}}]"""
    
    response = call_gemini(full_prompt, image_bytes)
    return parse_mcq_json(response)

async def generate_mcqs_from_text(text: str, active_prompts: List[str], 
                                   count: int = 12) -> List[Dict]:
    """Generate MCQs from text using active prompts"""
    prompt_text = "\n\n".join(active_prompts)
    full_prompt = f"""{prompt_text}

Generate the MAXIMUM POSSIBLE number of MCQs from this text. Use EVERY LINE as source. Maintain high quality. Do NOT create irrelevant questions.
Follow ALL rules from the prompts above.
Output ONLY valid JSON array:
[{{"question":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"A/B/C/D","explanation":"... (max 165 chars Bengali)"}}]

TEXT:
{text[:4000]}"""
    
    response = call_gemini(full_prompt)
    return parse_mcq_json(response)

def parse_mcq_json(response: str) -> List[Dict]:
    """Parse Gemini JSON response to MCQ list"""
    try:
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if json_match:
            mcqs = json.loads(json_match.group())
            # Convert answer A/B/C/D to 1/2/3/4
            for mcq in mcqs:
                ans = mcq.get('answer', 'A').upper()
                mcq['answer'] = {'A': '1', 'B': '2', 'C': '3', 'D': '4'}.get(ans, '1')
            return mcqs
    except: pass
    return []

# ============================================================
# CSV HELPER
# ============================================================
def mcqs_to_csv(mcqs: List[Dict]) -> bytes:
    """Convert MCQ list to CSV bytes"""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        'questions', 'option1', 'option2', 'option3', 'option4', 
        'answer', 'explanation', 'type', 'section'
    ])
    writer.writeheader()
    for mcq in mcqs:
        writer.writerow({
            'questions': mcq.get('question', ''),
            'option1': mcq.get('options', {}).get('A', ''),
            'option2': mcq.get('options', {}).get('B', ''),
            'option3': mcq.get('options', {}).get('C', ''),
            'option4': mcq.get('options', {}).get('D', ''),
            'answer': mcq.get('answer', '1'),
            'explanation': mcq.get('explanation', '')[:200],
            'type': '1',
            'section': '1'
        })
    return output.getvalue().encode('utf-8-sig')

def parse_csv_to_mcqs(content: str) -> List[Dict]:
    """Parse CSV content to MCQ list"""
    mcqs = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        mcqs.append({
            'question': row.get('questions', row.get('question', '')),
            'options': {
                'A': row.get('option1', ''),
                'B': row.get('option2', ''),
                'C': row.get('option3', ''),
                'D': row.get('option4', '')
            },
            'answer': row.get('answer', '1'),
            'explanation': row.get('explanation', '')
        })
    return mcqs

# ============================================================
# PROGRESS HELPER
# ============================================================
def format_progress(current: int, total: int, prefix: str = "প্রসেসিং") -> str:
    """Format progress message"""
    pct = int((current / total) * 100) if total > 0 else 0
    bar_len = 10
    filled = int(bar_len * current / total) if total > 0 else 0
    bar = '█' * filled + '░' * (bar_len - filled)
    return f"{prefix}\n[{bar}] {pct}%\n📊 {current}/{total}"

# ============================================================
# PDF PROCESSOR
# ============================================================
class PDFProcessor:
    @staticmethod
    def get_page_count(pdf_path: str) -> int:
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            return len(reader.pages)
        except: return 0
    
    @staticmethod
    def pdf_to_images(pdf_path: str, start: int = 1, end: int = 10) -> list:
        """Convert PDF pages to images using pdf2image"""
        try:
            from pdf2image import convert_from_path
            images = []
            pages = convert_from_path(pdf_path, first_page=start, last_page=min(end, 999))
            for i, img in enumerate(pages):
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=85)
                img_bytes = buf.getvalue()
                if isinstance(img_bytes, tuple):
                    img_bytes = img_bytes[0]
                images.append((start + i, img_bytes))
            # If no images (scanned PDF), try OCR text extraction

# ============================================================
            return images
        except Exception as e:
            logger.error(f"PDF to images error: {e}")
            return []
# ASYNC PDF EXPORTER (Playwright)
# ============================================================
class AsyncPDFExporter:
    _playwright = None
    _browser = None
    
    @classmethod
    async def get_browser(cls):
        if cls._browser is None:
            cls._playwright = await async_playwright().start()
            cls._browser = await cls._playwright.chromium.launch(headless=True)
        return cls._browser
    
    @classmethod
    async def html_to_pdf(cls, html: str, output_path: str) -> bool:
        try:
            browser = await cls.get_browser()
            page = await browser.new_page()
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(html)
                temp_path = f.name
            
            await page.goto(f"file://{os.path.abspath(temp_path)}", wait_until='networkidle')
            await page.evaluate("document.fonts.ready")
            await asyncio.sleep(3)
            
            await page.pdf(path=output_path, format='A4', 
                           margin={'top': '10mm', 'bottom': '10mm', 'left': '10mm', 'right': '10mm'},
                           print_background=True)
            await page.close()
            os.unlink(temp_path)
            return True
        except Exception as e:
            logger.error(f"PDF export error: {e}")
            return False

# ============================================================
# LARGE PDF HANDLER (Pyrogram)
# ============================================================
class LargePDFHandler:
    _client = None
    
    @classmethod
    async def get_pyrogram_client(cls):
        if cls._client is None and API_ID and API_HASH:
            from pyrogram import Client
            cls._client = Client("atlas_pyrogram", api_id=int(API_ID), 
                                 api_hash=API_HASH, bot_token=TELEGRAM_BOT_TOKEN, no_updates=True)
            await cls._client.start()
        return cls._client
    
    @classmethod
    async def download_large_file(cls, chat_id: int, message_id: int) -> Optional[str]:
        try:
            client = await cls.get_pyrogram_client()
            if not client: return None
            msg = await client.get_messages(chat_id, message_id)
            if msg and msg.document:
                path = await client.download_media(msg)
                return path
        except Exception as e:
            logger.error(f"Pyrogram download error: {e}")
        return None

# ============================================================
# POLL COLLECTOR
# ============================================================
class PollCollector:
    def __init__(self):
        self.sessions: Dict[int, Dict] = {}
    
    def start(self, user_id: int):
        self.sessions[user_id] = {'polls': [], 'collecting': True}
    
    def add_poll(self, user_id: int, poll_data: Dict):
        if user_id in self.sessions and self.sessions[user_id]['collecting']:
            self.sessions[user_id]['polls'].append(poll_data)
    
    def get_count(self, user_id: int) -> int:
        return len(self.sessions.get(user_id, {}).get('polls', []))
    
    def finish(self, user_id: int) -> List[Dict]:
        polls = self.sessions.get(user_id, {}).get('polls', [])
        if user_id in self.sessions: del self.sessions[user_id]
        return polls
    
    def cancel(self, user_id: int):
        if user_id in self.sessions: del self.sessions[user_id]

poll_collector = PollCollector()

# ============================================================
# HTML/MHTML PARSER
# ============================================================
def convert_bengali_numbers(text: str) -> str:
    return text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))

def compress_image_b64(b64_str: str) -> str:
    try:
        img_data = base64.b64decode(b64_str)
        img = Image.open(io.BytesIO(img_data))
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", optimize=True, quality=70)
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    except: return b64_str

def clean_html_text(text: str) -> str:
    if not text: return ""
    text = convert_bengali_numbers(text)
    text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def parse_mhtml_to_mcqs(file_bytes: bytes, filename: str) -> List[Dict]:
    """Parse MHTML/HTML file to MCQ list"""
    img_map = {}
    html_body = ""
    
    if filename.endswith('.mhtml') or filename.endswith('.mht'):
        msg = message_from_bytes(file_bytes, policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
            elif part.get_content_type().startswith('image/'):
                loc = part.get('Content-Location', '')
                raw = part.get_payload(decode=True)
                if loc and raw:
                    img_map[loc] = base64.b64encode(raw).decode('utf-8')
    else:
        html_body = file_bytes.decode('utf-8', errors='ignore')
    
    soup = BeautifulSoup(html_body, 'html.parser')
    results = []
    
    # Try Chorcha.net
    cards = soup.find_all('div', class_=lambda x: x and 'p-5' in x and 'rounded-xl' in x)
    if not cards:
        # Try TestMoz
        cards = soup.find_all('div', class_=lambda x: x and 'rounded-lg' in x and 'shadow-md' in x)
    
    for card in cards:
        q_text = ""
        options = []
        ans_idx = "1"
        exp_text = ""
        
        # Extract question
        q_div = card.find('p') or card.find('div', class_=lambda x: x and 'font-medium' in x if x else False)
        if q_div:
            q_text = clean_html_text(q_div.get_text())
            q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*', '', q_text)
        
        # Extract options
        for btn in card.find_all(['button', 'div'], class_=lambda x: x and ('cursor-pointer' in x or 'p-2' in x) if x else False):
            opt_text = clean_html_text(btn.get_text())
            if opt_text and len(opt_text) > 1:
                options.append(opt_text)
        
        while len(options) < 4: options.append("")
        
        # Extract answer
        for i, btn in enumerate(card.find_all(['button', 'div'])):
            btn_str = str(btn)
            if any(c in btn_str for c in ['bg-green-500', '#017A47', 'border-[#017A47]', 'border-[#F59E0B]']):
                ans_idx = str(i + 1) if i < 4 else "1"
                break
        
        # Extract explanation
        exp_div = card.find('div', class_=lambda x: x and ('prose' in x or 'font-semibold' in x) if x else False)
        if exp_div:
            exp_text = clean_html_text(exp_div.get_text())[:200]
        
        if q_text:
            results.append({
                'questions': q_text,
                'option1': options[0] if len(options) > 0 else '',
                'option2': options[1] if len(options) > 1 else '',
                'option3': options[2] if len(options) > 2 else '',
                'option4': options[3] if len(options) > 3 else '',
                'answer': ans_idx,
                'explanation': exp_text,
                'type': '1',
                'section': '1'
            })
    
    return results

# ============================================================
# SHEET TEMPLATES
# ============================================================
SHEET_TEMPLATES = {
    'format_01': '''<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:15mm;line-height:1.8;background:#fff}
h1{text-align:center;color:#1B4F72;margin-bottom:10mm;font-size:24pt;border-bottom:3px solid #3498db;padding-bottom:5mm}
.mcq{margin-bottom:8mm;page-break-inside:avoid;border:1px solid #FBBF24;border-radius:5px;padding:4mm;background:#FFFBF0}
.question{font-weight:700;font-size:11pt;margin-bottom:2mm;color:#34495e}
.options{margin-left:6mm;display:grid;grid-template-columns:1fr 1fr;gap:2mm}
.option{padding:2mm;background:#fff;border:1px solid #D1D5DB;border-radius:4px;font-size:10pt}
.answer{margin-top:3mm;padding:2mm;background:#DCFCE7;border-left:4px solid #4ADE80;font-size:9pt;color:#14532D}
.exp{margin-top:2mm;padding:2mm;background:#EFF6FF;border-left:4px solid #4299E1;font-size:9pt;color:#1E40AF}
</style></head><body data-ready="true">
<h1>{{ title }}</h1>
{% for mcq in mcqs %}
<div class="mcq">
<p class="question">{{ loop.index }}. {{ mcq.question }}</p>
<div class="options">
{% for key, val in mcq.options.items() %}
<div class="option">{{ key }}. {{ val }}</div>
{% endfor %}
</div>
<div class="answer"><strong>উত্তর:</strong> {{ mcq.answer }}</div>
{% if mcq.explanation %}<div class="exp">{{ mcq.explanation }}</div>{% endif %}
</div>
{% endfor %}
<script>document.body.setAttribute('data-ready','true')</script>
</body></html>''',

    'format_02': '''<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:15mm;line-height:1.8}
h1{text-align:center;color:#1B4F72;margin-bottom:5mm;font-size:24pt}
h2{text-align:center;color:#e74c3c;margin:10mm 0;font-size:18pt;page-break-before:always}
.mcq{margin-bottom:8mm;page-break-inside:avoid}
.question{font-weight:700;font-size:11pt;margin-bottom:2mm}
.options{margin-left:6mm}
.option{margin:1.5mm 0;font-size:10pt}
.answer-page{margin-top:10mm}
.answer-item{margin-bottom:5mm;padding:4mm;background:#FFF3E0;border-left:4px solid #FF9800}
</style></head><body data-ready="true">
<h1>{{ title }}</h1>
<h2>প্রশ্নপত্র</h2>
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
<h2>উত্তরপত্র</h2>
<div class="answer-page">
{% for mcq in mcqs %}
<div class="answer-item"><strong>{{ loop.index }}.</strong> {{ mcq.answer }}{% if mcq.explanation %} — {{ mcq.explanation }}{% endif %}</div>
{% endfor %}
</div>
<script>document.body.setAttribute('data-ready','true')</script>
</body></html>''',

    'format_03': '''<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:12mm;line-height:1.6}
h1{text-align:center;color:#1B4F72;margin-bottom:8mm;font-size:22pt}
.mcq{margin-bottom:6mm;page-break-inside:avoid}
.question{font-weight:700;font-size:10pt;margin-bottom:1.5mm}
.options{margin-left:5mm;display:grid;grid-template-columns:1fr 1fr;gap:1.5mm}
.option{font-size:9pt}
.answer-table{width:100%;border-collapse:collapse;margin-top:5mm;font-size:9pt}
.answer-table th,.answer-table td{border:1px solid #555;padding:2mm;text-align:center}
.answer-table th{background:#EFF6FF}
</style></head><body data-ready="true">
<h1>{{ title }}</h1>
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
<div class="answer-table">
<h3>📋 Answer Key</h3>
<table><tr><th>Q.No</th><th>Ans</th></tr>
{% for mcq in mcqs %}
<tr><td>{{ loop.index }}</td><td><b>{{ mcq.answer }}</b></td></tr>
{% endfor %}
</table></div>
<script>document.body.setAttribute('data-ready','true')</script>
</body></html>''',

    'format_04': '''<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:12mm;line-height:1.6}
h1{text-align:center;color:#1B4F72;margin-bottom:8mm;font-size:22pt}
.mcq{margin-bottom:6mm;page-break-inside:avoid;border:1px solid #D1D5DB;border-radius:4px;padding:3mm}
.question{font-weight:700;font-size:10pt;margin-bottom:1.5mm}
.options{margin-left:5mm}
.option{margin:1mm 0;font-size:9pt}
.ans-inline{font-weight:700;color:#14532D;margin-left:3mm}
</style></head><body data-ready="true">
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
</body></html>''',

    'format_05': '''<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Noto Sans Bengali',sans-serif;padding:12mm;line-height:1.6}
h1{text-align:center;color:#1B4F72;margin-bottom:8mm;font-size:22pt}
.answer-key{display:grid;grid-template-columns:repeat(5,1fr);gap:3mm}
.answer-item{padding:3mm;background:#ecf0f1;border-radius:3mm;text-align:center;font-size:10pt}
.answer-item strong{color:#e74c3c;font-size:13pt}
table{width:100%;border-collapse:collapse;margin-top:8mm;font-size:9pt}
th,td{border:1px solid #555;padding:2mm;text-align:center}
th{background:#EFF6FF}
</style></head><body data-ready="true">
<h1>{{ title }} - Answer Key</h1>
<div class="answer-key">
{% for mcq in mcqs %}
<div class="answer-item">{{ loop.index }}. <strong>{{ mcq.answer }}</strong></div>
{% endfor %}
</div>
<table style="margin-top:10mm"><tr><th>Q.No</th><th>Ans</th><th>ব্যাখ্যা</th></tr>
{% for mcq in mcqs %}
<tr><td>{{ loop.index }}</td><td><b>{{ mcq.answer }}</b></td><td>{{ mcq.explanation[:100] }}</td></tr>
{% endfor %}
</table>
<script>document.body.setAttribute('data-ready','true')</script>
</body></html>'''
}

# ============================================================
# WATERMARK PDF
# ============================================================
def add_watermark_to_pdf(pdf_bytes: bytes, watermark_text: str) -> bytes:
    """Add watermark to PDF"""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        # Simple text watermark (full implementation with Playwright for rotated text)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except:
        return pdf_bytes

from gemini_api import call_gemini
pdf_processor = PDFProcessor()
