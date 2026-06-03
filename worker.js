// ============================================================
// ATLAS QUIZ BOT v6.0 — Full Quiz System
// 19 Features | D1 Permanent | Countdown 2s | All Buttons
// ============================================================

export default {
async queue(batch, env) {
  console.log('Queue triggered! Batch size:', batch.messages.length);
  console.log('ENV check - DB:', !!env.DB, 'Token:', !!env.QUIZ_BOT_TOKEN, 'QUIZ_BOT_TOKEN:', !!env.QUIZ_BOT_TOKEN);
  try {
    var token = env.QUIZ_BOT_TOKEN;
    var DB = env.DB;
    console.log('DB available:', !!DB, 'Token:', !!token);
    for (const msg of batch.messages) {
      try {
        var body = msg.body;
        console.log('Processing msg:', JSON.stringify(body));
        var sessionRow = await DB.prepare('SELECT data FROM quiz_sessions WHERE key = ?1').bind('s_' + body.uid).first();
        console.log('Session found:', !!sessionRow);
        if (!sessionRow) continue;
        var session = JSON.parse(sessionRow.data);
        console.log('Queue check:', body.curIndex, 'vs', session.cur, session.cur === body.curIndex ? 'MATCH' : 'SKIP');
        if (session.cur === body.curIndex) {
          var result = await fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              chat_id: body.chatId,
              text: '⏱️ দ্রুত সঠিকভাবে দাগানোর অভ্যাস করুন,\nপরবর্তী Quiz এ যেতে "Next" Button এ ক্লিক করুন।',
              reply_markup: { inline_keyboard: [[{ text: '⏭️ Next', callback_data: 'next_' + body.uid }]] }
            })
          });
          var sendResult = await result.json();
          console.log('Send result:', sendResult.ok);
        }
      } catch (e) {
        console.error('Msg error:', e.message);
      }
    }
  } catch (e) {
    console.error('Queue error:', e.message);
  }
},

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    globalThis.DB = env.DB;
    globalThis.BOT_TOKEN = env.BOT_TOKEN;
    globalThis.QUIZ_BOT_TOKEN = env.QUIZ_BOT_TOKEN;
    globalThis.OWNER_ID = env.OWNER_ID;
    globalThis.GEMINI_KEYS = env.GEMINI_KEYS;
    globalThis.NEXT_QUEUE = env.NEXT_QUEUE;
    if (url.pathname === '/init-db') return await initDB();
    if (url.pathname === '/webhook') return await handleWebhook(request);
    return new Response('🚀 ATLAS QUIZ BOT v6.0 Running!');
  }
};

// ============================================================
// FEATURE 1: DATABASE (10 Tables)
// ============================================================
async function initDB() {
  try {
    await DB.exec("CREATE TABLE IF NOT EXISTS quizzes (id TEXT PRIMARY KEY, name TEXT, description TEXT, timer INTEGER DEFAULT 15, shuffle BOOLEAN DEFAULT 0, csv_data TEXT, tag TEXT DEFAULT '', exp_footer TEXT DEFAULT '', created_by INTEGER, created_at INTEGER DEFAULT (unixepoch()))");
    await DB.exec("CREATE TABLE IF NOT EXISTS quiz_results (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, user_name TEXT, quiz_id TEXT, right_count INTEGER DEFAULT 0, wrong_count INTEGER DEFAULT 0, skip_count INTEGER DEFAULT 0, total INTEGER, score TEXT, attempt INTEGER DEFAULT 1, created_at INTEGER DEFAULT (unixepoch()))");
    await DB.exec("CREATE TABLE IF NOT EXISTS quiz_leaderboard (quiz_id TEXT, user_id INTEGER, user_name TEXT, score TEXT, right_count INTEGER, total INTEGER, updated_at INTEGER, PRIMARY KEY (quiz_id, user_id))");
    await DB.exec("CREATE TABLE IF NOT EXISTS quiz_settings (id INTEGER PRIMARY KEY DEFAULT 1, tag TEXT DEFAULT '', exp_footer TEXT DEFAULT '')");
    await DB.exec("CREATE TABLE IF NOT EXISTS quiz_sessions (key TEXT PRIMARY KEY, data TEXT, updated_at INTEGER)");
    await DB.exec("CREATE TABLE IF NOT EXISTS quiz_preview (id INTEGER PRIMARY KEY DEFAULT 1, file_id TEXT)");
    await DB.exec("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)");
    await DB.exec("CREATE TABLE IF NOT EXISTS bot_users (user_id INTEGER PRIMARY KEY, user_name TEXT, first_seen INTEGER, last_seen INTEGER)");
    await DB.exec("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, title TEXT)");
    await DB.exec("CREATE TABLE IF NOT EXISTS quiz_question_results (id INTEGER PRIMARY KEY AUTOINCREMENT, result_id INTEGER, question_index INTEGER, result_type TEXT, quiz_id TEXT, user_id INTEGER, created_at INTEGER DEFAULT (unixepoch()))");
    await DB.exec("CREATE TABLE IF NOT EXISTS poll_sessions (poll_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, next_q_index INTEGER NOT NULL, session_uid INTEGER NOT NULL, created_at INTEGER DEFAULT (unixepoch()))");
    return new Response('✅ All tables created!');
  } catch (e) {
    return new Response('❌ Error: ' + e.message);
  }
}

// ============================================================
// FEATURE 2: UTILITY FUNCTIONS
// ============================================================
function cleanText(text) {
  return (text || '').replace(/\\\*/g, '').replace(/\*\*/g, '');
}

function extractImageUrl(text) {
  if (!text) {
    return { url: null, cleanText: text };
  }
  var match = text.match(/<img[^>]+src=["']([^"']+)["'][^>]*>/i);
  if (match) {
    return {
      url: match[1],
      cleanText: text.replace(/<img[^>]+>/i, '').trim()
    };
  }
  return { url: null, cleanText: text };
}

async function isOwnerOrAdmin(uid) {
  if (uid.toString() === OWNER_ID) {
    return true;
  }
  var admin = await DB.prepare('SELECT 1 FROM admins WHERE user_id=?1').bind(uid).first();
  return !!admin;
}

async function trackUser(uid, uname) {
  var now = Math.floor(Date.now() / 1000);
  var user = await DB.prepare('SELECT user_id FROM bot_users WHERE user_id=?1').bind(uid).first();
  if (user) {
    await DB.prepare('UPDATE bot_users SET user_name=?1, last_seen=?2 WHERE user_id=?3').bind(uname, now, uid).run();
  } else {
    await DB.prepare('INSERT INTO bot_users (user_id, user_name, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4)').bind(uid, uname, now, now).run();
  }
}

async function sendMsg(chatId, text, token, plain) {
  var body = { chat_id: chatId, text: text };
  if (plain) {
    body.parse_mode = 'HTML';
  }
  return await fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

async function sendPollMsg(chatId, question, options, correctIdx, timer, explanation, token, imgQuestion, imgOptions, imgExplanation) {
  var body = {
    chat_id: chatId,
    question: question.slice(0, 300),
    options: options.map(function(o) { return (o || '').slice(0, 100); }),
    type: 'quiz',
    correct_option_id: correctIdx || 0,
    open_period: timer || 15,
    is_anonymous: false,
    explanation: (explanation || '').slice(0, 200)
  };
  if (imgQuestion) {
    body.question_media = { type: 'photo', media: imgQuestion };
  }
  if (imgOptions && imgOptions.some(function(x) { return x; })) {
    body.options = options.map(function(opt, i) {
      var img = imgOptions[i];
      if (img) {
        return { text: (opt || '').slice(0, 100), media: { type: 'photo', media: img } };
      }
      return { text: (opt || '').slice(0, 100) };
    });
  }
  if (imgExplanation) {
    body.explanation_media = { type: 'photo', media: imgExplanation };
  }
  return await fetch('https://api.telegram.org/bot' + token + '/sendPoll', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

function parseCSV(csvText) {
  var questions = [];
  try {
    var clean = csvText.replace(/^\ufeff/, '').replace(/\t/g, ',');
    var lines = clean.split('\n').filter(function(l) { return l.trim(); });
    if (lines.length < 2) {
      return questions;
    }
    var headers = lines[0].split(',').map(function(h) { return h.trim().toLowerCase(); });
    var qIdx = headers.findIndex(function(h) { return h === 'question' || h === 'questions'; });
    var aIdx = headers.findIndex(function(h) { return h === 'answer' || h === 'correct'; });
    var eIdx = headers.findIndex(function(h) { return h === 'explanation'; });
    var iIdx = headers.findIndex(function(h) { return h === 'image' || h === 'qi'; });
    for (var i = 1; i < lines.length; i++) {
      var cols = lines[i].split(',').map(function(c) { return c.trim(); });
      var q = {
        question: cols[qIdx] || '',
        options: [],
        answer_index: 0,
        explanation: cols[eIdx] || '',
        image_url: cols[iIdx] || ''
      };
      for (var j = 0; j < cols.length; j++) {
        var h = headers[j];
        if (h && (h.startsWith('option') || ['a', 'b', 'c', 'd'].indexOf(h) !== -1)) {
          if (cols[j]) {
            q.options.push(cols[j]);
          }
        }
      }
      var ans = cols[aIdx] || '1';
      if (ans.match(/^[a-d]$/i)) {
        q.answer_index = ans.toLowerCase().charCodeAt(0) - 97;
      } else if (ans.match(/^\d+$/)) {
        q.answer_index = Math.max(0, parseInt(ans) - 1);
      }
      if (q.question && q.options.length >= 2) {
        questions.push(q);
      }
    }
  } catch (e) {
    console.error('CSV Parse:', e.message);
  }
  return questions;
}

// ============================================================
// FEATURE 3: WEBHOOK + ROUTER
// ============================================================
async function handleWebhook(request) {
  try {
    var update = await request.json();
    var token = QUIZ_BOT_TOKEN;
    if (update.message) {
      return await handleMsg(update.message, token);
    }
    if (update.poll_answer) {
      await handleQuizPoll(update.poll_answer);
      return new Response('OK');
    }



 if (update.callback_query) {
      await handleCB(update.callback_query, token);
      return new Response('OK');
    }
    return new Response('OK');
  } catch (e) {
    console.error('Webhook:', e.message);
    return new Response('OK');
  }
}

async function handleMsg(msg, token) {
  var text = (msg.text || '').trim();
  var chatId = msg.chat.id;
  var uid = msg.from ? msg.from.id : 0;
  var uname = msg.from ? (msg.from.first_name || 'User') : 'User';
  try {
    await trackUser(uid, uname);
    var isAuth = await isOwnerOrAdmin(uid);
    if (text.startsWith('/start qz_')) {
      await startQuiz(chatId, text.split(' ')[1], msg.from, token);
      return new Response('OK');
    }
    if (text === '/start') {
      await sendMsg(chatId, '🌟 ATLAS Quiz Bot\n\n🔗 Quiz link দিয়ে start করুন!\n\n📝 /q - Create Quiz\n📋 /qlist - List\n🗑️ /qdel - Delete\n🏷️ /tagQ - Tag\n📝 /expQ - Footer\n🖼️ /pre - Preview\n👑 /permit - Admin\n📤 /send - Broadcast\n📊 /info - Stats', token);
      return new Response('OK');
    }
    if (text === '/ping') {
      await sendMsg(chatId, '🏓 Pong! Quiz Bot Online!', token);
      return new Response('OK');
    }
    if (text === '/error') {
      await sendMsg(chatId, '✅ All systems running!', token);
      return new Response('OK');
    }
    if (!isAuth) {
      await sendMsg(chatId, '❌ Admin only!', token);
      return new Response('OK');
    }
    if (text.startsWith('/q') && !text.startsWith('/qlist') && !text.startsWith('/qdel')) {
      await handleQuizCreate(msg, token);
      return new Response('OK');
    }
    if (text === '/qlist') {
      await handleQlist(chatId, token);
      return new Response('OK');
    }
    if (text.startsWith('/qdel')) {
      await handleQdel(chatId, text.split(' ')[1], token);
      return new Response('OK');
    }
    if (text.startsWith('/tagQ')) {
      await handleTagQ(msg, token);
      return new Response('OK');
    }
    if (text.startsWith('/expQ')) {
      await handleExpQ(msg, token);
      return new Response('OK');
    }
    if (text.startsWith('/pre')) {
      await handlePre(msg, token);
      return new Response('OK');
    }
    if (text.startsWith('/permit') && uid.toString() === OWNER_ID) {
      await handlePermit(msg, token);
      return new Response('OK');
    }
    if (text === '/info' && uid.toString() === OWNER_ID) {
      await handleInfo(chatId, token);
      return new Response('OK');
    }
    if (text === '/send' && uid.toString() === OWNER_ID) {
      await handleSend(msg, token);
      return new Response('OK');
    }
    if (text.startsWith('/permit') || text === '/info' || text === '/send') {
      await sendMsg(chatId, '❌ Owner only!', token);
      return new Response('OK');
    }
    return new Response('OK');
  } catch (e) {
    console.error('Msg Error:', e.message);
    await sendMsg(chatId, '❌ ' + e.message, token);
    return new Response('OK');
  }
}

// ============================================================
// FEATURE 4: CALLBACKS (Smooth Buttons)
// ============================================================
async function handleCB(query, token) {
  var data = query.data;
  var chatId = query.message.chat.id;
  var uid = query.from.id;
  var answerCb = async function(text) {
    await fetch('https://api.telegram.org/bot' + token + '/answerCallbackQuery', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ callback_query_id: query.id, text: text || '' })
    });
  };
  try {
    await answerCb();
    if (data.startsWith('lb_')) {
      await handleLB(chatId, data.replace('lb_', ''), uid, token);
      return;
    }
    if (data.startsWith('hist_')) {
      await handleHist(chatId, data.replace('hist_', ''), uid, token);
      return;
    }
    if (data.startsWith('send_')) {
      await handleSendCB(query, token);
      return;
    }
    if (data.startsWith('next_')) {
      await handleNext(chatId, data.replace('next_', ''), token);
      return;
    }
    if (data.startsWith('mp1_')) {
      await handleMistake(chatId, data.replace('mp1_', ''), uid, token, 'wrong');
      return;
    }
    if (data.startsWith('mp2_')) {
      await handleMistake(chatId, data.replace('mp2_', ''), uid, token, 'wrong+skip');
      return;
    }
  } catch (e) {
    console.error('CB Error:', e.message);
  }
}

// ============================================================
// FEATURE 5: QUIZ CREATE
// ============================================================
async function handleQuizCreate(msg, token) {
  var chatId = msg.chat.id;
  var text = msg.text || '';
  if (!msg.reply_to_message || !msg.reply_to_message.document) {
    await sendMsg(chatId, '❌ CSV ফাইলে reply করে `/q` দাও!\n\n📝 Quiz Setup:\n1️⃣ Quiz Name\n2️⃣ Description\n3️⃣ Timer (seconds)\n4️⃣ Shuffle (Yes/No)', token);
    return;
  }
  var lines = text.split('/q')[1] ? text.split('/q')[1].split('\n').filter(function(l) { return l.trim(); }) : [];
  if (lines.length < 4) {
    await sendMsg(chatId, '❌ ৪টা info একসাথে দাও:\n1️⃣ Name\n2️⃣ Description\n3️⃣ Timer\n4️⃣ Shuffle (Yes/No)', token);
    return;
  }
  var name = lines[0].trim();
  var desc = lines[1].trim();
  var timer = parseInt(lines[2]) || 15;
  var shuffle = lines[3].toLowerCase().trim() === 'yes';
  try {
    var fileRes = await fetch('https://api.telegram.org/bot' + token + '/getFile?file_id=' + msg.reply_to_message.document.file_id);
    var fileData = await fileRes.json();
    var filePath = fileData.result ? fileData.result.file_path : null;
    if (!filePath) {
      await sendMsg(chatId, '❌ File download failed!', token);
      return;
    }
    var csvRes = await fetch('https://api.telegram.org/file/bot' + token + '/' + filePath);
    var questions = parseCSV(await csvRes.text());
    if (!questions.length) {
      await sendMsg(chatId, '❌ CSV-তে কোনো প্রশ্ন পাওয়া যায়নি!', token);
      return;
    }
    var quizId = 'qz_' + Math.random().toString(36).substring(2, 10);
    var settings = await DB.prepare('SELECT tag, exp_footer FROM quiz_settings WHERE id=1').first();
    var tag = settings ? (settings.tag || '') : '';
    var exp = settings ? (settings.exp_footer || '') : '';
    await DB.prepare('INSERT INTO quizzes (id, name, description, timer, shuffle, csv_data, tag, exp_footer, created_by) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)').bind(quizId, name, desc, timer, shuffle ? 1 : 0, JSON.stringify(questions), tag, exp, msg.from.id).run();
    var link = 'https://t.me/atlasQuizProBot?start=' + quizId;
    await sendMsg(chatId, '✅ Quiz Created Successfully!\n\n📝 Name: ' + name + '\n📄 Description: ' + desc + '\n⏱️ Timer: ' + timer + 's\n🔀 Shuffle: ' + (shuffle ? 'Yes' : 'No') + '\n📊 Questions: ' + questions.length + '\n\n🔗 Quiz Link:\n' + link + '\n\n👆 যে কেউ এই লিংকে ক্লিক করে কুইজ solve করতে পারবে!', token, true);
  } catch (e) {
    await sendMsg(chatId, '❌ ' + e.message, token);
  }
}

// ============================================================
// FEATURE 6: QLIST + QDEL + TAGQ + EXPQ + PRE
// ============================================================
async function handleQlist(chatId, token) {
  var quizzes = await DB.prepare('SELECT id, name FROM quizzes ORDER BY created_at ASC').all();
  if (!quizzes.results || !quizzes.results.length) {
    await sendMsg(chatId, '❌ কোনো quiz নেই!', token);
    return;
  }
  var text = '📋 All Quizzes\n\n';
  quizzes.results.forEach(function(q) {
    text += '📝 ' + q.name + '\n🔗 https://t.me/atlasQuizProBot?start=' + q.id + '\n\n';
  });
  await sendMsg(chatId, text, token, true);
}

async function handleQdel(chatId, qid, token) {
  if (!qid) {
    await sendMsg(chatId, '❌ /qdel qz_xxx', token);
    return;
  }
  await DB.prepare('DELETE FROM quizzes WHERE id=?1').bind(qid).run();
  await DB.prepare('DELETE FROM quiz_results WHERE quiz_id=?1').bind(qid).run();
  await DB.prepare('DELETE FROM quiz_leaderboard WHERE quiz_id=?1').bind(qid).run();
  await sendMsg(chatId, '✅ Quiz deleted: ' + qid, token);
}

async function handleTagQ(msg, token) {
  var tag = (msg.text || '').replace('/tagQ', '').trim();
  if (tag) {
    await DB.prepare('INSERT OR REPLACE INTO quiz_settings (id, tag) VALUES (1, ?1)').bind(tag).run();
    await sendMsg(msg.chat.id, '✅ Tag set: ' + tag + '\n(Future quizzes)', BOT_TOKEN);
  } else {
    var settings = await DB.prepare('SELECT tag FROM quiz_settings WHERE id=1').first();
    await sendMsg(msg.chat.id, '🔖 Current tag: ' + (settings ? (settings.tag || 'None') : 'None') + '\n\nSet: /tagQ [ATLAS 📚]', BOT_TOKEN);
  }
}

async function handleExpQ(msg, token) {
  var exp = (msg.text || '').replace('/expQ', '').trim();
  if (exp) {
    await DB.prepare('INSERT OR REPLACE INTO quiz_settings (id, exp_footer) VALUES (1, ?1)').bind(exp).run();
    await sendMsg(msg.chat.id, '✅ Footer set: ' + exp + '\n(Future quizzes)', BOT_TOKEN);
  } else {
    var settings = await DB.prepare('SELECT exp_footer FROM quiz_settings WHERE id=1').first();
    await sendMsg(msg.chat.id, '📝 Current footer: ' + (settings ? (settings.exp_footer || 'None') : 'None') + '\n\nSet: /expQ [✅ এটলাস]', BOT_TOKEN);
  }
}

async function handlePre(msg, token) {
  var text = (msg.text || '').replace('/pre', '').trim();
  if (text === 'remove') {
    await DB.prepare('DELETE FROM quiz_preview WHERE id=1').run();
    await sendMsg(msg.chat.id, '✅ Preview Image removed!', token);
  } else if (msg.reply_to_message && msg.reply_to_message.photo) {
    var fileId = msg.reply_to_message.photo[msg.reply_to_message.photo.length - 1].file_id;
    await DB.prepare('INSERT OR REPLACE INTO quiz_preview (id, file_id) VALUES (1, ?1)').bind(fileId).run();
    await sendMsg(msg.chat.id, '✅ Quiz Preview Image set!', token);
  } else {
    var preview = await DB.prepare('SELECT file_id FROM quiz_preview WHERE id=1').first();
    if (preview && preview.file_id) {
      await sendMsg(msg.chat.id, '🖼️ Preview is set.\n/pre remove to delete', token);
    } else {
      await sendMsg(msg.chat.id, '❌ No preview!\nReply to image with /pre', token);
    }
  }
}

// ============================================================
// FEATURE 7: PERMIT + INFO + SEND (Owner Only)
// ============================================================
async function handlePermit(msg, token) {
  var args = (msg.text || '').split(/\s+/);
  if (args[1] && /^\d+$/.test(args[1])) {
    await DB.prepare('INSERT OR IGNORE INTO admins (user_id) VALUES (?1)').bind(parseInt(args[1])).run();
    await sendMsg(msg.chat.id, '✅ Admin added: ' + args[1], token);
  } else {
    var admins = await DB.prepare('SELECT user_id FROM admins').all();
    var text = '👑 Admins:\n• ' + OWNER_ID + ' (Owner)\n';
    if (admins.results) {
      admins.results.forEach(function(a) {
        text += '• ' + a.user_id + '\n';
      });
    }
    await sendMsg(msg.chat.id, text, token);
  }
}

async function handleInfo(chatId, token) {
  var users = await DB.prepare('SELECT COUNT(*) as c FROM bot_users').first();
  var quizzes = await DB.prepare('SELECT COUNT(*) as c FROM quizzes').first();
  var attempts = await DB.prepare('SELECT COUNT(*) as c FROM quiz_results').first();
  var top = await DB.prepare('SELECT user_name, COUNT(*) as c FROM quiz_results GROUP BY user_id ORDER BY c DESC LIMIT 3').all();
  var active = await DB.prepare("SELECT q.id, q.name, COUNT(r.id) as c FROM quizzes q LEFT JOIN quiz_results r ON q.id = r.quiz_id GROUP BY q.id ORDER BY c DESC LIMIT 5").all();
  var text = '📊 Bot Statistics\n\n';
  text += '👥 Total Users: ' + (users ? (users.c || 0) : 0) + '\n';
  text += '📝 Total Quizzes Created: ' + (quizzes ? (quizzes.c || 0) : 0) + '\n';
  text += '🎯 Total Quiz Attempts: ' + (attempts ? (attempts.c || 0) : 0) + '\n';
  text += '\n🔝 Top Quiz Solvers:\n';
  var medals = ['🥇', '🥈', '🥉'];
  if (top.results) {
    top.results.forEach(function(r, i) {
      text += (medals[i] || '') + ' ' + r.user_name + ' — ' + r.c + ' quizzes\n';
    });
  }
  text += '\n📌 Active Quizzes:\n';
  if (active.results) {
    active.results.forEach(function(q) {
      text += '🔗 ' + q.id + ' (' + q.name + ') — ' + q.c + ' attempts\n';
    });
  }
  await sendMsg(chatId, text, token, true);
}

async function handleSend(msg, token) {
  if (!msg.reply_to_message) {
    await sendMsg(msg.chat.id, '❌ Reply to a message with /send', token);
    return;
  }
  var usersCount = await DB.prepare('SELECT COUNT(*) as c FROM bot_users').first();
  var chsCount = await DB.prepare('SELECT COUNT(*) as c FROM channels').first();
  var totalUsers = usersCount ? (usersCount.c || 0) : 0;
  var totalChs = chsCount ? (chsCount.c || 0) : 0;
  var combined = totalUsers + totalChs;
  var keyboard = {
    inline_keyboard: [
      [{ text: '👥 All Users (' + totalUsers + ')', callback_data: 'send_users' }],
      [{ text: '📢 All Channels (' + totalChs + ')', callback_data: 'send_chns' }],
      [{ text: '👥+📢 Both (' + combined + ')', callback_data: 'send_both' }]
    ]
  };
  await DB.prepare('INSERT OR REPLACE INTO quiz_sessions (key, data, updated_at) VALUES (?1, ?2, ?3)').bind('send_' + msg.from.id, JSON.stringify({ msgId: msg.reply_to_message.message_id, chatId: msg.chat.id }), Date.now()).run();
  await fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: msg.chat.id,
      text: '📤 Send This Message To:\n\n👥 Total Users: ' + totalUsers + '\n📢 Total Channels: ' + totalChs + '\n👥+📢 Combined: ' + combined,
      reply_markup: keyboard
    })
  });
}

async function handleSendCB(query, token) {
  var data = query.data;
  var uid = query.from.id;
  var chatId = query.message.chat.id;
  var row = await DB.prepare('SELECT data FROM quiz_sessions WHERE key=?1').bind('send_' + uid).first();
  if (!row) {
    return;
  }
  var info = JSON.parse(row.data);
  var msgId = info.msgId;
  var fromChatId = info.chatId;
  var forwardMsg = async function(targetId) {
    await fetch('https://api.telegram.org/bot' + token + '/forwardMessage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: targetId, from_chat_id: fromChatId, message_id: msgId })
    });
  };
  if (data === 'send_users') {
    var users = await DB.prepare('SELECT user_id FROM bot_users').all();
    if (users.results) {
      for (var i = 0; i < users.results.length; i++) {
        await forwardMsg(users.results[i].user_id);
      }
    }
    await sendMsg(chatId, '✅ Sent to all users!', token);
  } else if (data === 'send_chns') {
    var chs = await DB.prepare('SELECT chat_id FROM channels').all();
    if (chs.results) {
      for (var i = 0; i < chs.results.length; i++) {
        await forwardMsg(chs.results[i].chat_id);
      }
    }
    await sendMsg(chatId, '✅ Sent to all channels!', token);
  } else if (data === 'send_both') {
    var users = await DB.prepare('SELECT user_id FROM bot_users').all();
    if (users.results) {
      for (var i = 0; i < users.results.length; i++) {
        await forwardMsg(users.results[i].user_id);
      }
    }
    var chs = await DB.prepare('SELECT chat_id FROM channels').all();
    if (chs.results) {
      for (var i = 0; i < chs.results.length; i++) {
        await forwardMsg(chs.results[i].chat_id);
      }
    }
    await sendMsg(chatId, '✅ Sent to all!', token);
  }
  await DB.prepare('DELETE FROM quiz_sessions WHERE key=?1').bind('send_' + uid).run();
}

// ============================================================
// FEATURE 8: QUIZ PLAY — START
// ============================================================
async function startQuiz(chatId, quizId, user, token, mistakeQuestions, mistakeType) {
  try {
    var quiz, questions;
    if (mistakeQuestions) {
      questions = mistakeQuestions;
      quiz = { timer: 15, tag: '', exp_footer: '', name: 'Practice', description: '' };
      var origRow = await DB.prepare('SELECT data FROM quiz_sessions WHERE key=?1').bind('otag_' + user.id).first();
      if (origRow) {
        var origData = JSON.parse(origRow.data);
        quiz.timer = origData.timer;
        quiz.tag = origData.tag;
        quiz.exp_footer = origData.exp;
        quiz.name = origData.name;
      }
    } else {
      quiz = await DB.prepare('SELECT * FROM quizzes WHERE id=?1').bind(quizId).first();
      if (!quiz) {
        await sendMsg(chatId, '❌ কুইজ পাওয়া যায়নি! লিংক ভুল হতে পারে!', token);
        return;
      }
      questions = JSON.parse(quiz.csv_data);
      if (quiz.shuffle) {
        questions = questions.sort(function() { return Math.random() - 0.5; });
        questions = questions.map(function(x) {
          var correctOption = x.options[x.answer_index];
          x.options = x.options.sort(function() { return Math.random() - 0.5; });
          x.answer_index = x.options.indexOf(correctOption);
          return x;
        });
      }
      await DB.prepare('INSERT OR REPLACE INTO quiz_sessions (key, data, updated_at) VALUES (?1, ?2, ?3)').bind('otag_' + user.id, JSON.stringify({ timer: quiz.timer, tag: quiz.tag, exp: quiz.exp_footer, name: quiz.name }), Date.now()).run();
    }
    var session = {
      quizId: mistakeQuestions ? (quizId + 'mp') : quizId,
      name: cleanText(quiz.name) + (mistakeQuestions ? ' — Practice' : ''),
      desc: cleanText(quiz.description || ''),
      questions: questions,
      cur: 0,
      tot: questions.length,
      right: 0,
      wrong: 0,
      skip: 0,
      timer: quiz.timer || 15,
      tag: cleanText(quiz.tag || ''),
      exp: cleanText(quiz.exp_footer || ''),
      chatId: chatId,
      uname: cleanText(user.first_name || 'Student'),
      uid: user.id,
      pid: null,
      cor: null,
      qResults: [],
      isMistake: !!mistakeQuestions
    };
    await DB.prepare('INSERT OR REPLACE INTO quiz_sessions (key, data, updated_at) VALUES (?1, ?2, ?3)').bind('s_' + user.id, JSON.stringify(session), Date.now()).run();
    if (mistakeQuestions) {
      var introText = '📝 ' + session.name + '\n';
      if (mistakeType === 'wrong') {
        introText += '❌ Wrong Questions: ' + questions.length + '\n';
      } else {
        introText += '❌ Wrong+Skip: ' + questions.length + '\n';
      }
      introText += '🔄 Practice\n\nএখনই কুইজ আসবে, আপনি প্রস্তুত তো? 😎';
      await sendMsg(chatId, introText, token, true);
      var countdownMessages = ['3...', '2...', '1...'];
      for (var i = 0; i < countdownMessages.length; i++) {
        await new Promise(function(resolve) { setTimeout(resolve, 666); });
        await sendMsg(chatId, countdownMessages[i], token, true);
      }
    } else {
      var preview = await DB.prepare('SELECT file_id FROM quiz_preview WHERE id=1').first();
      var infoText = '📝 ' + session.name + '\n📄 ' + session.desc + '\n⏱️ Timer: ' + session.timer + 's\n📊 Questions: ' + session.tot;
      if (preview && preview.file_id) {
        await fetch('https://api.telegram.org/bot' + token + '/sendPhoto', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId, photo: preview.file_id, caption: infoText })
        });
      } else {
        await sendMsg(chatId, infoText, token, true);
      }
      var countdownMessages = ['3...', '2...', '1...'];
      for (var i = 0; i < countdownMessages.length; i++) {
        await new Promise(function(resolve) { setTimeout(resolve, 666); });
        await sendMsg(chatId, countdownMessages[i], token, true);
      }
    }
    await new Promise(function(resolve) { setTimeout(resolve, 1000); });
    await sendQuestion(chatId, session, token);
  } catch (e) {
    await sendMsg(chatId, '❌ ' + e.message, token);
  }
}

// ============================================================
// FEATURE 9: SEND QUESTION + NEXT BUTTON
// ============================================================
async function sendQuestion(chatId, session, token) {
  if (session.cur >= session.tot) {
    return await finishQuiz(chatId, session, token);
  }
  var q = session.questions[session.cur];
  if (!q) {
    return;
  }
  session.qResults.push({ index: session.cur, type: null });
  var tagPart = session.tag ? session.tag + '\n\n' : '';
  var questionText = cleanText(tagPart + (session.cur + 1) + '. ' + (q.question || '?')).slice(0, 300);
  var explanationText = session.exp ? cleanText((q.explanation || '') + '\n' + session.exp).slice(0, 200) : cleanText(q.explanation || '').slice(0, 200);
  var qImg = extractImageUrl(questionText);
  var optImgs = (q.options || []).map(function(o) { return extractImageUrl(o).url; });
  var expImg = extractImageUrl(explanationText);
  var res = await sendPollMsg(chatId, questionText, q.options || [], q.answer_index || 0, session.timer, explanationText, token, qImg.url, optImgs, expImg.url);
  var data = await res.json();
  if (data.ok && data.result) {
    session.pid = data.result.poll ? data.result.poll.id : null;
    session.cor = q.answer_index || 0;
    await DB.prepare('INSERT OR REPLACE INTO quiz_sessions (key, data, updated_at) VALUES (?1, ?2, ?3)').bind('s_' + session.uid, JSON.stringify(session), Date.now()).run();
  if (session.pid) {
    console.log('Queue send for Q:', session.cur);
    await DB.prepare('INSERT OR REPLACE INTO poll_sessions (poll_id, chat_id, next_q_index, session_uid) VALUES (?1, ?2, ?3, ?4)').bind(session.pid, chatId, session.cur + 1, session.uid).run();
    await globalThis.NEXT_QUEUE.send({ chatId: chatId, uid: session.uid, curIndex: session.cur }, { delaySeconds: session.timer + 2 }); 
 } else {
  console.log('No poll ID for Q:', session.cur);
}
}
}

// ============================================================
// FEATURE 10: POLL ANSWER + NEXT HANDLER
// ============================================================
async function handleQuizPoll(pollAnswer) {
  var uid = pollAnswer.user.id;
  var row = await DB.prepare('SELECT data FROM quiz_sessions WHERE key=?1').bind('s_' + uid).first();
  if (!row) {
    return;
  }
  var session = JSON.parse(row.data);
  if (session.pid !== pollAnswer.poll_id) {
    return;
  }
  var optionIds = pollAnswer.option_ids || [];
  var qResult = session.qResults.find(function(x) { return x.index === session.cur; });
  if (qResult) {
    if (!optionIds.length) {
      qResult.type = 'skip';
    } else if (optionIds[0] === session.cor) {
      qResult.type = 'right';
    } else {
      qResult.type = 'wrong';
    }
  }
  if (!optionIds.length) {
    session.skip++;
  } else if (optionIds[0] === session.cor) {
    session.right++;
  } else {
    session.wrong++;
  }
  session.cur++;
  if (session.cur >= session.tot) {
    await finishQuiz(uid, session, QUIZ_BOT_TOKEN);
  } else {
    await sendQuestion(uid, session, QUIZ_BOT_TOKEN);
  }
}

async function handleNext(chatId, uid, token) {
  var row = await DB.prepare('SELECT data FROM quiz_sessions WHERE key=?1').bind('s_' + uid).first();
  if (!row) {
    return;
  }
  var session = JSON.parse(row.data);
  var qResult = session.qResults.find(function(x) { return x.index === session.cur; });
  if (qResult) {
    qResult.type = 'skip';
  }
  session.skip++;
  session.cur++;
  if (session.cur >= session.tot) {
    await finishQuiz(uid, session, QUIZ_BOT_TOKEN);
  } else {
    await sendQuestion(uid, session, QUIZ_BOT_TOKEN);
  }
}

// ============================================================
// FEATURE 11: FINISH + RESULT
// ============================================================
async function finishQuiz(uid, session, token) {
  var tot = session.tot;
  var right = session.right;
  var wrong = session.wrong;
  var skip = session.skip;
  var name = session.name;
  var uname = session.uname;
  var quizId = session.quizId;
  var chatId = session.chatId;
  var score = right + '/' + tot;
  var pct = tot > 0 ? Math.round(right / tot * 100) : 0;
  try {
    var cntRow = await DB.prepare('SELECT COUNT(*) as cnt FROM quiz_results WHERE user_id=?1 AND quiz_id=?2').bind(uid, quizId).first();
    var attempt = (cntRow ? (cntRow.cnt || 0) : 0) + 1;
    await DB.prepare('INSERT INTO quiz_results (user_id, user_name, quiz_id, right_count, wrong_count, skip_count, total, score, attempt) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)').bind(uid, uname, quizId, right, wrong, skip, tot, score, attempt).run();
    var resultIdRow = await DB.prepare('SELECT last_insert_rowid() as id').first();
    var resultId = resultIdRow ? resultIdRow.id : null;
    if (resultId) {
      var qResults = session.qResults || [];
      for (var i = 0; i < qResults.length; i++) {
        var qr = qResults[i];
        if (qr.type) {
          await DB.prepare('INSERT INTO quiz_question_results (result_id, question_index, result_type, quiz_id, user_id) VALUES (?1, ?2, ?3, ?4, ?5)').bind(resultId, qr.index, qr.type, quizId, uid).run();
        }
      }
    }
    var existing = await DB.prepare('SELECT right_count FROM quiz_leaderboard WHERE quiz_id=?1 AND user_id=?2').bind(quizId, uid).first();
    if (existing) {
      if (right > existing.right_count) {
        await DB.prepare('UPDATE quiz_leaderboard SET user_name=?1, score=?2, right_count=?3, total=?4, updated_at=?5 WHERE quiz_id=?6 AND user_id=?7').bind(uname, score, right, tot, Date.now(), quizId, uid).run();
      }
    } else {
      await DB.prepare('INSERT INTO quiz_leaderboard (quiz_id, user_id, user_name, score, right_count, total, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)').bind(quizId, uid, uname, score, right, tot, Date.now()).run();
    }
    await DB.prepare('DELETE FROM quiz_sessions WHERE key=?1').bind('s_' + uid).run();
  } catch (e) {
    console.error('Save Error:', e.message);
  }
  var mot = '';
  if (pct >= 90) {
    mot = '🏆 অসাধারণ! তুমি সেরা! আরও এগিয়ে যাও!';
  } else if (pct >= 70) {
    mot = '🎉 চমৎকার! তুমি খুব ভালো করেছো! আরও প্র্যাকটিস করো!';
  } else if (pct >= 50) {
    mot = '👍 মোটামুটি ভালো! আরও একটু পড়াশোনা করো!';
  } else {
    mot = '📚 পড়া হয়নি! আবার পড়ে চেষ্টা করো!';
  }
  var originalQuizId = session.isMistake ? quizId.replace('mp', '') : quizId;
  var link = 'https://t.me/atlasQuizProBot?start=' + originalQuizId; 
  var txt = '🌟 এটলাসের ' + name + ' কুইজে অংশগ্রহণ করার \nতোমাকে অভিনন্দন প্রিয় শিক্ষার্থী ' + uname + '!\n\n📊 তোমার রেজাল্ট:\n✅ Right: ' + right + '\n❌ Wrong: ' + wrong + '\n😐 Skipped: ' + skip + '\n\n⚡ Final Result: ' + score + ' (' + pct + '%)\n\n' + mot;
  var kb;
  if (session.isMistake) {
    kb = {
      inline_keyboard: [[{ text: '📌 আবার প্রাক্টিস করো', url: link }]]
    };
  } else {
    kb = {
      inline_keyboard: [
        [{ text: '📌 আবার প্রাক্টিস করো', url: link }],
        [{ text: '👥 Leaderboard', callback_data: 'lb_' + quizId }, { text: '📈 History', callback_data: 'hist_' + quizId }],
        [{ text: '🔴 Practice-01 (Wrong only)', callback_data: 'mp1_' + quizId }],
        [{ text: '🟡 Practice-02 (Wrong + Skip)', callback_data: 'mp2_' + quizId }],
        [{ text: '🌐 ATLAS All Courses', url: 'https://WWW.Atlascourses.com' }, { text: '💬 WhatsApp Helpline', url: 'https://wa.me/8801999681290' }],
        [{ text: '📢 All Free Groups+Channels', url: 'https://t.me/MediAtlas/4221' }]
      ]
    };
  }
  await fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text: txt, reply_markup: kb, disable_web_page_preview: true })
  });
}

// ============================================================
// FEATURE 12: LEADERBOARD
// ============================================================
async function handleLB(chatId, quizId, uid, token) {
  var lb = await DB.prepare('SELECT user_name, score, right_count, total, user_id FROM quiz_leaderboard WHERE quiz_id=?1 ORDER BY right_count DESC').bind(quizId).all();
  if (!lb.results || !lb.results.length) {
    await sendMsg(chatId, 'এখনো কেউ quiz solve করেনি!', token, true);
    return;
  }
  var yourPos = -1;
  lb.results.forEach(function(r, i) {
    if (r.user_id === uid) {
      yourPos = i + 1;
    }
  });
  var text = (yourPos > 0 ? '📊 Your Position: #' + yourPos + '\n\n' : '') + '🏆 Leaderboard\n\n';
  lb.results.forEach(function(r, i) {
    var medal = ['🥇', '🥈', '🥉'][i] || (i + 1 + '.');
    var isYou = r.user_id === uid;
    var pct = r.total > 0 ? Math.round(r.right_count / r.total * 100) : 0;
    text += (isYou ? '**' : '') + medal + ' ' + r.user_name + ' — ' + r.score + ' (' + pct + '%)' + (isYou ? ' 👈 You**' : '') + '\n';
  });
  await sendMsg(chatId, text, token, true);
}

// ============================================================
// FEATURE 13: HISTORY
// ============================================================
async function handleHist(chatId, quizId, uid, token) {
  var hist = await DB.prepare('SELECT score, attempt, created_at FROM quiz_results WHERE user_id=?1 AND quiz_id=?2 ORDER BY attempt').bind(uid, quizId).all();
  var text = '📈 Progress\n\n';
  if (hist.results && hist.results.length) {
    hist.results.forEach(function(r) {
      text += '🟢 Attempt ' + r.attempt + ': ' + r.score;
      if (r.created_at) {
        text += ' | 📅 ' + new Date(r.created_at * 1000).toISOString().slice(0, 10);
      }
      text += '\n';
    });
  } else {
    text += 'এখনো কোনো history নেই!';
  }
  await sendMsg(chatId, text, token, true);
}

// ============================================================
// FEATURE 14 & 15: MISTAKE PRACTICE
// ============================================================
async function handleMistake(chatId, quizId, uid, token, type) {
  try {
    var lastResult = await DB.prepare('SELECT id FROM quiz_results WHERE user_id=?1 AND quiz_id=?2 ORDER BY id DESC LIMIT 1').bind(uid, quizId).first();
    if (!lastResult) {
      await sendMsg(chatId, 'No previous attempt found!', token);
      return;
    }
    var types = type === 'wrong' ? ['wrong'] : ['wrong', 'skip'];
    var wrongQs;
    if (types.length === 1) {
    wrongQs = await DB.prepare('SELECT question_index FROM quiz_question_results WHERE result_id=?1 AND result_type=?2').bind(lastResult.id, types[0]).all();
    } else {
    wrongQs = await DB.prepare('SELECT question_index FROM quiz_question_results WHERE result_id=?1 AND result_type IN (?2, ?3)').bind(lastResult.id, types[0], types[1]).all();
    }
    if (!wrongQs.results || !wrongQs.results.length) {    
      await sendMsg(chatId, type === 'wrong' ? '🎉 সব সঠিক ছিল! Practice-এর প্রয়োজন নেই!' : '🎉 সব সঠিক ছিল, skip-ও নেই!', token);
      return;
    }
    var quiz = await DB.prepare('SELECT * FROM quizzes WHERE id=?1').bind(quizId).first();
    if (!quiz) {
      await sendMsg(chatId, 'Quiz not found!', token);
      return;
    }
    var allQuestions = JSON.parse(quiz.csv_data);
    var practiceQuestions = wrongQs.results.map(function(r) { return allQuestions[r.question_index]; }).filter(function(q) { return q; });
    if (!practiceQuestions.length) {
      await sendMsg(chatId, 'Questions not found!', token);
      return;
    }
    await startQuiz(chatId, quizId, { id: uid, first_name: 'Student' }, token, practiceQuestions, type);
  } catch (e) {
    console.error('Mistake Practice:', e.message);
    await sendMsg(chatId, '❌ ' + e.message, token);
  }
}

// ============================================================
// END — ATLAS QUIZ BOT v6.0 COMPLETE
// ============================================================;
