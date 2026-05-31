// ATLAS QUIZ BOT — Cloudflare Worker
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/webhook') {
      const update = await request.json();
      if (update.message) return handleMessage(update.message, env);
      if (update.poll_answer) return handlePollAnswer(update.poll_answer, env);
    }
    return new Response('ATLAS Quiz Bot Running!');
  }
};

async function handleMessage(msg, env) {
  const text = msg.text || '';
  const chatId = msg.chat.id;
  const args = text.split(' ');
  if (args[0] === '/start' && args[1] && args[1].startsWith('qz_')) {
    return startQuiz(chatId, args[1], env);
  }
  return sendMessage(chatId, '🌟 ATLAS Quiz Bot\\n\\n🔗 Quiz link দিয়ে start করুন!', env);
}

async function handlePollAnswer(answer, env) {
  const userId = answer.user.id;
  const pollId = answer.poll_id;
  const optionIds = answer.option_ids || [];
  const session = await env.QUIZ_SESSIONS.get(`session_${userId}`, 'json');
  if (!session || session.current_poll_id !== pollId) return new Response('OK');
  if (optionIds.length === 0) session.skip += 1;
  else if (optionIds[0] === session.current_correct) session.right += 1;
  else session.wrong += 1;
  session.current += 1;
  if (session.current >= session.total) return finishQuiz(userId, session, env);
  await env.QUIZ_SESSIONS.put(`session_${userId}`, JSON.stringify(session));
  await sendNextQuestion(userId, session, env);
  return new Response('OK');
}

async function startQuiz(chatId, quizId, env) {
  const quiz = await env.DB.prepare('SELECT * FROM quizzes WHERE id = ?').bind(quizId).first();
  if (!quiz) return sendMessage(chatId, '❌ কুইজ পাওয়া যায়নি!', env);
  let questions;
  try { questions = JSON.parse(quiz.csv_data); } catch { return sendMessage(chatId, '❌ কুইজ ডাটা সমস্যা!', env); }
  const session = { quiz_id: quizId, questions, current: 0, total: questions.length, right: 0, wrong: 0, skip: 0, timer: quiz.timer || 15, tag: quiz.tag || '', chat_id: chatId, current_poll_id: null, current_correct: null };
  await env.QUIZ_SESSIONS.put(`session_${chatId}`, JSON.stringify(session));
  await sendMessage(chatId, `📝 *${quiz.name}*\\n⏳ কুইজ শুরু হচ্ছে...`, env);
  await new Promise(r => setTimeout(r, 2000));
  await sendNextQuestion(chatId, session, env);
  return new Response('OK');
}

async function sendNextQuestion(chatId, session, env) {
  if (session.current >= session.total) return finishQuiz(chatId, session, env);
  const q = session.questions[session.current];
  if (!q) return;
  const options = (q.options || []).slice(0, 10);
  const tag = session.tag ? `${session.tag}\\n` : '';
  const question = `${tag}${session.current + 1}. ${q.question || '?'}`.slice(0, 300);
  const resp = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendPoll`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, question, options: options.map(o => (o || '').slice(0, 100)), type: 'quiz', correct_option_id: q.answer_index || 0, open_period: session.timer || 15, is_anonymous: false, explanation: (q.explanation || '').slice(0, 200) })
  });
  const result = await resp.json();
  if (result.ok && result.result) { session.current_poll_id = result.result.poll?.id; session.current_correct = q.answer_index || 0; await env.QUIZ_SESSIONS.put(`session_${chatId}`, JSON.stringify(session)); }
}

async function finishQuiz(userId, session, env) {
  const total = session.total, right = session.right, wrong = session.wrong, skip = session.skip;
  const score = `${right}/${total}`, pct = total > 0 ? Math.round(right / total * 100) : 0;
  await env.DB.prepare('INSERT INTO quiz_results (user_id, quiz_id, right_count, wrong_count, skip_count, score) VALUES (?, ?, ?, ?, ?, ?)').bind(userId, session.quiz_id, right, wrong, skip, score).run();
  await env.DB.prepare('INSERT OR REPLACE INTO quiz_leaderboard (quiz_id, user_id, score) VALUES (?, ?, ?)').bind(session.quiz_id, userId, score).run();
  let motamot = pct >= 90 ? '🏆 অসাধারণ!' : pct >= 70 ? '🎉 চমৎকার!' : pct >= 50 ? '👍 মোটামুটি ভালো!' : '📚 আরও পড়ো!';
  await sendMessage(session.chat_id || userId, `🌟 কুইজ শেষ!\\n\\n✅ Right: ${right}\\n❌ Wrong: ${wrong}\\n😐 Skipped: ${skip}\\n\\n⚡ ${score} (${pct}%)\\n\\n${motamot}`, env);
  await env.QUIZ_SESSIONS.delete(`session_${userId}`);
}

async function sendMessage(chatId, text, env) {
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'Markdown' }) });
  return new Response('OK');
}
