#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS QUIZ SYSTEM — Complete | Safe | Modular"""

import os, re, io, json, time, asyncio, random, hashlib, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import db

logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTS
# ============================================================
QUIZ_TIMER_OPTIONS = [10, 15, 20, 30]
MOTAMOT = {
    90: ("🏆", "অসাধারণ! তুমি সেরা! আরও এগিয়ে যাও!"),
    70: ("🎉", "চমৎকার! তুমি খুব ভালো করেছো! আরও প্র্যাকটিস করো!"),
    50: ("👍", "মোটামুটি ভালো! আরও একটু পড়াশোনা করো!"),
    0:  ("📚", "পড়া হয়নি! আবার পড়ে চেষ্টা করো!")
}

# ============================================================
# SECTION 1: DATABASE SETUP
# ============================================================
async def setup_quiz_tables():
    """Create quiz tables if not exists"""
    await db.execute('''
        CREATE TABLE IF NOT EXISTS quizzes (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            timer INTEGER DEFAULT 15,
            shuffle BOOLEAN DEFAULT 0,
            csv_data TEXT,
            tag TEXT DEFAULT '',
            exp_footer TEXT DEFAULT '',
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS quiz_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            quiz_id TEXT,
            right_count INTEGER DEFAULT 0,
            wrong_count INTEGER DEFAULT 0,
            skip_count INTEGER DEFAULT 0,
            total INTEGER,
            score TEXT,
            attempt INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS quiz_leaderboard (
            quiz_id TEXT,
            user_id INTEGER,
            user_name TEXT,
            score TEXT,
            right_count INTEGER,
            total INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (quiz_id, user_id)
        )
    ''')

# ============================================================
# SECTION 2: HELPERS
# ============================================================
def generate_quiz_id():
    """Generate unique quiz ID"""
    return f"qz_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"

def get_motamot(percentage):
    """Get motamot based on percentage"""
    for threshold in sorted(MOTAMOT.keys(), reverse=True):
        if percentage >= threshold:
            return MOTAMOT[threshold]
    return MOTAMOT[0]

def shuffle_questions(questions):
    """Shuffle questions and options"""
    shuffled = questions.copy()
    random.shuffle(shuffled)
    for q in shuffled:
        if random.choice([True, False]):
            opts = q.get('options', [])
            if len(opts) > 1:
                correct = opts[q.get('answer_index', 0)]
                random.shuffle(opts)
                q['answer_index'] = opts.index(correct)
                q['options'] = opts
    return shuffled

def parse_csv_to_quiz(csv_text):
    """Parse CSV text to quiz questions"""
    questions = []
    try:
        import csv as csv_module
        # Clean BOM + TAB
        csv_text = csv_text.replace('\ufeff', '').replace('\t', ',')
        reader = csv_module.DictReader(io.StringIO(csv_text))
        for row in reader:
            q = {
                'question': row.get('questions', row.get('question', '')),
                'options': [],
                'answer_index': 0,
                'explanation': row.get('explanation', ''),
                'image_url': row.get('image', row.get('qi', ''))
            }
            for key in ['option1', 'option2', 'option3', 'option4', 'A', 'B', 'C', 'D']:
                val = row.get(key, '')
                if val:
                    q['options'].append(val)
            ans = row.get('answer', row.get('correct', '1'))
            try:
                q['answer_index'] = int(ans) - 1 if ans.isdigit() else 0
            except:
                q['answer_index'] = 0
            if q['question'] and len(q['options']) >= 2:
                questions.append(q)
    except Exception as e:
        logger.error(f"CSV parse error: {e}")
    return questions

# ============================================================
# SECTION 3: QUIZ CREATE (/q)
# ============================================================
async def quiz_create_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /q command — 4-line format"""
    try:
        msg = update.message
        if not msg.reply_to_message or not msg.reply_to_message.document:
            await msg.reply_text("❌ CSV ফাইলে reply করে `/q` দাও!")
            return
        text = msg.text.split('/q', 1)[-1].strip()
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if len(lines) < 4:
            await msg.reply_text("📝 *Quiz Setup*\n\n৪টা info একসাথে লিখো:\n1️⃣ Quiz Name\n2️⃣ Description\n3️⃣ Timer (seconds)\n4️⃣ Shuffle (Yes/No)\n\n👆 সিরিয়ালি!")
            return
        quiz_name = lines[0]
        description = lines[1]
        timer = int(lines[2]) if lines[2].isdigit() else 15
        shuffle = lines[3].lower() == 'yes'
        file = await msg.reply_to_message.document.get_file()
        csv_bytes = await file.download_as_bytearray()
        csv_text = csv_bytes.decode('utf-8-sig')
        questions = parse_csv_to_quiz(csv_text)
        if not questions:
            await msg.reply_text("❌ CSV-তে কোনো প্রশ্ন পাওয়া যায়নি!")
            return
        quiz_id = generate_quiz_id()
        await db.execute('INSERT INTO quizzes (id, name, description, timer, shuffle, csv_data, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)', (quiz_id, quiz_name, description, timer, 1 if shuffle else 0, json.dumps(questions), update.effective_user.id))
        quiz_link = f"https://t.me/atlasQuizProBot?start={quiz_id}"
        await msg.reply_text(f"✅ *Quiz Created Successfully!*\n\n📝 *Name:* {quiz_name}\n📄 *Description:* {description}\n⏱️ *Timer:* {timer}s\n🔀 *Shuffle:* {'Yes' if shuffle else 'No'}\n📊 *Questions:* {len(questions)}\n\n🔗 *Quiz Link:*\n{quiz_link}\n\n👆 যে কেউ এই লিংকে ক্লিক করে কুইজ solve করতে পারবে!", parse_mode=None)
    except Exception as e:
        logger.error(f"Quiz create error: {e}")
        await update.message.reply_text("❌ কিছু সমস্যা হয়েছে!")
# ============================================================
# SECTION 4: QUIZ START (Deep Link)
# ============================================================
async def quiz_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start qz_* — Start quiz"""
    try:
        args = context.args
        if not args or not args[0].startswith('qz_'):
            return  # Not a quiz start
        
        quiz_id = args[0]
        quiz = await db.fetchone('SELECT * FROM quizzes WHERE id = ?', (quiz_id,))
        if not quiz:
            await update.message.reply_text("❌ কুইজ পাওয়া যায়নি! লিংক ভুল হতে পারে!")
            return
        
        quiz_name = quiz[1]
        description = quiz[2]
        timer = quiz[3]
        shuffle = quiz[4]
        csv_data = quiz[5]
        tag = quiz[6] or ''
        
        questions = parse_csv_to_quiz(csv_data)
        if shuffle:
            questions = shuffle_questions(questions)
        
        # Save session
        session_data = {
            'quiz_id': quiz_id,
            'questions': questions,
            'current': 0,
            'total': len(questions),
            'right': 0,
            'wrong': 0,
            'skip': 0,
            'timer': timer,
            'tag': tag,
            'results': []
        }
        
        if not context.chat_data.get('quiz_sessions'):
            context.chat_data['quiz_sessions'] = {}
        context.chat_data['quiz_sessions'][quiz_id] = session_data
        
        # Show quiz info
        await update.message.reply_text(
            f"📝 *{quiz_name}*\n"
            f"📄 {description}\n"
            f"⏱️ Timer: {timer}s per question\n"
            f"📊 Total: {len(questions)} questions\n\n"
            f"⏳ Quiz starting in 3 seconds...",
            parse_mode=None
        )
        
        await asyncio.sleep(3)
        await send_next_question(update, context, quiz_id)
        
    except Exception as e:
        logger.error(f"Quiz start error: {e}")
        await update.message.reply_text("❌ কিছু সমস্যা হয়েছে!")

# ============================================================
# SECTION 5: SEND QUESTION
# ============================================================
async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: str):
    """Send next quiz question as native Telegram poll"""
    try:
        sessions = context.chat_data.get('quiz_sessions', {})
        session = sessions.get(quiz_id)
        if not session:
            return
        
        current = session['current']
        if current >= session['total']:
            await finish_quiz(update, context, quiz_id)
            return
        
        question = session['questions'][current]
        q_text = question['question']
        options = question['options'][:10]  # Max 10 options
        correct_idx = question['answer_index']
        explanation = question.get('explanation', '')
        tag = session.get('tag', '')
        
        # Build question text
        if tag:
            q_text = f"{tag}\n{current+1}. {q_text}"
        else:
            q_text = f"{current+1}. {q_text}"
        
        # Send image if exists
        image_url = question.get('image_url', '')
        if image_url and ('http' in image_url):
            try:
                await update.message.reply_photo(photo=image_url)
            except:
                pass
        
        # Send poll
        poll_msg = await context.bot.send_poll(
            chat_id=update.effective_chat.id,
            question=q_text[:300],
            options=[opt[:100] for opt in options],
            type='quiz',
            correct_option_id=correct_idx,
            open_period=session['timer'],
            is_anonymous=True,
            explanation=explanation[:200] if explanation else None
        )
        
        # Update session
        session['current_poll_id'] = poll_msg.poll.id
        session['current_question'] = current
        session['current_correct'] = correct_idx
        
    except Exception as e:
        logger.error(f"Send question error: {e}")

# ============================================================
# SECTION 6: POLL ANSWER HANDLER
# ============================================================
async def quiz_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll_answer — Track scores"""
    try:
        poll_answer = update.poll_answer
        user_id = poll_answer.user.id
        option_ids = poll_answer.option_ids
        
        # Find active session
        for quiz_id, session in context.chat_data.get('quiz_sessions', {}).items():
            if session.get('current_poll_id') == poll_answer.poll_id:
                current = session['current']
                correct = session.get('current_correct', -1)
                
                if not option_ids:
                    # No answer = skip
                    session['skip'] += 1
                elif option_ids[0] == correct:
                    session['right'] += 1
                else:
                    session['wrong'] += 1
                
                session['current'] += 1
                
                # Wait for poll close then send next
                # Check if quiz finished
                if session['current'] >= session['total']:
                    await finish_quiz(update, context, quiz_id)
                    return
                await asyncio.sleep(2)
                await send_next_question(update, context, quiz_id)
                break
                
    except Exception as e:
        logger.error(f"Answer handler error: {e}")

# ============================================================
# SECTION 7: FINISH QUIZ + RESULT
# ============================================================
async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: str):
    """Finish quiz and show result"""
    try:
        sessions = context.chat_data.get('quiz_sessions', {})
        session = sessions.pop(quiz_id, {})
        if not session:
            return
        
        total = session['total']
        right = session['right']
        wrong = session['wrong']
        skip = session['skip']
        score = f"{right}/{total}"
        percentage = int(right / total * 100) if total > 0 else 0
        
        # Get motamot
        emoji, motamot_text = get_motamot(percentage)
        
        # Get user info
        user = update.effective_user
        user_name = user.first_name or "Student"
        
        # Quiz info
        quiz = await db.fetchone('SELECT name FROM quizzes WHERE id = ?', (quiz_id,))
        quiz_name = quiz[0] if quiz else "Quiz"
        
        # Save result
        existing = await db.fetchone(
            'SELECT COUNT(*) as cnt FROM quiz_results WHERE user_id = ? AND quiz_id = ?',
            (user.id, quiz_id)
        )
        attempt = (existing[0] if existing else 0) + 1
        
        await db.execute('''
            INSERT INTO quiz_results (user_id, user_name, quiz_id, right_count, wrong_count, skip_count, total, score, attempt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user.id, user_name, quiz_id, right, wrong, skip, total, score, attempt))
        
        # Update leaderboard
        await db.execute('''
            INSERT OR REPLACE INTO quiz_leaderboard (quiz_id, user_id, user_name, score, right_count, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (quiz_id, user.id, user_name, score, right, total))
        
        # History
        history_text = ""
        if attempt > 1:
            prev_results = await db.fetchall(
                'SELECT score, attempt FROM quiz_results WHERE user_id = ? AND quiz_id = ? ORDER BY attempt',
                (user.id, quiz_id)
            )
            if len(prev_results) >= 2:
                prev_score = prev_results[-2][0]
                history_text = f"\n\n📈 *Progress:*\n🟢 Previous: {prev_score}\n🟢 Now: {score}"
                if right > int(prev_score.split('/')[0]):
                    history_text += f"\n🎉 উন্নতি হয়েছে!"
        
        # Buttons
        keyboard = [
            [
                InlineKeyboardButton("📌 আবার প্রাক্টিস করো", url=f"https://t.me/{(await context.bot.get_me()).username}?start={quiz_id}"),
            ],
            [
                InlineKeyboardButton("👥 Leaderboard", callback_data=f"quiz_leaderboard_{quiz_id}"),
                InlineKeyboardButton("📈 History", callback_data=f"quiz_history_{quiz_id}"),
            ]
        ]
        
        result_text = (
            f"🌟 এটলাসের *{quiz_name}* কুইজে অংশগ্রহণ করায়\n"
            f"অভিনন্দন প্রিয় শিক্ষার্থী *{user_name}*!\n\n"
            f"📊 *তোমার রেজাল্ট:*\n"
            f"✅ Right: {right}\n"
            f"❌ Wrong: {wrong}\n"
            f"😐 Skipped: {skip}\n\n"
            f"⚡ *Final Result:* {score} ({percentage}%)\n\n"
            f"{emoji} _{motamot_text}_"
            f"{history_text}"
        )
        
        await update.message.reply_text(
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None
        )
        
    except Exception as e:
        logger.error(f"Finish quiz error: {e}")

# ============================================================
# SECTION 8: LEADERBOARD + HISTORY CALLBACKS
# ============================================================
async def quiz_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz inline button callbacks"""
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data.startswith('quiz_leaderboard_'):
            quiz_id = query.data.replace('quiz_leaderboard_', '')
            leaderboard = await db.fetchall('''
                SELECT user_name, score, right_count, total FROM quiz_leaderboard 
                WHERE quiz_id = ? ORDER BY CAST(SUBSTR(score, 1, INSTR(score, '/')-1) AS INTEGER) DESC LIMIT 10
            ''', (quiz_id,))
            
            if leaderboard:
                text = f"🏆 *Leaderboard*\n\n"
                medals = ['🥇', '🥈', '🥉']
                for i, (name, score, right, total) in enumerate(leaderboard):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    text += f"{medal} {name} — {score} ({int(right/total*100) if total else 0}%)\n"
            else:
                text = "এখনো কেউ quiz solve করেনি!"
            
            await query.edit_message_text(text, parse_mode=None)
        
        elif query.data.startswith('quiz_history_'):
            quiz_id = query.data.replace('quiz_history_', '')
            user_id = query.from_user.id
            history = await db.fetchall(
                'SELECT score, attempt, created_at FROM quiz_results WHERE user_id = ? AND quiz_id = ? ORDER BY attempt',
                (user_id, quiz_id)
            )
            
            if history:
                text = f"📈 *তোমার Progress*\n\n"
                for score, attempt, date in history:
                    text += f"🟢 Attempt {attempt}: {score} — {date[:10]}\n"
            else:
                text = "এখনো কোনো history নেই!"
            
            await query.edit_message_text(text, parse_mode=None)
            
    except Exception as e:
        logger.error(f"Callback error: {e}")

# ============================================================
# SECTION 9: SETTINGS
# ============================================================
async def tagQ_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set quiz tag — /tagQ"""
    try:
        args = context.args
        if not args:
            tag = await db.fetchone("SELECT tag FROM quizzes WHERE created_by = ? ORDER BY created_at DESC LIMIT 1", (update.effective_user.id,))
            if tag and tag[0]:
                await update.message.reply_text(f"🔖 Current tag: {tag[0]}\n\nChange: /tagQ New Tag")
            else:
                await update.message.reply_text("❌ No tag set!\nSet: /tagQ ATLAS 📚")
            return
        
        tag_text = ' '.join(args)

        await db.execute("INSERT OR REPLACE INTO quiz_settings (id, tag) VALUES (1, ?)", (tag_text,))
        await db.execute("UPDATE quizzes SET tag=?", (tag_text,))
        await update.message.reply_text(f"✅ Tag set: {tag_text} (permanent)")
    except Exception as e:
        await update.message.reply_text("❌ Error setting tag!")

async def expQ_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set explanation footer — /expQ"""
    try:
        args = context.args
        if not args:
            exp = await db.fetchone("SELECT exp_footer FROM quizzes WHERE created_by = ? ORDER BY created_at DESC LIMIT 1", (update.effective_user.id,))
            if exp and exp[0]:
                await update.message.reply_text(f"📝 Current footer: {exp[0]}\n\nChange: /expQ New Footer")
            else:
                await update.message.reply_text("❌ No footer set!\nSet: /expQ ✅ এটলাস")
            return
        
        footer = ' '.join(args)

        await db.execute("INSERT OR REPLACE INTO quiz_settings (id, exp_footer) VALUES (1, ?)", (footer,))
        await db.execute("UPDATE quizzes SET exp_footer=?", (footer,))
        await db.execute("UPDATE quizzes SET exp_footer=?", (footer,))
        await update.message.reply_text(f"✅ Footer set: {footer} (permanent)")
    except Exception as e:
        await update.message.reply_text("❌ Error setting footer!")

async def qlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all quizzes — /qlist"""
    try:
        quizzes = await db.fetchall("SELECT id, name, description, timer, created_at FROM quizzes ORDER BY created_at DESC LIMIT 10")
        if not quizzes:
            await update.message.reply_text("❌ কোনো quiz নেই!")
            return
        
        text = "📋 *All Quizzes*\n\n"
        for q_id, name, desc, timer, date in quizzes:
            text += f"📝 *{name}*\n⏱️ {timer}s | 🗓 {date[:10]}\n🔗 {q_id}\n\n"
        
        await update.message.reply_text(text, parse_mode=None)
    except Exception as e:
        await update.message.reply_text("❌ Error!")

async def qdel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete quiz — /qdel ID"""
    try:
        args = context.args
        if not args:
            await update.message.reply_text("❌ Quiz ID দাও! /qdel qz_abc123")
            return
        
        quiz_id = args[0]
        await db.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
        await db.execute("DELETE FROM quiz_results WHERE quiz_id = ?", (quiz_id,))
        await db.execute("DELETE FROM quiz_leaderboard WHERE quiz_id = ?", (quiz_id,))
        await update.message.reply_text(f"✅ Quiz deleted: {quiz_id}")
    except Exception as e:
        await update.message.reply_text("❌ Error deleting quiz!")

# ============================================================
# AUTO SETUP
# ============================================================
import asyncio as _asyncio
_asyncio.get_event_loop().create_task(setup_quiz_tables())

# ============================================================
# CLOUDFLARE SYNC — Send quiz to Cloudflare D1
# ============================================================
import aiohttp, json

async def sync_quiz_to_cloudflare(quiz_id, name, description, timer, shuffle, csv_text, tag='', exp_footer=''):
    """Sync quiz to Cloudflare Worker"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://atlasquizbot.hamza818483.workers.dev/create-quiz',
                json={
                    'id': quiz_id,
                    'name': name,
                    'desc': description,
                    'timer': timer,
                    'shuffle': shuffle,
                    'csv': json.dumps(questions),
                    'tag': tag,
                    'exp': exp_footer
                }
            ) as resp:
                result = await resp.json()
                if result.get('success'):
                    logger.info(f"Quiz synced to Cloudflare: {quiz_id}")
                    return True
    except Exception as e:
        logger.error(f"Cloudflare sync error: {e}")
    return False
