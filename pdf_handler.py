#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - PDF Handlers FINAL with Source Option"""
import os, re, io, json, time, asyncio, logging, tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import db
from services import pdf_processor, generate_mcqs_from_image, mcqs_to_csv, format_progress, parse_csv_to_mcqs
from ocr_engine import ocr_image, is_scanned_pdf
from global_state import GLOBAL_PAUSE
from csv_poll_handler import get_pre_message, get_ending_message, get_message_link

logger = logging.getLogger(__name__)

# ============================================================
# DASHBOARD
# ============================================================
async def update_dashboard(msg, data: dict):
    pct = data.get('pct', 0)
    bar_len = 20
    filled = int(bar_len * pct / 100)
    bar = '█' * filled + '░' * (bar_len - filled)
    status = data.get('status', '⏳')
    dashboard = f"""╔══════════════════════════════════╗
║     📊 ATLAS PDF PROCESSOR      ║
╠══════════════════════════════════╣
║ 📁 {data.get('pdf','N/A')[:30]:<30} ║
║ 📥 {data.get('dl','')[:32]:<32} ║
╠══════════════════════════════════╣
║ 📄 Pages: {data.get('pg','0/0'):<24} ║
        is_qbm = data.startswith("qbm"); mood = data.split("_")[-1]
║ 📤 Sent: {data.get('sent','0/0'):<22} ║
║ ⏱️ {data.get('time',''):<28} ║
║ [{bar}] {pct}%{'':<10} ║
║ {status:<32} ║
╚══════════════════════════════════╝"""
    try: await msg.edit_text(f"```{dashboard}```", parse_mode='Markdown')
    except: await msg.edit_text(dashboard)

# ============================================================
# POLL SENDER
# ============================================================
async def send_poll_robust(bot, chat_id, mcq, reply_to, uid, with_source=False):
    for attempt in range(10):
        while GLOBAL_PAUSE.get(uid, False): await asyncio.sleep(1)
        try:
            # Get question with tags
            q_raw = mcq.get('question','?')
            tags = await db.fetchall('SELECT tag_name, position FROM tag_settings WHERE is_active = 1')
            for tname, tpos in tags:
                if tpos == 'tag1': q_raw = f"{tname}\n\n{q_raw}"
                elif tpos == 'tag2': q_raw = f"{q_raw}\n\n{tname}"
                elif tpos == 'tag3': q_raw = f"{q_raw} {tname}"
                elif tpos == 'tag4': q_raw = f"{tname}\n{q_raw}"
            
            # Source tag
            source_tag = ''
            if with_source:
                # Keep ALL source tags from original question
                src_matches = re.findall(r'[\[\(][^\]\)]*?(?:BCS|DU|HSTU|Medical|Admission|Exam|Test|উন্মেষ|মেডিকেল|RU|JU|CU|GST|RUET|KUET|CUET|BUET|HSC|SSC|JSC|PSC|primary|teacher|registrar|assistant|officer|bank|government|NTRCA|NSI|প্রাইমারি|শিক্ষক|নিবন্ধন|বিসিএস|পিএসসি|মাস্টার্স|ডিগ্রী|সম্মান|অনার্স|প্রিলি|লিখিত|ভাইভা|MCQ|CQ|সৃজনশীল|নৈর্ব্যক্তিক|সংক্ষিপ্ত|রচনামূলক)[^\]\)]*[\]\)]', q_raw, re.IGNORECASE)
                if src_matches: source_tag = ' ' + ' '.join(src_matches)
            # Remove numbering from poll question too
            q_no_num = re.sub(r'^\s*[\d০-৯]+\s*[.)\-:\s]+\s*', '', q_raw)
            q_no_num = re.sub(r'^\s*[Qq]\.?\s*[\d]+\s*[.)\-:\s]*\s*', '', q_no_num)
            q = (re.sub(r'\s*[\[\(].*?[\]\)]\s*$', '', q_no_num).strip() + source_tag)[:300]
            
            opts = [mcq.get('options',{}).get(k,'Option '+k) for k in ['A','B','C','D']]
            ans = str(mcq.get('answer','1')).upper()
            cid = {'A':0,'B':1,'C':2,'D':3,'1':0,'2':1,'3':2,'4':3}.get(ans,0)
            
            # Get explanation from /exp settings
            exp_row = await db.fetchone('SELECT mode, custom_text, tag_name FROM exp_settings WHERE id = 1')
            if exp_row:
                mode, custom_text, tag_name = exp_row
                if mode == 'custom' and custom_text:
                    exp = custom_text
                else:
                    exp = mcq.get('explanation','')[:200]
                if tag_name:
                    exp = f"{exp}\n{tag_name}" if exp else tag_name
            else:
                exp = mcq.get('explanation','')[:200]
            
            poll_msg = await bot.send_poll(chat_id=chat_id, question=q, options=opts,
                type='quiz', correct_option_id=cid, explanation=exp or None,
                is_anonymous=True, reply_to_message_id=reply_to)
            return poll_msg.message_id, True
        except:
            if attempt < 9: await asyncio.sleep(3)
    return None, False

# ============================================================
# ARGS
# ============================================================
def parse_args(args):
    page_range, channel_id, title, mcq_count = None, None, "MCQ Practice", None
    i = 0
    while i < len(args):
        if args[i] == '-p' and i+1 < len(args): page_range = args[i+1]; i += 2
        elif args[i] == '-c' and i+1 < len(args): channel_id = args[i+1]; i += 2
        elif args[i] == '-m' and i+1 < len(args): title = args[i+1]; i += 2
        else:
            match = re.match(r'\[(\d+)\]', args[i])
            if match: mcq_count = int(match.group(1))
            i += 1
    return page_range, channel_id, title, mcq_count

# ============================================================
# /pdfm HANDLER
# ============================================================
async def pdfm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF ফাইলে reply করে `/pdfm` দাও"); return
    doc = update.message.reply_to_message.document
    if not doc.file_name.lower().endswith('.pdf'): await update.message.reply_text("❌ শুধু PDF!"); return
    page_range, channel_id, title, mcq_count = parse_args(context.args)
    for k, v in [('pdf_title', title), ('pdf_channel', channel_id), ('pdf_mcq_count', mcq_count),
                 ('pdf_page_range', page_range), ('pdf_doc', doc.file_id)]: context.chat_data[k] = v
    buttons = [
        [InlineKeyboardButton("📸 Image Mood", callback_data="pdfm_mood_image")],
        [InlineKeyboardButton("📝 Topic Name Mood", callback_data="pdfm_mood_topic")],
        [InlineKeyboardButton("❌ Cancel", callback_data="pdfm_cancel")]
    ]
    await update.message.reply_text(
        f"📄 *PDF MCQ Generation*\n\n📁 `{doc.file_name}`\n📄 Pages: {page_range or '1-10'}\n📝 {title}\n🎯 MCQ/Page: {mcq_count or 'Auto'}\n\n*Select Mood:*",
        parse_mode=None, reply_markup=InlineKeyboardMarkup(buttons))

# ============================================================
# /qbm HANDLER
# ============================================================
async def qbm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ PDF ফাইলে reply করে `/qbm` দাও"); return
    doc = update.message.reply_to_message.document
    if not doc.file_name.lower().endswith('.pdf'): await update.message.reply_text("❌ শুধু PDF!"); return
    page_range, channel_id, title, _ = parse_args(context.args)
    for k, v in [('qbm_doc', doc.file_id), ('qbm_page_range', page_range), ('qbm_title', title), ('qbm_channel', channel_id)]:
        context.chat_data[k] = v
    buttons = [
        [InlineKeyboardButton("📸 Image Mood", callback_data="qbm_mood_image")],
        [InlineKeyboardButton("📝 Topic Name Mood", callback_data="qbm_mood_topic")],
    ]
    await update.message.reply_text("📋 *Select Extraction Mood:*", parse_mode=None, reply_markup=InlineKeyboardMarkup(buttons))

# ============================================================
# CORE PROCESSING
# ============================================================
async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, is_qbm: bool = False, mood: str = 'topic'):
    query = update.callback_query if hasattr(update, 'callback_query') else None
    uid = update.effective_user.id
    prefix = 'qbm' if is_qbm else 'pdf'
    title = context.chat_data.get(f'{prefix}_title') or context.chat_data.get('pdf_title', 'MCQ')
    channel_id = context.chat_data.get(f'{prefix}_channel') or context.chat_data.get('pdf_channel')
    mcq_count = context.chat_data.get(f'{prefix}_mcq_count') or context.chat_data.get('pdf_mcq_count')
    page_range_str = context.chat_data.get(f'{prefix}_page_range') or context.chat_data.get('pdf_page_range')
    doc_id = context.chat_data.get(f'{prefix}_doc') or context.chat_data.get('pdf_doc')
    with_source = context.chat_data.get('qbm_with_source', False)

    if not doc_id:
        if query: await query.edit_message_text("❌ PDF not found!"); return

    msg_target = query.message if query else update.message
    start_time = time.time()
    dash_data = {'pdf': 'Loading...', 'dl': '', 'pg': '0/0', 'mcq': 0, 'sent': '0/0', 'time': '', 'pct': 0, 'status': '📥 Downloading...'}
    dash_msg = await msg_target.reply_text("⏳ Initializing...")
    await update_dashboard(dash_msg, dash_data)

    sp, ep = 1, 10
    if page_range_str:
        try:
            if '-' in page_range_str: sp, ep = int(page_range_str.split('-')[0]), int(page_range_str.split('-')[1])
            else: sp = ep = int(page_range_str)
        except: pass

    try:
        file = await context.bot.get_file(doc_id)
        pdf_name = file.file_path.split('/')[-1] if file.file_path else "PDF"
        dash_data['pdf'] = pdf_name
        pdf_bytes = await file.download_as_bytearray()
        if isinstance(pdf_bytes, bytearray): pdf_bytes = bytes(pdf_bytes)
        dash_data['dl'] = f"Done {len(pdf_bytes)/1024:.0f}KB"
    except:
        dash_data['status'] = '❌ Download Failed'; await update_dashboard(dash_msg, dash_data); return

    pdf_path = f"data/temp/pdf_{int(time.time())}_{uid}.pdf"
    os.makedirs("data/temp", exist_ok=True)
    with open(pdf_path, 'wb') as f: f.write(pdf_bytes)
    total_pages = pdf_processor.get_page_count(pdf_path)
    ep = min(ep, total_pages)
    total = ep - sp + 1
    dash_data['pg'] = f"0/{total}"; dash_data['status'] = '📄 Converting...'
    await update_dashboard(dash_msg, dash_data)

    images = pdf_processor.pdf_to_images(pdf_path, sp, ep)

    if is_qbm and (not images or len(str(images[0][1])) < 100):
        dash_data['status'] = '🔍 OCR Scanning...'; await update_dashboard(dash_msg, dash_data)
        ocr_images = []
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            for pi in range(sp-1, min(ep, len(reader.pages))):
                from pdf2image import convert_from_path
                pil_imgs = convert_from_path(pdf_path, first_page=pi+1, last_page=pi+1)
                if pil_imgs:
                    ocr_texts = []
                    for _ in range(3):
                        buf = io.BytesIO(); pil_imgs[0].save(buf, format='JPEG', quality=90)
                        ocr_texts.append(ocr_image(buf.getvalue()))
                    best = max(ocr_texts, key=len) if ocr_texts else ""
                    if best: ocr_images.append((pi+1, best.encode('utf-8')))
        except: pass
        if ocr_images: images = ocr_images

    if not images:
        dash_data['status'] = '❌ No Content'; await update_dashboard(dash_msg, dash_data)
        try: os.remove(pdf_path)
        except: pass; return

    all_mcqs = []
    page_links = {}
    sent_count = 0
    pdf_name = "PDF"
    
    if is_qbm:
        active_prompts = ["""YOU ARE AN MCQ EXTRACTOR. STRICT RULES:
1. ONLY extract EXISTING MCQs from this image. NEVER create new questions from info.
2. Extract ALL - Bangla/English, A/B/C/D, 1/2/3/4, ক/খ/গ/ঘ, a/b/c/d.
3. Multiple OCR passes to catch every MCQ. Triple-check.
4. Remove question numbering (১., 1., Q1., Q.1) from question text.
5. Detect answer from markings (circle, tick, underline, bold, answer key).
6. If explanation exists in image → use it. If NOT → CREATE explanation: why answer is correct + why others are not + relevant topic info (max 165 chars Bengali).
7. Add /exp tag_name after explanation.
8. Output ONLY valid JSON array. If NO MCQ exists, return []."""]
    if not is_qbm:
        rows = await db.fetchall('SELECT content FROM prompts WHERE is_active = 1')
        if not rows:
            await msg_target.reply_text("❌ No Active Prompt!")
            return
        active_prompts = [r[0] for r in rows]
    for idx, (page_num, img_bytes) in enumerate(images):
        while GLOBAL_PAUSE.get(uid, False):
            dash_data['status'] = '⏸️ PAUSED'; await update_dashboard(dash_msg, dash_data); await asyncio.sleep(1)

        page_mcqs = []
        for retry in range(3 if is_qbm else 2):
            try:
                cnt = mcq_count if mcq_count and not is_qbm else (15 if not is_qbm else 20)
                page_mcqs = await generate_mcqs_from_image(img_bytes, active_prompts, cnt)
                if page_mcqs: break
            except: await asyncio.sleep(2)

        if page_mcqs:
            # Clean source from CSV (always without source)
            for mcq_clean in page_mcqs:
                # Remove source tag + ALL numbering formats
                q_clean = mcq_clean.get('question','')
                q_clean = re.sub(r'\s*[\[\(].*?[\]\)]\s*$', '', q_clean)  # Remove source
                q_clean = re.sub(r'^\s*[\d০-৯]+\s*[.)\-:\s]+\s*', '', q_clean)  # 1. ১.
                q_clean = re.sub(r'^\s*[Qq]\.?\s*[\d]+\s*[.)\-:\s]*\s*', '', q_clean)  # Q1. Q.1
                q_clean = re.sub(r'^\s*\(?\s*[\d০-৯]+\s*\)?\s*[.)\-:\s]*\s*', '', q_clean)  # (1) (১)
                mcq_clean['question'] = q_clean.strip()

            all_mcqs.extend(page_mcqs)
            dash_data['mcq'] = len(all_mcqs)
            dash_data['pg'] = f"{idx+1}/{total}"
            dash_data['pct'] = int((idx+1)/total*100)

            if channel_id:
                if mood == 'image':
                    img_raw = img_bytes[1] if isinstance(img_bytes, tuple) else img_bytes
                    img_msg = await context.bot.send_photo(chat_id=channel_id, photo=io.BytesIO(img_raw if isinstance(img_raw, bytes) else img_raw))
                    reply_to = img_msg.message_id
                else:
                    pre_text = get_pre_message(f"{title} (Page-{page_num:02d})", len(page_mcqs))
                    pre_msg = await context.bot.send_message(chat_id=channel_id, text=pre_text)
                    reply_to = pre_msg.message_id

                first_pid, psent = None, 0
                for mcq in page_mcqs:
                    pid, ok = await send_poll_robust(context.bot, channel_id, mcq, reply_to, uid, with_source)
                    if ok:
                        if not first_pid: first_pid = pid
                        psent += 1; sent_count += 1
                        dash_data['sent'] = f"{sent_count}/{len(all_mcqs)}"
                    await update_dashboard(dash_msg, dash_data)
                    await asyncio.sleep(1)

                first_link = await get_message_link(context.bot, channel_id, first_pid) if first_pid else ""
                ending = get_ending_message(f"{title} (Page-{page_num:02d})", psent, first_link)
                await context.bot.send_message(chat_id=channel_id, text=ending, reply_to_message_id=reply_to, disable_web_page_preview=True)
                page_links[page_num] = first_link

        dash_data['time'] = f"{int(time.time()-start_time)}s"; await update_dashboard(dash_msg, dash_data)

    try: os.remove(pdf_path)
    except: pass

    dash_data['status'] = '📊 CSV...'; await update_dashboard(dash_msg, dash_data)
    csv_bytes = mcqs_to_csv(all_mcqs)
    await msg_target.reply_document(document=csv_bytes, filename=f"{title}.csv", caption=f"✅ {len(all_mcqs)} MCQ | {len(images)} pages")

    if channel_id and len(page_links) > 1:
        summary = f"🟥পেইজভিত্তিক Important Poll Solve By ATLAS\n🌟Topic: {title}\n\n✅নিচে সিরিয়ালী সাজিয়ে দেওয়া হলো:\n\n"
        for pg, link in page_links.items(): summary += f"📍Page-{pg:02d}:\n{link}\n\n"
        await context.bot.send_message(chat_id=channel_id, text=summary, disable_web_page_preview=True)

    dash_data['status'] = '✅ COMPLETE!'; dash_data['pct'] = 100; await update_dashboard(dash_msg, dash_data)
    context.user_data['last_csv'] = csv_bytes; context.user_data['last_mcqs'] = all_mcqs
    
    # For QBM without channel, show channel list after CSV
    if is_qbm and not channel_id and all_mcqs:
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        if channels:
            buttons = []
            for ch_id, ch_name in channels:
                buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"qbm_send_{ch_id}")])
            buttons.append([InlineKeyboardButton("❌ Skip", callback_data="qbm_skip")])
            await msg_target.reply_text(f"✅ CSV Ready!\n\nSend Polls to Channel?", reply_markup=InlineKeyboardMarkup(buttons))
        return

# ============================================================
# CALLBACKS
# ============================================================
async def handle_pdf_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mood = "topic"
    is_qbm = False
    mood = "topic"
    query = update.callback_query; await query.answer()
        is_qbm = data.startswith("qbm"); mood = data.split("_")[-1]
    data = query.data
    if data.startswith('pdfm_mood_') or data.startswith('qbm_mood_'):
        if mood == 'cancel': await query.edit_message_text("❌ Cancelled!"); return
        all_mcqs = []
    page_links = {}
    sent_count = 0
    all_mcqs = []
    sent_count = 0
    page_links = {}
    images = []
    pdf_name = "PDF"
    
    if is_qbm:
            context.chat_data['qbm_mood'] = mood
            buttons = [
                [InlineKeyboardButton("📝 With Source", callback_data="qbm_source_yes")],
                [InlineKeyboardButton("📝 Without Source", callback_data="qbm_source_no")],
            ]
            await query.edit_message_text("📋 *Source Option:*\n\nWith Source = প্রশ্নে [BCS] tag সহ\nWithout Source = tag বাদে\n\n*CSV সবসময় Without Source*", parse_mode=None, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith('qbm_source_'):
        context.chat_data['qbm_with_source'] = (data == 'qbm_source_yes')
        mood = context.chat_data.get("qbm_mood", "topic")
        mood = context.chat_data.get('qbm_mood', 'topic')
        await query.edit_message_text(f"⏳ QBM Extracting...\n📝 Source: {'With' if context.chat_data['qbm_with_source'] else 'Without'}")
        await process_pdf(update, context, True, mood)
    elif data == 'qbm_skip':
        await query.edit_message_text("✅ CSV saved! Poll skipped.")
    
    elif data.startswith('qbm_send_'):
        channel_id = data.replace('qbm_send_', '')
        context.chat_data['qbm_channel'] = channel_id
        mcqs = context.user_data.get('last_mcqs', [])
        topic = context.user_data.get('last_topic', 'MCQ')
        mood = context.chat_data.get('qbm_mood', 'topic')
        with_source = context.chat_data.get('qbm_with_source', False)
        if mcqs:
            await query.edit_message_text(f"📤 Sending {len(mcqs)} polls...")
            await send_polls_to_channel(update, context, channel_id, mcqs, topic, mood, None)
        else:
            await query.edit_message_text("❌ No MCQs!")
    
    elif data in ('pdfm_cancel', 'qbm_cancel'):
        await query.edit_message_text("❌ Cancelled!")

