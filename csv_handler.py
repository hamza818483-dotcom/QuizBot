#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSV to Poll Handler - Complete CSV/JSON Poll Management"""

import asyncio
import json
import csv
import io
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db

# CSV Parser
def parse_csv_content(content: str):
    """Parse CSV/JSON content to MCQ list"""
    mcqs = []
    
    # Try JSON first
    try:
        data = json.loads(content)
        if isinstance(data, list):
            for item in data:
                mcqs.append({
                    'question': item.get('question', ''),
                    'options': item.get('options', {}),
                    'answer': item.get('answer', ''),
                    'explanation': item.get('explanation', '')
                })
            return mcqs
    except:
        pass
    
    # Try CSV
    try:
        reader = csv.DictReader(io.StringIO(content))
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
        return mcqs
    except:
        pass
    
    return []


def is_short_option(options: dict) -> bool:
    """Check if all options are short (≤16 chars) for 2x2 grid"""
    for opt in options.values():
        clean = re.sub(r'<[^>]+>', '', str(opt)).strip()
        if len(clean) > 16:
            return False
    return True


async def send_poll_header(chat_id, title: str, count: int, context):
    """Send poll header message"""
    header = f"""🌟Important Poll Solve By ATLAS
🔥Topic Name: {title if title else ''}

✅প্রশ্ন সংখ্যা: {count}"""
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=header,
        parse_mode=ParseMode.HTML
    )
    return msg.message_id


async def send_ending_message(chat_id, title: str, count: int, first_poll_link: str, context):
    """Send ending message after all polls"""
    ending = f"""🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!
 👉এটলাস আয়োজিত "{title}" পোল সলভে অংশগ্রহণ করার জন্য। 😊

📊 মোট পোল: {count}

⁉️তোমার স্কোর কত? 🤔
( ? / {count} )

নিচে লিখো! 👇

✅পোল যেখান থেকে শুরু হয়েছে:
{first_poll_link}"""
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=ending,
        parse_mode=ParseMode.HTML
    )


async def send_poll(chat_id, mcq: dict, context):
    """Send a single poll"""
    # Get explanation settings
    exp_row = await db.fetchone('SELECT mode, custom_text, tag_name FROM exp_settings WHERE id = 1')
    exp_mode = exp_row[0] if exp_row else 'auto'
    custom_exp = exp_row[1] if exp_row else ''
    tag_name = exp_row[2] if exp_row else ''
    
    # Get tag settings
    tag_row = await db.fetchone('SELECT tag_name, position FROM tag_settings WHERE is_active = 1 LIMIT 1')
    if tag_row:
        tag_text = tag_row[0]
        tag_position = tag_row[1]
    else:
        tag_text = None
        tag_position = None
    
    # Build explanation
    if exp_mode == 'custom' and custom_exp:
        explanation = custom_exp
    elif exp_mode == 'auto':
        explanation = mcq.get('explanation', '')
    elif exp_mode == 'tag' and tag_name:
        explanation = tag_name
    else:
        explanation = ''
    
    # Add tag name if exists
    if tag_text:
        if tag_position == 'after':
            if explanation:
                explanation += f"\n{tag_text}"
            else:
                explanation = tag_text
        elif tag_position == 'before':
            if explanation:
                explanation = f"{tag_text}\n{explanation}"
            else:
                explanation = tag_text
        elif tag_position == 'inline':
            if explanation:
                explanation += f" {tag_text}"
            else:
                explanation = tag_text
    
    # Truncate explanation to 200 chars
    if len(explanation) > 200:
        explanation = explanation[:197] + '...'
    
    # Build question with tag if needed
    question = mcq['question']
    if tag_text and tag_position == 'question_before':
        question = f"{tag_text}\n{question}"
    elif tag_text and tag_position == 'question_after':
        question = f"{question}\n{tag_text}"
    
    # Send poll
    options_list = list(mcq['options'].values())
    correct_index = ord(mcq['answer'].upper()) - ord('A')
    
    poll_msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=question,
        options=options_list,
        type='quiz',
        correct_option_id=correct_index,
        explanation=explanation if explanation else None,
        is_anonymous=False
    )
    
    return poll_msg.message_id


async def csv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/csv - Upload CSV/JSON and send polls"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ CSV/JSON file-এ reply করে /csv দাও")
        return
    
    # Download file
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8')
    
    # Parse
    mcqs = parse_csv_content(content_str)
    if not mcqs:
        await update.message.reply_text("❌ Valid CSV/JSON format পাওয়া যায়নি")
        return
    
    # Get channels
    channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
    
    # Build buttons
    buttons = []
    for ch_id, ch_name in channels:
        buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"csv_send_{ch_id}")])
    buttons.append([InlineKeyboardButton("📤 শুধু CSV", callback_data="csv_only")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="csv_cancel")])
    
    # Store in context
    context.user_data['csv_mcqs'] = mcqs
    context.user_data['csv_file_name'] = update.message.reply_to_message.document.file_name
    
    await update.message.reply_text(
        f"✅ {len(mcqs)}টি MCQ পাওয়া গেছে!\nChannel select করো:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def csvs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/csvS [batch] [channel] [topic] - Send CSV polls in batches"""
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ CSV file-এ reply করে /csvS দাও")
        return
    
    # Parse args
    args = context.args
    batch_size = 15
    channel_id = None
    topic = ''
    
    if len(args) >= 1:
        try:
            batch_size = int(args[0])
        except:
            pass
    
    if len(args) >= 2:
        channel_id = args[1]
    
    if len(args) >= 3:
        topic = ' '.join(args[2:])
    
    # Download file
    file = await update.message.reply_to_message.document.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8')
    
    # Parse
    mcqs = parse_csv_content(content_str)
    if not mcqs:
        await update.message.reply_text("❌ Valid CSV format পাওয়া যায়নি")
        return
    
    if not channel_id:
        # Ask for channel
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        buttons = []
        for ch_id, ch_name in channels:
            buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"csvs_ch_{ch_id}")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="csvs_cancel")])
        
        context.user_data['csvs_mcqs'] = mcqs
        context.user_data['csvs_batch'] = batch_size
        context.user_data['csvs_topic'] = topic
        
        await update.message.reply_text(
            f"✅ {len(mcqs)}টি MCQ | Batch: {batch_size}\nChannel select:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    
    # Start sending
    await send_csvs_polls(update.message.chat.id, channel_id, mcqs, batch_size, topic, context)


async def send_csvs_polls(user_id, channel_id, mcqs, batch_size, topic, context):
    """Send CSV polls in serial batches"""
    total = len(mcqs)
    batches = [mcqs[i:i+batch_size] for i in range(0, total, batch_size)]
    
    progress_msg = await context.bot.send_message(
        chat_id=user_id,
        text=f"📤 Poll পাঠানো শুরু হচ্ছে...\n✅ Total: {total}\n📦 Batches: {len(batches)}"
    )
    
    part_links = []
    first_poll_link = None
    
    for part_num, batch in enumerate(batches, 1):
        # Send header
        await send_poll_header(channel_id, topic, len(batch), context)
        
        # Send polls
        batch_first_link = None
        for idx, mcq in enumerate(batch):
            # Check pause
            while context.user_data.get('paused', False):
                await asyncio.sleep(1)
            
            poll_id = await send_poll(channel_id, mcq, context)
            
            if part_num == 1 and idx == 0:
                first_poll_link = f"https://t.me/c/{str(channel_id).replace('-100', '')}/{poll_id}"
            
            if idx == 0:
                batch_first_link = f"https://t.me/c/{str(channel_id).replace('-100', '')}/{poll_id}"
            
            await asyncio.sleep(2)
            
            # Update progress
            sent = (part_num - 1) * batch_size + idx + 1
            await progress_msg.edit_text(
                f"📤 পোল পাঠানো হচ্ছে...\n✅ পাঠানো: {sent}/{total}\n⏱️ বাকি: ~{(total - sent) * 2} সেকেন্ড"
            )
        
        # Part summary
        part_links.append(batch_first_link)
        part_summary = f"""📚 Part-{part_num:02d} শেষ! ✅
🔗 Part-{part_num:02d}: {batch_first_link}
তোমার স্কোর: ? / {len(batch)}"""
        
        await context.bot.send_message(chat_id=channel_id, text=part_summary)
    
    # Final summary
    final_text = "🎯 সব Part শেষ!\n\n"
    for i, link in enumerate(part_links, 1):
        final_text += f"📌 Part-{i:02d}: {link}\n"
    
    await context.bot.send_message(chat_id=channel_id, text=final_text)
    
    # Ending message
    await send_ending_message(channel_id, topic, total, first_poll_link, context)
    
    await progress_msg.edit_text(f"✅ সম্পন্ন! {total}টি poll পাঠানো হয়েছে।")


async def handle_csv_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle CSV callback queries"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == 'csv_cancel' or data == 'csvs_cancel':
        await query.edit_message_text("❌ Cancelled")
        return
    
    if data == 'csv_only':
        # Send CSV file
        mcqs = context.user_data.get('csv_mcqs', [])
        file_name = context.user_data.get('csv_file_name', 'mcqs.csv')
        
        # Create CSV
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=['question', 'A', 'B', 'C', 'D', 'answer', 'explanation'])
        writer.writeheader()
        for mcq in mcqs:
            writer.writerow({
                'question': mcq['question'],
                'A': mcq['options']['A'],
                'B': mcq['options']['B'],
                'C': mcq['options']['C'],
                'D': mcq['options']['D'],
                'answer': mcq['answer'],
                'explanation': mcq.get('explanation', '')
            })
        
        # Get thumbnail if exists
        thumb_row = await db.fetchone('SELECT file_id FROM thumbnail WHERE id = 1')
        thumb = thumb_row[0] if thumb_row else None
        
        await query.message.reply_document(
            document=output.getvalue().encode('utf-8'),
            filename=file_name,
            thumbnail=thumb
        )
        await query.edit_message_text("✅ CSV পাঠানো হয়েছে")
        return
    
    if data.startswith('csv_send_'):
        channel_id = data.replace('csv_send_', '')
        mcqs = context.user_data.get('csv_mcqs', [])
        
        await query.edit_message_text(f"📤 {len(mcqs)}টি poll পাঠানো হচ্ছে...")
        
        # Send header
        first_msg_id = await send_poll_header(channel_id, '', len(mcqs), context)
        
        # Send polls
        for mcq in mcqs:
            await send_poll(channel_id, mcq, context)
            await asyncio.sleep(2)
        
        # Ending
        first_link = f"https://t.me/c/{str(channel_id).replace('-100', '')}/{first_msg_id}"
        await send_ending_message(channel_id, '', len(mcqs), first_link, context)
        
        await query.message.reply_text(f"✅ {len(mcqs)}টি poll পাঠানো সম্পন্ন!")
        return
    
    if data.startswith('csvs_ch_'):
        channel_id = data.replace('csvs_ch_', '')
        mcqs = context.user_data.get('csvs_mcqs', [])
        batch_size = context.user_data.get('csvs_batch', 15)
        topic = context.user_data.get('csvs_topic', '')
        
        await query.edit_message_text("📤 শুরু হচ্ছে...")
        await send_csvs_polls(query.message.chat.id, channel_id, mcqs, batch_size, topic, context)
