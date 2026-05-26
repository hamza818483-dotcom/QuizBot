#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MHTML/HTML to CSV Handler"""

import re
import csv
import io
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ContextTypes

async def mhtml_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract polls from MHTML/HTML and convert to CSV"""
    doc = update.message.document
    
    # Check file extension
    if not (doc.file_name.endswith('.mhtml') or doc.file_name.endswith('.mht') or doc.file_name.endswith('.html')):
        return  # Not an MHTML/HTML file
    
    await update.message.reply_text("⏳ MHTML থেকে polls extract হচ্ছে...")
    
    # Download file
    file = await doc.get_file()
    content = await file.download_as_bytearray()
    content_str = content.decode('utf-8', errors='ignore')
    
    # Parse HTML
    soup = BeautifulSoup(content_str, 'html.parser')
    
    # Find all polls
    mcqs = []
    
    # Method 1: Find Telegram poll structure
    polls = soup.find_all('div', class_=re.compile('poll|quiz|question'))
    
    for poll in polls:
        try:
            # Extract question
            question_elem = poll.find(['div', 'p', 'span'], class_=re.compile('question|title'))
            if not question_elem:
                continue
            question = question_elem.get_text(strip=True)
            
            # Extract options
            options_elems = poll.find_all(['div', 'li', 'span'], class_=re.compile('option|answer|choice'))
            if len(options_elems) < 4:
                continue
            
            options = {}
            for i, opt in enumerate(options_elems[:4]):
                options[chr(65 + i)] = opt.get_text(strip=True)
            
            # Find correct answer
            correct = poll.find(class_=re.compile('correct|right|answer'))
            if correct:
                answer_text = correct.get_text(strip=True)
                # Try to match with options
                answer = 'A'
                for key, val in options.items():
                    if val in answer_text or answer_text in val:
                        answer = key
                        break
            else:
                answer = 'A'
            
            # Extract explanation
            explanation_elem = poll.find(['div', 'p'], class_=re.compile('explanation|hint|detail'))
            explanation = explanation_elem.get_text(strip=True) if explanation_elem else ''
            
            mcqs.append({
                'question': question,
                'options': options,
                'answer': answer,
                'explanation': explanation
            })
        except:
            continue
    
    # Method 2: Simple text parsing if no structured polls found
    if not mcqs:
        text = soup.get_text()
        lines = text.split('\n')
        
        current_q = None
        current_opts = {}
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Check if it's a question (ends with ?)
            if line.endswith('?') and len(line) > 10:
                if current_q and len(current_opts) >= 4:
                    mcqs.append({
                        'question': current_q,
                        'options': current_opts,
                        'answer': 'A',
                        'explanation': ''
                    })
                current_q = line
                current_opts = {}
            
            # Check if it's an option (starts with A. B. C. D.)
            elif re.match(r'^[A-D][.)]\s+', line):
                key = line[0]
                value = re.sub(r'^[A-D][.)]\s+', '', line)
                current_opts[key] = value
        
        # Add last question
        if current_q and len(current_opts) >= 4:
            mcqs.append({
                'question': current_q,
                'options': current_opts,
                'answer': 'A',
                'explanation': ''
            })
    
    if not mcqs:
        await update.message.reply_text("❌ কোন poll খুঁজে পাওয়া যায়নি")
        return
    
    # Create CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['question', 'A', 'B', 'C', 'D', 'answer', 'explanation'])
    writer.writeheader()
    
    for mcq in mcqs:
        writer.writerow({
            'question': mcq['question'],
            'A': mcq['options'].get('A', ''),
            'B': mcq['options'].get('B', ''),
            'C': mcq['options'].get('C', ''),
            'D': mcq['options'].get('D', ''),
            'answer': mcq['answer'],
            'explanation': mcq.get('explanation', '')
        })
    
    # Send CSV
    await update.message.reply_document(
        document=output.getvalue().encode('utf-8'),
        filename='extracted_polls.csv',
        caption=f"✅ {len(mcqs)}টি poll extract করা হয়েছে!"
    )
