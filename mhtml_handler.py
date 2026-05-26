#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MHTML/HTML Handler - FULL FEATURES
Includes: ImgBB upload, Formula fix, TestMoz+Chorcha support, Progress tracking
"""

import re
import csv
import io
import email
import time
import base64
import asyncio
import urllib.parse
from email import policy
from bs4 import BeautifulSoup
from PIL import Image
from telegram import Update
from telegram.ext import ContextTypes
from config import imgbb_manager, db

# Bengali number conversion
def convert_to_english_numbers(text):
    """Convert Bengali digits to English"""
    if not text:
        return text
    return text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))

# Image compression
def compress_image(b64_str):
    """Compress base64 image"""
    try:
        img_data = base64.b64decode(b64_str)
        img = Image.open(io.BytesIO(img_data))
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        out_buffer = io.BytesIO()
        img.save(out_buffer, format="JPEG", optimize=True, quality=70)
        return base64.b64encode(out_buffer.getvalue()).decode('utf-8')
    except:
        return b64_str

# Upload to ImgBB
def upload_to_imgbb(b64_str):
    """Upload base64 image to ImgBB"""
    if not b64_str:
        return ""
    
    compressed = compress_image(b64_str)
    
    try:
        # Use existing imgbb_manager from config
        img_bytes = base64.b64decode(compressed)
        url = imgbb_manager.upload(img_bytes)
        return url
    except:
        return ""

# Aggressive text cleaning with formula fixes
def aggressive_clean(text):
    """Clean text with formula conversion"""
    if not text:
        return ""
    
    text = convert_to_english_numbers(text)
    
    # 1. Fractions: \frac{a}{b} -> a/b
    text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
    
    # 2. Subscript conversion
    text = re.sub(r'_\{\s*([^}]+)\s*\}', r'_\1', text)
    text = re.sub(r'_([0-9a-zA-Z+-]+)', 
                  lambda m: m.group(1).translate(
                      str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
                  ), text)
    
    # 3. Superscript conversion
    text = re.sub(r'\^\{\s*([^}]+)\s*\}', r'^\1', text)
    text = re.sub(r'\^([0-9a-zA-Z+-]+)',
                  lambda m: m.group(1).translate(
                      str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₌⁽⁾")
                  ), text)
    
    # 4. Degree symbol fix
    sup_to_normal = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
    text = re.sub(r'([⁰¹²³⁴⁵⁶⁷⁸⁹]+)°', 
                  lambda m: m.group(1).translate(sup_to_normal) + '°', text)
    text = text.replace('^\\circ', '°').replace('^{\\circ}', '°')
    text = text.replace('∘', '°').replace('° C', '°C')
    
    # 5. Remove subscript letters from formulas (NₐHCO₃ -> NaHCO₃)
    sub_chars = str.maketrans("ₐₑₒₓₕₖₗₘₙₚₛₜ", "aeoxhklmnpst")
    text = text.translate(sub_chars)
    
    # 6. Remove spaces before superscripts/subscripts
    text = re.sub(r'\s+([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+)', r'\1', text)
    
    # 7. Fix brackets
    text = text.replace('₍', '(').replace('₎', ')')
    
    # 8. Remove LaTeX commands
    text = re.sub(r'\\[a-zA-Z]+\s*\{?', ' ', text)
    
    # 9. Clean extra spaces
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()

# Format content with image embedding
def format_content(element, img_map):
    """Format HTML element to text with images"""
    if not element:
        return ""
    
    # Remove hidden elements
    for hidden in element.find_all(['annotation', 'script', 'mjx-assistive-mathml']):
        hidden.decompose()
    
    # Process fractions
    for mfrac in element.find_all('mfrac'):
        contents = mfrac.find_all(recursive=False)
        if len(contents) == 2:
            num = contents[0].get_text(strip=True)
            den = contents[1].get_text(strip=True)
            mfrac.replace_with(f"{num}/{den}")
    
    # Process subscripts
    sub_map = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
    for sub in element.find_all(['sub', 'msub']):
        sub.replace_with(sub.get_text(strip=True).translate(sub_map))
    
    # Process superscripts
    sup_map = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₌⁽⁾")
    for sup in element.find_all(['sup', 'msup']):
        sup.replace_with(sup.get_text(strip=True).translate(sup_map))
    
    # Process images
    for img in element.find_all('img'):
        src = img.get('src', '') or img.get('data-src', '')
        if not src:
            img.decompose()
            continue
        
        url = ""
        b64 = ""
        
        if src.startswith('http'):
            url = src
        elif src.startswith('data:image'):
            try:
                if 'base64,' in src:
                    b64 = src.split('base64,')[1]
            except:
                pass
        else:
            decoded_src = urllib.parse.unquote(src)
            b64 = img_map.get(src) or img_map.get(decoded_src) or ""
        
        if b64 and not url:
            url = upload_to_imgbb(b64)
        
        if url:
            img.replace_with(f" IMG_START{url}IMG_END ")
        else:
            img.decompose()
    
    # Get text
    raw_text = element.get_text(separator=" ", strip=True)
    
    # Preserve image markers
    img_markers = []
    def img_repl(match):
        img_markers.append(match.group(0))
        return f" IMGPLACEHOLDER{len(img_markers)-1} "
    
    raw_text = re.sub(r'IMG_START.*?IMG_END', img_repl, raw_text)
    
    # Clean text
    cleaned_text = aggressive_clean(raw_text)
    
    # Restore images
    for i, marker in enumerate(img_markers):
        cleaned_text = cleaned_text.replace(f"IMGPLACEHOLDER{i}", marker)
    
    # Convert markers to HTML
    cleaned_text = re.sub(r'IMG_START(.*?)IMG_END', 
                         r'<img class="qimg" src="\1">', cleaned_text)
    
    return cleaned_text


async def mhtml_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Extract MCQs from MHTML/HTML files
    Supports: TestMoz, Chorcha.net
    Features: Image upload, Formula conversion, Progress tracking
    """
    doc = update.message.document
    
    # Check file extension
    if not (doc.file_name.endswith('.mhtml') or 
            doc.file_name.endswith('.mht') or 
            doc.file_name.endswith('.html')):
        return
    
    status_msg = await update.message.reply_text(
        f"🚀 **Starting:** `{doc.file_name}`"
    )
    
    # Download file
    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()
    
    # Parse MHTML/HTML
    img_map = {}
    html_body = ""
    
    if doc.file_name.endswith('.mhtml') or doc.file_name.endswith('.mht'):
        # MHTML parsing with email library
        msg = email.message_from_bytes(bytes(file_bytes), policy=policy.default)
        
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or 'utf-8',
                    errors='ignore'
                )
            elif part.get_content_type().startswith('image/'):
                loc = part.get('Content-Location', '')
                raw = part.get_payload(decode=True)
                if loc and raw:
                    b64_data = base64.b64encode(raw).decode('utf-8')
                    img_map[loc] = b64_data
                    img_map[urllib.parse.unquote(loc)] = b64_data
    else:
        # HTML parsing
        html_body = file_bytes.decode('utf-8', errors='ignore')
    
    # Parse with BeautifulSoup
    soup = BeautifulSoup(html_body, 'html.parser')
    
    # Try Chorcha.net format first
    chorcha_cards = soup.find_all('div', class_=lambda x: x and 'p-5' in x and 'rounded-xl' in x)
    
    results = []
    start_time = time.time()
    last_ui_update = time.time()
    
    if chorcha_cards:
        # Chorcha.net parsing
        total_mcq = len(chorcha_cards)
        
        for idx, card in enumerate(chorcha_cards, 1):
            # Extract question
            q_div = card.find('div', class_=lambda x: x and 'font-medium' in x)
            if not q_div:
                continue
            
            q_text = format_content(q_div, img_map)
            q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*', '', q_text)
            
            # Extract options
            options = []
            ans_idx = "1"
            ans_map = {'ক': '1', 'খ': '2', 'গ': '3', 'ঘ': '4'}
            
            for i, btn in enumerate(card.find_all('button', class_=lambda x: x and 'p-2' in x), 1):
                lbl = btn.find('span', class_=lambda x: x and 'rounded-full' in x)
                opt_content = btn.find('div', class_='flex-1')
                
                if opt_content:
                    options.append(format_content(opt_content, img_map))
                    
                    # Check if correct answer
                    if any(c in str(btn) for c in ['#017A47', 'border-[#017A47]', 
                                                     '#E2A03F', '#F59E0B', 'border-[#F59E0B]']):
                        ans_idx = ans_map.get(lbl.get_text(strip=True) if lbl else "", str(i))
            
            # Pad options to 4
            while len(options) < 4:
                options.append("")
            
            # Extract explanation
            exp_div = card.find('div', class_=lambda x: x and 'prose' in x)
            exp_text = format_content(exp_div, img_map) if exp_div else ""
            
            results.append({
                'question': q_text,
                'A': options[0],
                'B': options[1],
                'C': options[2],
                'D': options[3],
                'answer': ans_idx,
                'explanation': exp_text
            })
            
            # Update progress
            if idx % 10 == 0 or idx == total_mcq:
                now = time.time()
                if now - last_ui_update > 5:
                    elapsed = now - start_time
                    eta = (elapsed / idx) * (total_mcq - idx)
                    try:
                        await status_msg.edit_text(
                            f"⌛ **ATLAS Dashboard (Chorcha)**\n"
                            f"📝 MCQ: `{idx}/{total_mcq}`\n"
                            f"⏳ ETA: `{int(eta//60):02d}:{int(eta%60):02d}`"
                        )
                    except:
                        pass
                    last_ui_update = now
    
    else:
        # TestMoz format parsing
        cards = soup.find_all('div', class_=lambda x: x and 'rounded-lg' in x and 'shadow-md' in x)
        total_mcq = len(cards)
        
        for idx, card in enumerate(cards, 1):
            # Extract question
            q_p = card.find('p', class_='text-[17px]')
            q_text = format_content(q_p, img_map) if q_p else ""
            q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*', '', q_text)
            
            # Extract options
            opt_divs = card.find_all('div', class_=lambda x: x and 'cursor-pointer' in x and 'col-span-2' in x)
            
            options = []
            ans_idx = "1"
            
            for i, opt in enumerate(opt_divs, 1):
                text_sm = opt.find('div', class_='text-sm')
                opt_text = format_content(text_sm, img_map) if text_sm else ""
                options.append(opt_text)
                
                # Check if correct
                if opt.find('div', class_=lambda x: x and 'bg-green-500' in x) or opt.find('svg'):
                    ans_idx = str(i)
            
            # Pad options
            while len(options) < 4:
                options.append("")
            
            # Extract explanation
            exp_div = card.find('div', class_=lambda x: x and 'col-span-2' in x and 'font-semibold' in x and 'cursor-pointer' not in x)
            exp_text = format_content(exp_div, img_map) if exp_div else ""
            
            results.append({
                'question': q_text,
                'A': options[0],
                'B': options[1],
                'C': options[2],
                'D': options[3],
                'answer': ans_idx,
                'explanation': exp_text
            })
            
            # Update progress
            if idx % 10 == 0 or idx == total_mcq:
                now = time.time()
                if now - last_ui_update > 5:
                    elapsed = now - start_time
                    eta = (elapsed / idx) * (total_mcq - idx)
                    try:
                        await status_msg.edit_text(
                            f"⌛ **ATLAS Dashboard**\n"
                            f"📝 MCQ: `{idx}/{total_mcq}`\n"
                            f"⏳ ETA: `{int(eta//60):02d}:{int(eta%60):02d}`"
                        )
                    except:
                        pass
                    last_ui_update = now
    
    # Check if any results
    if not results:
        await status_msg.edit_text("❌ No MCQs found in file")
        return
    
    # Create CSV
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=['question', 'A', 'B', 'C', 'D', 'answer', 'explanation']
    )
    writer.writeheader()
    writer.writerows(results)
    
    # Send CSV
    await update.message.reply_document(
        document=output.getvalue().encode('utf-8'),
        filename=f"ATLAS_{doc.file_name}.csv",
        caption=f"✅ **Extraction Complete!**\n📊 Total MCQs: `{len(results)}`"
    )
    
    await status_msg.delete()
