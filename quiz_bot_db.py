#!/usr/bin/env python3
"""ATLAS Quiz Bot - DB VERSION (Restart-Proof)"""
import os, json, asyncio, random, logging, aiosqlite, traceback, sys
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, PollAnswerHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

TOKEN = "8672553290:AAGVPBir4iqGFi5NEQeIHd5-rYto82XQ4jU"
DB_PATH = '/data/data/com.termux/files/home/AtlasMasterBot/data/atlas_bot.db'
QUIZ_SESSIONS = {}

async def start(update, context):
    try:
        args = context.args
        if args and args[0].startswith('qz_'):
            qid = args[0]
            logger.info(f"Quiz start requested: {qid} by user {update.effective_user.id}")
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            cur = await db.execute('SELECT * FROM quizzes WHERE id=?', (qid,))
            q = await cur.fetchone()
            await db.close()
            
            if not q:
                await update.message.reply_text("❌ Quiz not found!")
                logger.warning(f"Quiz {qid} not found in DB")
                return
            
            qs = json.loads(q['csv_data'])
            if q['shuffle']:
                random.shuffle(qs)
                for x in qs:
                    co = x['options'][x['answer_index']]
                    random.shuffle(x['options'])
                    x['answer_index'] = x['options'].index(co)
            
            s = {
                'qid': qid,
                'name': q['name'],
		'desc': q['description'] if q['description'] else '',
                'qs': qs,
                'cur': 0,
                'tot': len(qs),
                'right': 0,
                'wrong': 0,
                'skip': 0,
                'timer': q['timer'] or 15,
                'tag': q['tag'] or '',
                'exp': q['exp_footer'] or '',
                'chat': update.effective_chat.id,
                'uname': update.effective_user.first_name or 'Student',
                'pid': None,
                'cor': None
            }
            QUIZ_SESSIONS[update.effective_user.id] = s
            
            # COUNTDOWN START — Fix #1
            info_msg = (
                f"📝 {s['name']}\n"
                f"📄 {s['desc']}\n"
                f"⏱️ Timer: {s['timer']}s\n"
                f"📊 Questions: {s['tot']}"
            )
            await update.message.reply_text(info_msg, parse_mode=None)
            logger.info(f"Quiz info sent: {s['name']} | {s['tot']} Qs | {s['timer']}s timer")
            
            for count in ["3...", "2...", "1..."]:
                await asyncio.sleep(1)
                await update.message.reply_text(count, parse_mode=None)
            
            await asyncio.sleep(1)
            logger.info(f"Countdown complete, sending first question")
            await send_question(update.effective_user.id, context)
        else:
            await update.message.reply_text("🌟 ATLAS Quiz Bot\n\n🔗 Quiz link দিয়ে start করুন!")
    except Exception as e:
        logger.error(f"Start Error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error starting quiz: {e}", parse_mode=None)
async def auto_next_check(context):
    """Check if user answered, if not → skip + next"""
    try:
        data = context.job.data
        uid = data['uid']
        cur = data['cur']
        s = QUIZ_SESSIONS.get(uid)
        if s and s['cur'] == cur:  # User didn't answer
            s['skip'] += 1
            s['cur'] += 1
            logger.info(f"User {uid} Q{cur+1}: AUTO-SKIPPED (timer ended)")
            if s['cur'] >= s['tot']:
                await finish_quiz(uid, context)
            else:
                await send_question(uid, context)
    except Exception as e:
        logger.error(f"auto_next_check Error: {e}")
async def send_question(uid, context):
    try:
        s = QUIZ_SESSIONS.get(uid)
        if not s or s['cur'] >= s['tot']:
            logger.info(f"Quiz finished for user {uid}, calling finish_quiz")
            return await finish_quiz(uid, context)
        
        q = s['qs'][s['cur']]
        opts = q['options'][:10]
        
        # Tag spacing — Fix #2: '\n\n' for 1 line gap
        tag_part = (s['tag'] + '\n\n') if s['tag'] else ''
        
        # Clean question text — Fix #4: Remove \*
        q_text = q['question'].replace('\\*', '').replace('**', '')
        que = f"{tag_part}{s['cur']+1}. {q_text}"[:300]
        
        # Exp position — Fix #3: Footer at END of explanation
        exp_raw = q.get('explanation', '').replace('\\*', '').replace('**', '')
        if s['exp'] and exp_raw:
            exp = exp_raw + '\n' + s['exp'].replace('\\*', '').replace('**', '')
        elif s['exp']:
            exp = s['exp'].replace('\\*', '').replace('**', '')
        else:
            exp = exp_raw
        exp = exp[:200]
        
        # Clean option texts — Fix #4
        clean_opts = [o.replace('\\*', '').replace('**', '')[:100] for o in opts]
        
        logger.info(f"Sending Q{s['cur']+1}/{s['tot']} to user {uid}")
        
        if q.get('image_url', '').startswith('http'):
            await context.bot.send_photo(s['chat'], q['image_url'])
        
        msg = await context.bot.send_poll(
            chat_id=s['chat'],
            question=que,
            options=clean_opts,
            type='quiz',
            correct_option_id=q['answer_index'],
            open_period=s['timer'],
            is_anonymous=False,
            explanation=exp
        )
        s['pid'] = msg.poll.id
        s['cor'] = q['answer_index']
        logger.info(f"Poll sent: poll_id={s['pid']}, correct={s['cor']}")
        # Auto-next on timer end
        context.job_queue.run_once(auto_next_check, s['timer'], data={'uid': uid, 'cur': s['cur'], 'pid': s['pid']})

    except Exception as e:
        logger.error(f"send_question Error: {e}\n{traceback.format_exc()}")
        try:
            await context.bot.send_message(s['chat'], f"❌ Error: {e}", parse_mode=None)
        except:
            pass

async def poll_answer(update, context):
    try:
        ans = update.poll_answer
        uid = ans.user.id
        s = QUIZ_SESSIONS.get(uid)
        
        if not s:
            logger.warning(f"Poll answer from unknown session: user={uid}")
            return
        if s['pid'] != ans.poll_id:
            logger.warning(f"Poll ID mismatch: expected {s['pid']}, got {ans.poll_id}")
            return
        
        oid = ans.option_ids or []
        if not oid:
            s['skip'] += 1
            logger.info(f"User {uid} Q{s['cur']+1}: SKIPPED (timer ended)")
        elif oid[0] == s['cor']:
            s['right'] += 1
            logger.info(f"User {uid} Q{s['cur']+1}: CORRECT")
        else:
            s['wrong'] += 1
            logger.info(f"User {uid} Q{s['cur']+1}: WRONG (chose {oid[0]}, correct was {s['cor']})")
        
        s['cur'] += 1
        
        # Instant next — Fix #5: No delay
        if s['cur'] >= s['tot']:
            await finish_quiz(uid, context)
        else:
            await send_question(uid, context)
            
    except Exception as e:
        logger.error(f"poll_answer Error: {e}\n{traceback.format_exc()}")

async def finish_quiz(uid, context):
    try:
        s = QUIZ_SESSIONS.pop(uid, {})
        if not s:
            logger.warning(f"finish_quiz called but no session for user {uid}")
            return
        
        tot, r, w, sk = s['tot'], s['right'], s['wrong'], s['skip']
        sc = f"{r}/{tot}"
        pct = round(r/tot*100) if tot > 0 else 0
        
        logger.info(f"Quiz finished for {s['uname']}: {sc} ({pct}%) | R={r} W={w} S={sk}")
        
        # Save to DB
        db = await aiosqlite.connect(DB_PATH)
        
        # Count attempts
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM quiz_results WHERE user_id=? AND quiz_id=?",
            (uid, s['qid'])
        )
        row = await cur.fetchone()
        att = (row[0] or 0) + 1
        
        # Save all attempts to quiz_results
        await db.execute(
            "INSERT INTO quiz_results (user_id, user_name, quiz_id, right_count, wrong_count, skip_count, total, score, attempt, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, s['uname'], s['qid'], r, w, sk, tot, sc, att, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        logger.info(f"Saved to quiz_results: attempt #{att}")
        
        # Leaderboard — Fix #6: Only update if better score
        cur = await db.execute(
            "SELECT right_count FROM quiz_leaderboard WHERE quiz_id=? AND user_id=?",
            (s['qid'], uid)
        )
        existing = await cur.fetchone()
        if existing:
            if r > existing[0]:
                await db.execute(
                    "UPDATE quiz_leaderboard SET user_name=?, score=?, right_count=?, total=?, updated_at=? WHERE quiz_id=? AND user_id=?",
                    (s['uname'], sc, r, tot, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), s['qid'], uid)
                )
                logger.info(f"Leaderboard UPDATED: {s['uname']} improved to {r}/{tot}")
            else:
                logger.info(f"Leaderboard NOT updated: {r} <= previous best {existing[0]}")
        else:
            await db.execute(
                "INSERT INTO quiz_leaderboard (quiz_id, user_id, user_name, score, right_count, total, updated_at) VALUES (?,?,?,?,?,?,?)",
                (s['qid'], uid, s['uname'], sc, r, tot, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            logger.info(f"Leaderboard INSERTED: {s['uname']} {r}/{tot}")
        
        await db.commit()
        await db.close()
        
        
        # Motamot — 4 levels
        if pct >= 90:
            mot = '🏆 অসাধারণ! তুমি সেরা! আরও এগিয়ে যাও!'
        elif pct >= 70:
            mot = '🎉 চমৎকার! তুমি খুব ভালো করেছো! আরও প্র্যাকটিস করো!'
        elif pct >= 50:
            mot = '👍 মোটামুটি ভালো! আরও একটু পড়াশোনা করো!'
        else:
            mot = '📚 পড়া হয়নি! আবার পড়ে চেষ্টা করো!'
        
        link = f"https://t.me/atlasQuizProBot?start={s['qid']}"
        
        # Clean name — Fix #4
        clean_name = s['name'].replace('\\*', '').replace('**', '')
        clean_uname = s['uname'].replace('\\*', '').replace('**', '')
        
        txt = (
            f"🌟 এটলাসের {clean_name} কুইজে অংশগ্রহণ করার\n"
            f"তোমাকে অভিনন্দন প্রিয় শিক্ষার্থী {clean_uname}!\n\n"
            f"📊 তোমার রেজাল্ট:\n"
            f"✅ Right: {r}\n"
            f"❌ Wrong: {w}\n"
            f"😐 Skipped: {sk}\n\n"
            f"⚡ Final Result: {sc} ({pct}%)\n\n"
            f"{mot}\n\n"
            ""
            f"📌 আবার প্রাক্টিস করো (Unlimited)\n"
            f"🔗 {link}"
        )
        
        # Fix #6: All 3 buttons working
        kb = [[
            InlineKeyboardButton("📌 আবার প্রাক্টিস করো", url=link),
            InlineKeyboardButton("👥 Leaderboard", callback_data=f"lb_{s['qid']}"),
            InlineKeyboardButton("📈 History", callback_data=f"hist_{s['qid']}")
        ]]
        
        await context.bot.send_message(s['chat'], txt, parse_mode=None, reply_markup=InlineKeyboardMarkup(kb))
        logger.info(f"Result sent to {s['uname']}: {sc} ({pct}%)")
        
    except Exception as e:
        logger.error(f"finish_quiz Error: {e}\n{traceback.format_exc()}")
        try:
            await context.bot.send_message(s.get('chat', uid), f"❌ Error saving result: {e}", parse_mode=None)
        except:
            pass

async def callback(update, context):
    try:
        q = update.callback_query
        await q.answer()
        d = q.data
        uid = q.from_user.id
        
        if d.startswith('lb_'):
            qid = d.replace('lb_', '')
            logger.info(f"Leaderboard requested for quiz {qid} by user {uid}")
            
            db = await aiosqlite.connect(DB_PATH)
            # Fix #6: Show ALL users, not just top 10
            cur = await db.execute(
                "SELECT user_name, score, right_count, total FROM quiz_leaderboard WHERE quiz_id=? ORDER BY right_count DESC",
                (qid,)
            )
            rows = await cur.fetchall()
            await db.close()
            
            medals = ['🥇', '🥈', '🥉']
            txt = '🏆 Leaderboard\n\n'
            if rows:
                for i, r in enumerate(rows):
                    pct = round(r[2]/r[3]*100) if r[3] > 0 else 0
                    prefix = medals[i] if i < 3 else f'{i+1}.'
                    txt += f"{prefix} {r[0]} — {r[1]} ({pct}%)\n"
            else:
                txt += 'এখনো কেউ কুইজ দেয়নি!'
            
            await q.message.reply_text(txt, parse_mode=None)
            logger.info(f"Leaderboard sent: {len(rows)} users")
            
        elif d.startswith('hist_'):
            qid = d.replace('hist_', '')
            logger.info(f"History requested for quiz {qid} by user {uid}")
            
            db = await aiosqlite.connect(DB_PATH)
            # Fix #7: Show ALL attempts with date
            cur = await db.execute(
                "SELECT score, attempt, created_at FROM quiz_results WHERE user_id=? AND quiz_id=? ORDER BY attempt",
                (uid, qid)
            )
            rows = await cur.fetchall()
            await db.close()
            
            txt = '📈 Progress\n\n'
            if rows:
                for i, r in enumerate(rows):
                    txt += f"🟢 Attempt {r[1]}: {r[0]}"
                    if r[2]:
                        txt += f" | 📅 {r[2]}"
                    txt += '\n'
            else:
                txt += 'এখনো কোনো attempt নেই!'
            
            await q.message.reply_text(txt, parse_mode=None)
            logger.info(f"History sent: {len(rows)} attempts")
            
    except Exception as e:
        logger.error(f"Callback Error: {e}\n{traceback.format_exc()}")
        try:
            await q.message.reply_text(f"❌ Error: {e}", parse_mode=None)
        except:
            pass

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(PollAnswerHandler(poll_answer))
app.add_handler(CallbackQueryHandler(callback))

if __name__ == '__main__':
    print("🚀 Quiz Bot DB Starting...")
    logger.info("Quiz Bot DB Starting...")
    app.run_polling(drop_pending_updates=True)
