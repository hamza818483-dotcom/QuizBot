#!/usr/bin/env python3
"""ATLAS Quiz Bot - DEBUG VERSION"""
import os, json, asyncio, random, logging, aiosqlite, traceback, sys
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, PollAnswerHandler, CallbackQueryHandler, PollHandler, ContextTypes

# Setup logging to file AND console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('quiz_debug.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

TOKEN = "8672553290:AAGVPBir4iqGFi5NEQeIHd5-rYto82XQ4jU"
DB_PATH = '/data/data/com.termux/files/home/AtlasMasterBot/data/atlas_bot.db'
QUIZ_SESSIONS = {}
LEADERBOARD = {}
HISTORY = {}

logger.info("="*50)
logger.info("QUIZ BOT STARTING - DEBUG MODE")
logger.info(f"DB: {DB_PATH}")
logger.info("="*50)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        uid = update.effective_user.id
        logger.info(f"START: user={uid}, args={args}")
        
        if args and args[0].startswith('qz_'):
            qid = args[0]
            logger.info(f"Looking for quiz: {qid}")
            
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            cur = await db.execute('SELECT * FROM quizzes WHERE id=?', (qid,))
            q = await cur.fetchone()
            await db.close()
            
            if not q:
                logger.error(f"Quiz NOT FOUND: {qid}")
                await update.message.reply_text(f"❌ Quiz not found!\nID: {qid}")
                return
            
            logger.info(f"Quiz found: {q['name']}, timer={q['timer']}")
            qs = json.loads(q['csv_data'])
            logger.info(f"Questions loaded: {len(qs)}")
            
            if q['shuffle']:
                random.shuffle(qs)
                for x in qs:
                    co = x['options'][x['answer_index']]
                    random.shuffle(x['options'])
                    x['answer_index'] = x['options'].index(co)
                logger.info("Questions shuffled")
            
            s = {
                'qid': qid, 'name': q['name'], 'qs': qs, 'cur': 0, 'tot': len(qs),
                'right': 0, 'wrong': 0, 'skip': 0, 'timer': q['timer'] or 15,
                'tag': q['tag'] or '', 'exp': q['exp_footer'] or '',
                'chat': update.effective_chat.id, 'uname': update.effective_user.first_name or 'Student',
                'pid': None, 'cor': None
            }
            QUIZ_SESSIONS[uid] = s
            logger.info(f"Session created for user={uid}, total={s['tot']}")
            
            await update.message.reply_text(f"📝 *{q['name']}*\n⏳ Starting...", parse_mode=None)
            await asyncio.sleep(3)
            await send_question(uid, context)
        else:
            logger.info(f"Normal start, no quiz ID")
            await update.message.reply_text("🌟 ATLAS Quiz Bot\n\n🔗 Quiz link দিয়ে start করুন!")
    except Exception as e:
        logger.error(f"START ERROR: {traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def send_question(uid, context):
    try:
        s = QUIZ_SESSIONS.get(uid)
        if not s:
            logger.error(f"No session for user={uid}")
            return
        if s['cur'] >= s['tot']:
            logger.info(f"All questions done, finishing quiz for user={uid}")
            return await finish_quiz(uid, context)
        
        q = s['qs'][s['cur']]
        logger.info(f"Sending Q{s['cur']+1}/{s['tot']} to user={uid}")
        opts = q['options'][:10]
        tag = s['tag'] + '\n\n' if s['tag'] else ''
        que = f"{tag}{s['cur']+1}. {q['question']}"[:300]
        exp = (q.get('explanation', '') + '\n' + s['exp'] if s['exp'] else q.get('explanation', ''))[:200]
        
        if q.get('image_url', '').startswith('http'):
            await context.bot.send_photo(s['chat'], q['image_url'])
            logger.info(f"Image sent")
        
        msg = await context.bot.send_poll(
            chat_id=s['chat'], question=que,
            options=[o[:100] for o in opts],
            type='quiz', correct_option_id=q['answer_index'],
            open_period=s['timer'], is_anonymous=False, explanation=exp
        )
        s['pid'] = msg.poll.id
        s['cor'] = q['answer_index']
        logger.info(f"Poll sent: id={msg.poll.id}, correct={q['answer_index']}")
    except Exception as e:
        logger.error(f"SEND Q ERROR: {traceback.format_exc()}")

async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ans = update.poll_answer
        uid = ans.user.id
        logger.info(f"POLL ANSWER: user={uid}, poll={ans.poll_id}, options={ans.option_ids}")
        
        s = QUIZ_SESSIONS.get(uid)
        if not s:
            logger.error(f"No session for user={uid}")
            return
        if s['pid'] != ans.poll_id:
            logger.error(f"Poll ID mismatch: session={s['pid']}, answer={ans.poll_id}")
            return
        
        oid = ans.option_ids or []
        if not oid:
            s['skip'] += 1
            logger.info(f"Skipped")
        elif oid[0] == s['cor']:
            s['right'] += 1
            logger.info(f"Correct!")
        else:
            s['wrong'] += 1
            logger.info(f"Wrong")
        
        s['cur'] += 1
        logger.info(f"Progress: {s['cur']}/{s['tot']} | R={s['right']} W={s['wrong']} S={s['skip']}")
        
        if s['cur'] >= s['tot']:
            logger.info(f"All done! Finishing quiz for user={uid}")
            await finish_quiz(uid, context)
        else:
            await asyncio.sleep(2)
            await send_question(uid, context)
    except Exception as e:
        logger.error(f"POLL ANSWER ERROR: {traceback.format_exc()}")

async def finish_quiz(uid, context):
    try:
        s = QUIZ_SESSIONS.pop(uid, {})
        if not s:
            logger.error(f"No session to finish for user={uid}")
            return
        
        tot, r, w, sk = s['tot'], s['right'], s['wrong'], s['skip']
        sc = f"{r}/{tot}"
        pct = round(r/tot*100) if tot > 0 else 0
        logger.info(f"FINISH: user={uid} | Score={sc} ({pct}%)")
        
        # Leaderboard
        lb = LEADERBOARD.get(s['qid'], [])
        lb.append({'name': s['uname'], 'score': sc, 'right': r, 'total': tot})
        lb.sort(key=lambda x: x['right'], reverse=True)
        LEADERBOARD[s['qid']] = lb[:10]
        logger.info(f"Leaderboard updated: {len(lb)} entries")
        
        # History
        hk = f"{uid}_{s['qid']}"
        prev = HISTORY.get(hk)
        hist_text = ''
        if prev:
            diff = r - prev['right']
            hist_text = f"\n\n📈 *Progress:*\n🟢 Previous: {prev['score']} ({prev['pct']}%)\n🟢 Now: {sc} ({pct}%)" + (' 🎉 উন্নতি!' if diff > 0 else '')
            logger.info(f"History found: prev={prev['score']}")
        else:
            logger.info("First attempt - no history")
        HISTORY[hk] = {'right': r, 'score': sc, 'pct': pct}
        
        mot = '🏆 অসাধারণ! তুমি সেরা!' if pct>=90 else '🎉 চমৎকার! খুব ভালো করেছো!' if pct>=70 else '👍 মোটামুটি ভালো! আরও পড়ো!' if pct>=50 else '📚 পড়া হয়নি! আবার চেষ্টা করো!'
        link = f"https://t.me/atlasQuizProBot?start={s['qid']}"
        txt = f"🌟 এটলাসের *{s['name']}* কুইজে অংশগ্রহণ করার তোমাকে অভিনন্দন প্রিয় শিক্ষার্থী *{s['uname']}*!\n\n📊 *তোমার রেজাল্ট:*\n✅ Right: {r}\n❌ Wrong: {w}\n😐 Skipped: {sk}\n\n⚡ *Final Result:* {sc} ({pct}%)\n\n{mot}{hist_text}\n\n📌 *আবার প্রাক্টিস করো* (Unlimited)\n🔗 {link}"
        
        logger.info(f"Result message:\n{txt[:200]}...")
        
        kb = [[
            InlineKeyboardButton("📌 আবার প্রাক্টিস করো", url=link),
            InlineKeyboardButton("👥 Leaderboard", callback_data=f"lb_{s['qid']}"),
            InlineKeyboardButton("📈 History", callback_data=f"hist_{s['qid']}")
        ]]
        
        await context.bot.send_message(s['chat'], txt, parse_mode=None, reply_markup=InlineKeyboardMarkup(kb))
        logger.info(f"Result sent with keyboard")
    except Exception as e:
        logger.error(f"FINISH ERROR: {traceback.format_exc()}")

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        d = q.data
        logger.info(f"CALLBACK: user={q.from_user.id}, data={d}")
        
        if d.startswith('lb_'):
            qid = d.replace('lb_', '')
            lb = LEADERBOARD.get(qid, [])
            logger.info(f"Leaderboard for {qid}: {len(lb)} entries")
            medals = ['🥇', '🥈', '🥉']
            txt = '🏆 *Leaderboard*\n\n'
            for i, r in enumerate(lb):
                txt += f"{medals[i] if i<3 else f'{i+1}.'} {r['name']} — {r['score']} ({round(r['right']/r['total']*100)}%)\n"
            if not lb: txt += 'No data yet.'
            await q.message.reply_text(txt, parse_mode=None)
            logger.info("Leaderboard sent")
        elif d.startswith('hist_'):
            hk = f"{q.from_user.id}_{d.replace('hist_', '')}"
            prev = HISTORY.get(hk)
            txt = '📈 *Progress*\n\n'
            txt += f"🟢 {prev['score']} ({prev['pct']}%)" if prev else 'No history yet.'
            await q.message.reply_text(txt, parse_mode=None)
            logger.info("History sent")
    except Exception as e:
        logger.error(f"CALLBACK ERROR: {traceback.format_exc()}")

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(PollAnswerHandler(poll_answer))
app.add_handler(CallbackQueryHandler(callback))

async def handle_poll(update, context):
    try:
        poll = update.poll
        if not poll or not poll.is_closed:
            return
        for uid, s in list(QUIZ_SESSIONS.items()):
            if s.get('pid') == poll.id:
                s['cur'] += 1
                if s['cur'] >= s['tot']:
                    await finish_quiz(uid, context)
                else:
                    await asyncio.sleep(2)
                    await send_question(uid, context)
                break
    except:
        pass

app.add_handler(PollHandler(handle_poll))

if __name__ == '__main__':
    print("🚀 Quiz Bot Starting (DEBUG)...")
    logger.info("All handlers registered")
    app.run_polling(drop_pending_updates=True)
