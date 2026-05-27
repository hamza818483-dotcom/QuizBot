#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - MHTML/HTML to CSV Handler"""

import os
import re
import csv
import io
import time
import base64
import asyncio
import logging
from email import policy
from email import message_from_bytes
from bs4 import BeautifulSoup
from PIL import Image
import urllib.parse
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db
from services import imgbb_mgr

logger = logging.getLogger(__name__)

# ============================================================
# BENGALI NUMBER CONVERSION
# ============================================================
def convert_bengali_numbers(text: str) -> str:
    """Convert Bengali digits to English (১→1)"""
    if not text:
        return text
    return text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


# ============================================================
# IMAGE COMPRESSION
# ============================================================
def compress_image_b64(b64_str: str) -> str:
    """Compress base64 image to JPEG 70% quality"""
    try:
        img_data = base64.b64decode(b64_str)
        img = Image.open(io.BytesIO(img_data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", optimize=True, quality=70)
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception:
        return b64_str


# ============================================================
# IMGBB UPLOAD (with 7-key rotation)
# ============================================================
def upload_image_to_imgbb(b64_str: str) -> str:
    """Upload base64 image to ImgBB with compression and key rotation"""
    if not b64_str:
        return ""
    
    compressed = compress_image_b64(b64_str)
    try:
        img_bytes = base64.b64decode(compressed)
        url = imgbb_mgr.upload(img_bytes)
        return url if url else ""
    except Exception:
        return ""


# ============================================================
# AGGRESSIVE TEXT CLEANING
# ============================================================
def clean_html_text(text: str) -> str:
    """Clean text with formula fixes"""
    if not text:
        return ""
    
    # 1. Bengali → English numbers
    text = convert_bengali_numbers(text)
    
    # 2. Fractions: \frac{a}{b} → a/b
    text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
    
    # 3. Subscript conversion (H_2O → H₂O)
    text = re.sub(r'_\{\s*([^}]+)\s*\}', r'_\1', text)
    text = re.sub(r'_([0-9a-zA-Z+-]+)',
                  lambda m: m.group(1).translate(
                      str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
                  ), text)
    
    # 4. Superscript conversion (x^2 → x²)
    text = re.sub(r'\^\{\s*([^}]+)\s*\}', r'^\1', text)
    text = re.sub(r'\^([0-9a-zA-Z+-]+)',
                  lambda m: m.group(1).translate(
                      str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₌⁽⁾")
                  ), text)
    
    # 5. Degree symbol fix (^\circ → °)
    text = re.sub(r'\^\\circ', '°', text)
    text = re.sub(r'\^\{?\\circ\}?', '°', text)
    text = text.replace('∘', '°').replace('° C', '°C')
    
    # 6. Chemical spacing fix (Cu ²⁺ → Cu²⁺)
    text = re.sub(r'\s+([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+)', r'\1', text)
    
    # 7. Subscript letter fix (NₐHCO₃ → NaHCO₃)
    sub_chars = str.maketrans("ₐₑₒₓₕₖₗₘₙₚₛₜ", "aeoxhklmnpst")
    text = text.translate(sub_chars)
    
    # 8. Bracket fix for polymers
    text = text.replace('₍', '(').replace('₎', ')')
    
    # 9. Unit spacing (10mL → 10 mL, 5kg → 5 kg)
    units = r'(mL|L|m³|cm³|g|kg|mol|M|Pa|atm|J|K|V|A|W|N|C|Hz|eV|nm|mm|cm|m)'
    text = re.sub(r'(\d+)\s*' + units + r'\b', r'\1 \2', text)
    
    # 10. Remove LaTeX commands (\text{}, \mathrm{})
    text = re.sub(r'\\[a-zA-Z]+\s*\{?', ' ', text)
    
    # 11. Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # 12. HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&nbsp;', ' ').replace('&quot;', '"')
    
    # 13. Clean extra whitespace
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


# ============================================================
# FORMAT HTML CONTENT (with image embedding)
# ============================================================
def format_html_element(element, img_map: dict) -> str:
    """Format HTML element to clean text with <img> tags for images"""
    if not element:
        return ""
    
    # Remove hidden math elements
    for hidden in element.find_all(['annotation', 'script', 'mjx-assistive-mathml', 'style']):
        hidden.decompose()
    
    # Process fractions (mfrac → a/b)
    for mfrac in element.find_all('mfrac'):
        contents = mfrac.find_all(recursive=False)
        if len(contents) == 2:
            num = contents[0].get_text(strip=True)
            den = contents[1].get_text(strip=True)
            mfrac.replace_with(f"{num}/{den}")
    
    # Process subscripts (sub → Unicode subscript)
    sub_map = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
    for sub in element.find_all(['sub', 'msub']):
        sub.replace_with(sub.get_text(strip=True).translate(sub_map))
    
    # Process superscripts (sup → Unicode superscript)
    sup_map = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₌⁽⁾")
    for sup in element.find_all(['sup', 'msup']):
        sup.replace_with(sup.get_text(strip=True).translate(sup_map))
    
    # Process images (upload to ImgBB)
    for img in element.find_all('img'):
        src = img.get('src', '') or img.get('data-src', '')
        if not src:
            img.decompose()
            continue
        
        url = ""
        b64 = ""
        
        if src.startswith('http'):
            url = src  # Already a URL, use directly
        elif src.startswith('data:image'):
            try:
                if 'base64,' in src:
                    b64 = src.split('base64,')[1]
            except:
                pass
        else:
            # Check if in image map (MHTML embedded images)
            decoded_src = urllib.parse.unquote(src)
            b64 = img_map.get(src) or img_map.get(decoded_src) or ""
        
        # Upload to ImgBB if base64
        if b64 and not url:
            url = upload_image_to_imgbb(b64)
        
        if url:
            img.replace_with(f" IMGSTART{url}IMGEND ")
        else:
            img.decompose()
    
    # Get raw text
    raw_text = element.get_text(separator=" ", strip=True)
    
    # Preserve image markers
    img_markers = []
    def img_repl(match):
        img_markers.append(match.group(0))
        return f" ZZIMG{len(img_markers)-1}ZZ "
    
    raw_text = re.sub(r'IMGSTART.*?IMGEND', img_repl, raw_text)
    
    # Clean text
    cleaned = clean_html_text(raw_text)
    
    # Restore image markers as HTML img tags
    for i, marker in enumerate(img_markers):
        cleaned = cleaned.replace(f"ZZIMG{i}ZZ", marker)
    
    # Convert markers to proper CSV-safe format
    cleaned = re.sub(r'IMGSTART(.*?)IMGEND', r'<img class="qimg" src="\1">', cleaned)
    
    return cleaned


# ============================================================
# PARSE MHTML FILE (Extract HTML + Embedded Images)
# ============================================================
def parse_mhtml_to_parts(file_bytes: bytes, filename: str) -> tuple:
    """
    Parse MHTML or HTML file and return:
    - html_body: the HTML content
    - img_map: mapping of Content-Location → base64 image data
    """
    img_map = {}
    html_body = ""
    
    if filename.lower().endswith(('.mhtml', '.mht')):
        # Parse MHTML using email library
        msg = message_from_bytes(file_bytes, policy=policy.default)
        
        for part in msg.walk():
            content_type = part.get_content_type()
            
            if content_type == 'text/html':
                html_body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or 'utf-8', errors='ignore'
                )
            elif content_type.startswith('image/'):
                loc = part.get('Content-Location', '')
                raw = part.get_payload(decode=True)
                if loc and raw:
                    b64_data = base64.b64encode(raw).decode('utf-8')
                    img_map[loc] = b64_data
                    img_map[urllib.parse.unquote(loc)] = b64_data
    else:
        # Plain HTML file
        html_body = file_bytes.decode('utf-8', errors='ignore')
    
    return html_body, img_map


# ============================================================
# EXTRACT MCQs FROM CHORCHA.NET
# ============================================================
def extract_chorcha_mcqs(soup, img_map: dict, status_callback=None) -> list:
    """
    Extract MCQs from Chorcha.net format.
    Key: Button color/border indicates correct answer.
    """
    cards = soup.find_all('div', class_=lambda x: x and 'p-5' in x and 'rounded-xl' in x)
    
    if not cards:
        return []
    
    results = []
    total = len(cards)
    start_time = time.time()
    
    for idx, card in enumerate(cards, 1):
        # Extract question
        q_div = card.find('div', class_=lambda x: x and 'font-medium' in x if x else False)
        if not q_div:
            continue
        
        q_text = format_html_element(q_div, img_map)
        # Remove question number prefix (1. / ১. etc.)
        q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*', '', q_text)
        
        # Extract options
        options = []
        ans_idx = "1"
        ans_map = {'ক': '1', 'খ': '2', 'গ': '3', 'ঘ': '4'}
        
        for i, btn in enumerate(card.find_all('button', class_=lambda x: x and 'p-2' in x if x else False), 1):
            lbl = btn.find('span', class_=lambda x: x and 'rounded-full' in x if x else False)
            opt_content = btn.find('div', class_='flex-1')
            
            if opt_content:
                options.append(format_html_element(opt_content, img_map))
                
                # Detect correct answer from button color
                btn_html = str(btn)
                if any(color in btn_html for color in ['#017A47', 'border-[#017A47]', '#E2A03F', '#F59E0B', 'border-[#F59E0B]']):
                    lbl_text = lbl.get_text(strip=True) if lbl else ""
                    ans_idx = ans_map.get(lbl_text, str(i))
        
        # Pad options to 5 (option5 always empty)
        while len(options) < 5:
            options.append("")
        
        # If option5 has content and is answer, swap with option4
        if len(options) > 4 and options[4].strip() and ans_idx == "5":
            options[3], options[4] = options[4], options[3]
            ans_idx = "4"
        
        # Extract explanation
        exp_div = card.find('div', class_=lambda x: x and 'prose' in x if x else False)
        exp_text = format_html_element(exp_div, img_map) if exp_div else ""
        exp_text = exp_text[:200]  # Limit to 200 chars
        
        # Add to results
        results.append({
            'questions': q_text,
            'option1': options[0] if len(options) > 0 else '',
            'option2': options[1] if len(options) > 1 else '',
            'option3': options[2] if len(options) > 2 else '',
            'option4': options[3] if len(options) > 3 else '',
            'option5': '',  # Always empty
            'answer': ans_idx,
            'explanation': exp_text,
            'type': '1',
            'section': '1'
        })
        
        # Progress update
        if status_callback and (idx % 10 == 0 or idx == total):
            status_callback(idx, total)
    
    return results


# ============================================================
# EXTRACT MCQs FROM TESTMOZ
# ============================================================
def extract_testmoz_mcqs(soup, img_map: dict, status_callback=None) -> list:
    """
    Extract MCQs from TestMoz-style sites.
    Key: bg-green-500 class or SVG icon indicates correct answer.
    """
    cards = soup.find_all('div', class_=lambda x: x and 'rounded-lg' in x and 'shadow-md' in x if x else False)
    
    if not cards:
        return []
    
    results = []
    total = len(cards)
    
    for idx, card in enumerate(cards, 1):
        # Extract question
        q_p = card.find('p', class_='text-[17px]')
        q_text = format_html_element(q_p, img_map) if q_p else ""
        q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*', '', q_text)
        
        # Extract options
        opt_divs = card.find_all('div', class_=lambda x: x and 'cursor-pointer' in x and 'col-span-2' in x if x else False)
        
        options = []
        ans_idx = "1"
        
        for i, opt in enumerate(opt_divs, 1):
            text_sm = opt.find('div', class_='text-sm')
            opt_text = format_html_element(text_sm, img_map) if text_sm else ""
            
            # Check for images inside option
            for img in opt.find_all('img'):
                if text_sm and img not in text_sm.descendants:
                    from bs4 import BeautifulSoup as BS
                    dummy = BS(str(img), 'html.parser')
                    opt_text += " " + format_html_element(dummy, img_map)
            
            options.append(opt_text)
            
            # Detect correct answer
            if opt.find('div', class_=lambda x: x and 'bg-green-500' in x if x else False) or opt.find('svg'):
                ans_idx = str(i)
        
        # Pad options
        while len(options) < 5:
            options.append("")
        if len(options) > 4 and options[4].strip() and ans_idx == "5":
            options[3], options[4] = options[4], options[3]
            ans_idx = "4"
        
        # Extract explanation
        exp_div = card.find('div', class_=lambda x: x and 'col-span-2' in x and 'font-semibold' in x and 'cursor-pointer' not in x if x else False)
        exp_text = format_html_element(exp_div, img_map) if exp_div else ""
        exp_text = exp_text[:200]
        
        results.append({
            'questions': q_text,
            'option1': options[0] if len(options) > 0 else '',
            'option2': options[1] if len(options) > 1 else '',
            'option3': options[2] if len(options) > 2 else '',
            'option4': options[3] if len(options) > 3 else '',
            'option5': '',
            'answer': ans_idx,
            'explanation': exp_text,
            'type': '1',
            'section': '1'
        })
        
        if status_callback and (idx % 10 == 0 or idx == total):
            status_callback(idx, total)
    
    return results


# ============================================================
# CSV EXPORT
# ============================================================
def mcqs_to_csv_bytes(results: list) -> bytes:
    """Convert MCQ results list to CSV bytes with proper columns"""
    output = io.StringIO()
    fieldnames = ['questions', 'option1', 'option2', 'option3', 'option4', 
                  'option5', 'answer', 'explanation', 'type', 'section']
    writer = csv.DictWriter(output, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    
    for row in results:
        # Ensure all fields exist
        for field in fieldnames:
            if field not in row:
                row[field] = ''
        writer.writerow(row)
    
    return output.getvalue().encode('utf-8-sig')


# ============================================================
# MAIN MHTML HANDLER
# ============================================================
async def mhtml_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Auto-detect .mhtml/.html files and extract MCQs to CSV.
    Supports: Chorcha.net and TestMoz formats.
    """
    doc = update.message.document
    if not doc:
        return
    
    filename = doc.file_name or ""
    
    # Check file extension
    if not filename.lower().endswith(('.mhtml', '.mht', '.html')):
        return
    
    user_id = update.effective_user.id
    
    # Only work in private chat
    if update.effective_chat.type != 'private':
        await update.message.reply_text("⚠️ MHTML/HTML এক্সট্রাকশন শুধু Private Chat-এ কাজ করে!")
        return
    
    # Start status message
    status_msg = await update.message.reply_text(
        f"🚀 *ATLAS Extractor Started*\n📁 File: `{filename}`\n⏳ Downloading...",
        parse_mode=None
    )
    
    start_time = time.time()
    
    try:
        # Download file with progress tracking
        file = await doc.get_file()
        file_size = doc.file_size or 0
        
        # Progress callback for download
        last_update = [0]
        
        async def download_progress(current, total):
            now = time.time()
            if now - last_update[0] >= 3:  # Update every 3 seconds
                last_update[0] = now
                pct = int((current / total) * 100) if total > 0 else 0
                speed = current / (now - start_time + 1)
                eta = (total - current) / speed if speed > 0 else 0
                
                current_mb = current / (1024 * 1024)
                total_mb = total / (1024 * 1024)
                
                try:
                    await status_msg.edit_text(
                        f"📥 *Downloading:* `{filename}`\n"
                        f"📊 Size: `{current_mb:.1f}/{total_mb:.1f} MB`\n"
                        f"🚀 Speed: `{speed/1024:.1f} MB/s`\n"
                        f"⏳ ETA: `{int(eta//60):02d}:{int(eta%60):02d}`\n"
                        f"[{'█'*int(pct/10)}{'░'*(10-int(pct/10))}] {pct}%",
                        parse_mode=None
                    )
                except:
                    pass
        
        file_bytes = await file.download_as_bytearray()
        if isinstance(file_bytes, bytearray):
            file_bytes = bytes(file_bytes)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Download Error: {str(e)[:100]}")
        return
    
    # Parse MHTML/HTML
    await status_msg.edit_text("🔍 *Parsing file structure...*", parse_mode=None)
    html_body, img_map = parse_mhtml_to_parts(file_bytes, filename)
    
    if not html_body:
        await status_msg.edit_text("❌ ফাইলে কোনো HTML কন্টেন্ট পাওয়া যায়নি!")
        return
    
    # Parse with BeautifulSoup
    soup = BeautifulSoup(html_body, 'html.parser')
    
    # Try Chorcha.net first
    await status_msg.edit_text("🔍 *Scanning for Chorcha.net format...*", parse_mode=None)
    
    # Status callback for progress
    async def update_status(current, total):
        now = time.time()
        elapsed = now - start_time
        eta = (elapsed / current) * (total - current) if current > 0 else 0
        try:
            await status_msg.edit_text(
                f"⌛ *ATLAS Extractor*\n"
                f"📝 MCQ: `{current}/{total}`\n"
                f"⏳ ETA: `{int(eta//60):02d}:{int(eta%60):02d}`",
                parse_mode=None
            )
        except:
            pass
    
    # Make sync callback async-compatible
    def sync_callback(current, total):
        asyncio.create_task(update_status(current, total))
    
    # Extract MCQs
    results = extract_chorcha_mcqs(soup, img_map, sync_callback)
    
    if results:
        source = "Chorcha.net"
    else:
        # Try TestMoz
        await status_msg.edit_text("🔍 *Chorcha.net পাওয়া যায়নি। TestMoz চেষ্টা...*", parse_mode=None)
        results = extract_testmoz_mcqs(soup, img_map, sync_callback)
        source = "TestMoz" if results else "Unknown"
    
    if not results:
        await status_msg.edit_text("❌ *কোনো MCQ পাওয়া যায়নি!*\n\n✅ Supported: Chorcha.net & TestMoz", parse_mode=None)
        return
    
    # Generate CSV
    await status_msg.edit_text("📊 *CSV ফাইল তৈরি হচ্ছে...*", parse_mode=None)
    csv_bytes = mcqs_to_csv_bytes(results)
    
    # Get thumbnail
    thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
    thumb = thumb_row[0] if thumb_row else None
    
    # Send CSV
    await status_msg.delete()
    
    output_name = filename.rsplit('.', 1)[0] + '.csv'
    
    await update.message.reply_document(
        document=csv_bytes,
        filename=f"ATLAS_{output_name}",
        caption=f"""✅ *Extraction Complete!*

📁 Source: `{filename}`
🎯 Format: {source}
📊 Total MCQs: `{len(results)}`
⏱️ Time: `{int(time.time() - start_time)}s`

💡 This CSV can be used directly with:
• `/csv` — Send as Poll
• `/sheet` — Generate Practice Sheet""",
        parse_mode=None,
        thumbnail=thumb
    )
    
    logger.info(f"MHTML extraction complete: {filename} → {len(results)} MCQs ({source})")


# ============================================================
# QUEUE SYSTEM (for multiple files)
# ============================================================
_processing_queue = asyncio.Queue(maxsize=10)
_is_processing = False


async def mhtml_worker():
    """Background worker to process MHTML files serially"""
    global _is_processing
    
    while True:
        update, context = await _processing_queue.get()
        _is_processing = True
        
        try:
            await mhtml_handler(update, context)
        except Exception as e:
            logger.error(f"MHTML worker error: {e}")
            try:
                await update.message.reply_text(f"❌ Processing Error: {str(e)[:100]}")
            except:
                pass
        


async def queue_mhtml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add MHTML file to processing queue"""
    pos = _processing_queue.qsize()
    
    if pos >= 10:
        await update.message.reply_text("⚠️ সারি ভর্তি! পরে আবার চেষ্টা করো।")
        return
    
    await _processing_queue.put((update, context))
    
    if pos > 0:
        await update.message.reply_text(f"📥 Queue Position: {pos + 1}\n⏳ অনুগ্রহ করে অপেক্ষা করো...")
