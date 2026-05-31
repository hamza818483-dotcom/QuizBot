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
    args = context.args
    if args and args[0].startswith('qz_'):
        qid = args[0]
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM quizzes WHERE id=?', (qid,))
        q = await cur.fetchone()
        await db.close()
        if not q:
            await update.message.reply_text("❌ Quiz not found!")
            return
        qs = json.loads(q['csv_data'])
        if q['shuffle']:
            random.shuffle(qs)
            for x in qs:
                co = x['options'][x['answer_index']]
                random.shuffle(x['options'])
                x['answer_index'] = x['options'].index(co)
        s = {'qid': qid, 'name': q['name'], 'qs': qs, 'cur': 0, 'tot': len(qs), 'right': 0, 'wrong': 0, 'skip': 0, 'timer': q['timer'] or 15, 'tag': q['tag'] or '', 'exp': q['exp_footer'] or '', 'chat': update.effective_chat.id, 'uname': update.effective_user.first_name or 'Student', 'pid': None, 'cor': None}
        QUIZ_SESSIONS[update.effective_user.id] = s
        await update.message.reply_text(f"📝 **{q['name']}**\n⏳ Starting...", parse_mode=None)
        await asyncio.sleep(3)
        await send_question(update.effective_user.id, context)
    else:
        await update.message.reply_text("🌟 ATLAS Quiz Bot\n\n🔗 Quiz link দিয়ে start করুন!")

async def send_question(uid, context):
    s = QUIZ_SESSIONS.get(uid)
    if not s or s['cur'] >= s['tot']: return await finish_quiz(uid, context)
    q = s['qs'][s['cur']]
    opts = q['options'][:10]
    tag = s['tag'] + '\n\n' if s['tag'] else ''
    que = f"{tag}{s['cur']+1}. {q['question']}"[:300]
    exp = (q.get('explanation', '') + '\n' + s['exp'] if s['exp'] else q.get('explanation', ''))[:200]
    if q.get('image_url', '').startswith('http'):
        await context.bot.send_photo(s['chat'], q['image_url'])
    msg = await context.bot.send_poll(chat_id=s['chat'], question=que, options=[o[:100] for o in opts], type='quiz', correct_option_id=q['answer_index'], open_period=s['timer'], is_anonymous=False, explanation=exp)
    s['pid'] = msg.poll.id
    s['cor'] = q['answer_index']

async def poll_answer(update, context):
    ans = update.poll_answer
    uid = ans.user.id
    s = QUIZ_SESSIONS.get(uid)
    if not s or s['pid'] != ans.poll_id: return
    oid = ans.option_ids or []
    if not oid: s['skip'] += 1
    elif oid[0] == s['cor']: s['right'] += 1
    else: s['wrong'] += 1
    s['cur'] += 1
    if s['cur'] >= s['tot']: await finish_quiz(uid, context)
    else: await send_question(uid, context)

async def finish_quiz(uid, context):
    s = QUIZ_SESSIONS.pop(uid, {})
    if not s: return
    tot, r, w, sk = s['tot'], s['right'], s['wrong'], s['skip']
    sc = f"{r}/{tot}"
    pct = round(r/tot*100) if tot > 0 else 0
    
    # Save to DB
    db = await aiosqlite.connect(DB_PATH)
    cur = await db.execute("SELECT COUNT(*) as cnt FROM quiz_results WHERE user_id=? AND quiz_id=?", (uid, s['qid']))
    att = ((await cur.fetchone())[0] or 0) + 1
    await db.execute("INSERT INTO quiz_results (user_id, user_name, quiz_id, right_count, wrong_count, skip_count, total, score, attempt) VALUES (?,?,?,?,?,?,?,?,?)", (uid, s['uname'], s['qid'], r, w, sk, tot, sc, att))
    await db.execute("INSERT OR REPLACE INTO quiz_leaderboard (quiz_id, user_id, user_name, score, right_count, total) VALUES (?,?,?,?,?,?)", (s['qid'], uid, s['uname'], sc, r, tot))
    await db.commit()
    await db.close()
    
    # History text
    hist_text = ''
    if att > 1:
        db = await aiosqlite.connect(DB_PATH)
        cur = await db.execute("SELECT score FROM quiz_results WHERE user_id=? AND quiz_id=? ORDER BY attempt DESC LIMIT 2", (uid, s['qid']))
        rows = await cur.fetchall()
        await db.close()
        if len(rows) >= 2:
            diff = r - int(rows[1][0].split('/')[0])
            hist_text = f"\n\n📈 **Progress:**\n🟢 Previous: {rows[1][0]}\n🟢 Now: {sc}" + (' 🎉 উন্নতি!' if diff > 0 else '')
    
    mot = '🏆 অসাধারণ! তুমি সেরা!' if pct>=90 else '🎉 চমৎকার! খুব ভালো করেছো!' if pct>=70 else '👍 মোটামুটি ভালো! আরও পড়ো!' if pct>=50 else '📚 পড়া হয়নি! আবার চেষ্টা করো!'
    link = f"https://t.me/atlasQuizProBot?start={s['qid']}"
    txt = f"🌟 এটলাসের **{s['name']}** কুইজে অংশগ্রহণ করার তোমাকে অভিনন্দন প্রিয় শিক্ষার্থী **{s['uname']}**!\n\n📊 **তোমার রেজাল্ট:**\n✅ Right: {r}\n❌ Wrong: {w}\n😐 Skipped: {sk}\n\n⚡ **Final Result:** {sc} ({pct}%)\n\n{mot}{hist_text}\n\n📌 **আবার প্রাক্টিস করো** (Unlimited)\n🔗 {link}"
    kb = [[InlineKeyboardButton("📌 আবার প্রাক্টিস করো", url=link), InlineKeyboardButton("👥 Leaderboard", callback_data=f"lb_{s['qid']}"), InlineKeyboardButton("📈 History", callback_data=f"hist_{s['qid']}")]]
    await context.bot.send_message(s['chat'], txt, parse_mode=None, reply_markup=InlineKeyboardMarkup(kb))

async def callback(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data
    if d.startswith('lb_'):
        db = await aiosqlite.connect(DB_PATH)
        cur = await db.execute("SELECT user_name, score, right_count, total FROM quiz_leaderboard WHERE quiz_id=? ORDER BY right_count DESC LIMIT 10", (d.replace('lb_', ''),))
        rows = await cur.fetchall()
        await db.close()
        medals = ['🥇', '🥈', '🥉']
        txt = '🏆 **Leaderboard**\n\n'
        if rows:
            for i, r in enumerate(rows):
                pct = round(r[2]/r[3]*100) if r[3] > 0 else 0
                txt += f"{medals[i] if i<3 else f'{i+1}.'} {r[0]} — {r[1]} ({pct}%)\n"
        else: txt += 'No data yet.'
        await q.message.reply_text(txt, parse_mode=None)
    elif d.startswith('hist_'):
        db = await aiosqlite.connect(DB_PATH)
        cur = await db.execute("SELECT score, attempt FROM quiz_results WHERE user_id=? AND quiz_id=? ORDER BY attempt", (q.from_user.id, d.replace('hist_', '')))
        rows = await cur.fetchall()
        await db.close()
        txt = '📈 **Progress**\n\n'
        if rows:
            for r in rows: txt += f"🟢 Attempt {r[1]}: {r[0]}\n"
        else: txt += 'No history yet.'
        await q.message.reply_text(txt, parse_mode=None)

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(PollAnswerHandler(poll_answer))
app.add_handler(CallbackQueryHandler(callback))

if __name__ == '__main__':
    print("🚀 Quiz Bot DB Starting...")
    app.run_polling(drop_pending_updates=True)
