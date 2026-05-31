export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const DB = env.DB, QS = env.QUIZ_SESSIONS, BT = env.BOT_TOKEN;
    const TG = 'https://api.telegram.org/bot' + BT;

    if (url.pathname === '/create-quiz' && request.method === 'POST') {
      const d = await request.json();
      await DB.prepare('INSERT OR REPLACE INTO quizzes (id, name, description, timer, shuffle, csv_data, tag, exp_footer) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)').bind(d.id, d.name, d.desc, d.timer, d.shuffle ? 1 : 0, JSON.stringify(d.csv), d.tag || '', d.exp || '').run();
      return Response.json({ success: true });
    }

    if (url.pathname === '/webhook') {
      const up = await request.json();
      if (up.message) return handleMsg(up.message, DB, QS, TG);
      if (up.poll_answer) return pollAns(up.poll_answer, QS, TG);
      if (up.callback_query) return cb(up.callback_query, DB, QS, TG);
      return new Response('OK');
    }

    return new Response('ATLAS Quiz Bot Running!');
  }
};

async function handleMsg(m, DB, QS, TG) {
  const t = m.text || '', c = m.chat.id, a = t.split(' ');
  if (a[0] === '/start' && a[1]?.startsWith('qz_')) return startQuiz(c, a[1], m.from, DB, QS, TG);
  return sendMsg(c, '🌟 ATLAS Quiz Bot\n\n🔗 Quiz link দিয়ে start করুন!', TG);
}

async function pollAns(a, QS, TG) {
  const uid = a.user.id, pid = a.poll_id, oid = a.option_ids || [];
  const s = await QS.get('s_' + uid, 'json');
  if (!s || s.pid !== pid) return new Response('OK');
  oid.length === 0 ? s.skip++ : oid[0] === s.cor ? s.right++ : s.wrong++;
  s.cur++;
  if (s.cur >= s.tot) return finQuiz(uid, s, a.user, QS, TG);
  await QS.put('s_' + uid, JSON.stringify(s), { expirationTtl: 3600 });
  await sendQ(uid, s, QS, TG);
  return new Response('OK');
}

async function startQuiz(cid, qid, user, DB, QS, TG) {
  const q = await DB.prepare('SELECT * FROM quizzes WHERE id=?1').bind(qid).first();
  if (!q) return sendMsg(cid, '❌ কুইজ পাওয়া যায়নি!', TG);
  let qs = JSON.parse(q.csv_data);
  if (q.shuffle) { qs = qs.sort(() => Math.random() - 0.5); qs = qs.map(x => { const co = x.options[x.answer_index]; x.options = x.options.sort(() => Math.random() - 0.5); x.answer_index = x.options.indexOf(co); return x; }); }
  const s = { qid, name: q.name, qs, cur: 0, tot: qs.length, right: 0, wrong: 0, skip: 0, timer: q.timer || 15, tag: q.tag || '', exp: q.exp_footer || '', cid, uname: user.first_name || 'Student', pid: null, cor: null };
  await QS.put('s_' + cid, JSON.stringify(s), { expirationTtl: 3600 });
  await sendMsg(cid, '📝 *' + q.name + '*\n📄 ' + (q.description || '') + '\n⏱️ Timer: ' + q.timer + 's\n📊 Questions: ' + qs.length + '\n\n⏳ কুইজ শুরু হচ্ছে...', TG);
  await new Promise(r => setTimeout(r, 3000));
  await sendQ(cid, s, QS, TG);
  return new Response('OK');
}

async function sendQ(cid, s, QS, TG) {
  if (s.cur >= s.tot) return finQuiz(cid, s, { first_name: s.uname }, QS, TG);
  const q = s.qs[s.cur]; if (!q) return;
  if (q.image_url?.startsWith('http')) { await fetch(TG + '/sendPhoto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: cid, photo: q.image_url }) }); }
  const opts = (q.options || []).slice(0, 10), tag = s.tag ? s.tag + '\n' : '';
  const que = (tag + (s.cur + 1) + '. ' + (q.question || '?')).slice(0, 300);
  const exp = (s.exp ? (q.explanation || '') + '\n' + s.exp : (q.explanation || '')).slice(0, 200);
  const r = await (await fetch(TG + '/sendPoll', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: cid, question: que, options: opts.map(o => (o || '').slice(0, 100)), type: 'quiz', correct_option_id: q.answer_index || 0, open_period: s.timer || 15, is_anonymous: false, explanation: exp }) })).json();
  if (r.ok && r.result) { s.pid = r.result.poll?.id; s.cor = q.answer_index || 0; await QS.put('s_' + cid, JSON.stringify(s), { expirationTtl: 3600 }); }
}

async function finQuiz(uid, s, user, QS, TG) {
  const tot = s.tot, r = s.right, w = s.wrong, sk = s.skip, sc = r + '/' + tot, pct = tot > 0 ? Math.round(r / tot * 100) : 0;
  const uname = user.first_name || s.uname || 'Student', qname = s.name || 'Quiz';
  
  // Leaderboard save
  const lbKey = 'lb_' + s.qid;
  let lb = await QS.get(lbKey, 'json') || [];
  lb.push({ name: uname, score: sc, right: r, total: tot });
  lb.sort((a, b) => b.right - a.right);
  lb = lb.slice(0, 10);
  await QS.put(lbKey, JSON.stringify(lb));
  
  // History
  const cnt = await QS.get('cnt_' + uid + '_' + s.qid);
  const att = (parseInt(cnt) || 0) + 1;
  await QS.put('cnt_' + uid + '_' + s.qid, String(att));
  const prev = await QS.get('prev_' + uid + '_' + s.qid);
  let histText = '';
  if (prev) {
    const p = JSON.parse(prev);
    const diff = r - p.right;
    histText = '\n\n📈 *Progress:*\n🟢 Previous: ' + p.score + ' (' + p.pct + '%)\n🟢 Now: ' + sc + ' (' + pct + '%)' + (diff > 0 ? ' 🎉 উন্নতি!' : '');
  }
  await QS.put('prev_' + uid + '_' + s.qid, JSON.stringify({ right: r, score: sc, pct: pct }));
  
  const mot = pct >= 90 ? '🏆 অসাধারণ! তুমি সেরা! আরও এগিয়ে যাও!' : pct >= 70 ? '🎉 চমৎকার! তুমি খুব ভালো করেছো! আরও প্র্যাকটিস করো!' : pct >= 50 ? '👍 মোটামুটি ভালো! আরও একটু পড়াশোনা করো!' : '📚 পড়া হয়নি! আবার পড়ে চেষ্টা করো!';
  const link = 'https://t.me/atlasQuizProBot?start=' + s.qid;
  const txt = '🌟 এটলাসের *' + qname + '* কুইজে অংশগ্রহণ করার তোমাকে অভিনন্দন প্রিয় শিক্ষার্থী *' + uname + '*!\n\n📊 *তোমার রেজাল্ট:*\n✅ Right: ' + r + '\n❌ Wrong: ' + w + '\n😐 Skipped: ' + sk + '\n\n⚡ *Final Result:* ' + sc + ' (' + pct + '%)\n\n' + mot + histText + '\n\n📌 *আবার প্রাক্টিস করো* (Unlimited)\n🔗 ' + link;
  
  await fetch(TG + '/sendMessage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: s.cid || uid, text: txt, parse_mode: 'Markdown', disable_web_page_preview: true, reply_markup: { inline_keyboard: [[{ text: '📌 আবার প্রাক্টিস করো', url: link }, { text: '👥 Leaderboard', callback_data: 'lb_' + s.qid }, { text: '📈 History', callback_data: 'hist_' + s.qid }]] } }) });
  await QS.delete('s_' + uid);
}

async function cb(q, DB, QS, TG) {
  const d = q.data, cid = q.message.chat.id;
  if (d.startsWith('lb_')) {
    const lb = await QS.get('lb_' + d.replace('lb_', ''), 'json') || [];
    let t = '🏆 *Leaderboard*\n\n';
    if (lb.length === 0) t += 'এখনো কেউ quiz solve করেনি!';
    else lb.forEach((r, i) => t += (['🥇','🥈','🥉'][i]||(i+1)+'.') + ' ' + r.name + ' — ' + r.score + ' (' + Math.round(r.right/r.total*100) + '%)\n');
    await fetch(TG + '/sendMessage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: cid, text: t, parse_mode: 'Markdown' }) });
  }
  if (d.startsWith('hist_')) {
    const uid = q.from.id, qid = d.replace('hist_', '');
    const prev = await QS.get('prev_' + uid + '_' + qid);
    let t = '📈 *তোমার Progress*\n\n';
    if (prev) { const p = JSON.parse(prev); t += '🟢 Latest: ' + p.score + ' (' + p.pct + '%)\n'; }
    else t += 'এখনো কোনো history নেই!';
    await fetch(TG + '/sendMessage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: cid, text: t, parse_mode: 'Markdown' }) });
  }
  return new Response('OK');
}

async function sendMsg(cid, txt, TG) {
  await fetch(TG + '/sendMessage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: cid, text: txt, parse_mode: 'Markdown', disable_web_page_preview: true }) });
  return new Response('OK');
}
