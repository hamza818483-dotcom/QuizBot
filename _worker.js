// ============================================================
// ATLAS BOT — Cloudflare Worker (Proxy + D1)
// Minimal proxy: TG API, file download, photo/doc upload, D1 SQL
// All quiz logic lives in Python (app.py on HF Space)
// ============================================================

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    globalThis.DB = env.DB;
    globalThis.ATLAS_BOT_TOKEN = env.ATLAS_BOT_TOKEN || env.QUIZ_BOT_TOKEN;
    globalThis.D1_TOKEN = env.D1_TOKEN || '';

    // D1 key-value store
    if (url.pathname === '/d1/set' && request.method === 'POST') return await d1Set(request);
    if (url.pathname === '/d1/get' && request.method === 'GET') return await d1Get(request);
    if (url.pathname === '/d1/del' && request.method === 'POST') return await d1Del(request);

    // D1 raw SQL query
    if (url.pathname === '/d1/query' && request.method === 'POST') return await d1Query(request);

    // D1 init tables
    if (url.pathname === '/init-db') return await initDB();

    // TG API proxy
    if (url.pathname.startsWith('/tg-proxy/')) return await handleTgProxy(request, url);

    // TG file download proxy
    if (url.pathname === '/tg-file') return await handleTgFileProxy(request, url);

    // TG send photo (multipart)
    if (url.pathname === '/tg-sendphoto') return await handleTgSendPhoto(request);

    // TG send document (multipart)
    if (url.pathname === '/tg-senddoc') return await handleTgSendDoc(request);

    // Web Quiz — bot ছাড়াই চলে, D1 থেকে directly serve
    if (url.pathname.startsWith('/quiz/')) return await handleWebQuiz(request, url, env);
    if (url.pathname === '/quiz-data' && request.method === 'GET') return await handleQuizData(request, url);

    // Webhook → forward everything to HF Space
    if (url.pathname === '/webhook' || url.pathname.startsWith('/webhook/')) {
      return await forwardToHF(request, env);
    }

    // Health check
    if (url.pathname === '/health') return jsonResp({ ok: true, status: 'alive' });

    return jsonResp({ ok: true, service: 'ATLAS Bot Proxy', version: '2.0' });
  },

  // ── Cron: প্রতি 5 মিনিটে HF Space ping করে alive রাখে ──
  async scheduled(event, env) {
    const HF_URL = env.HF_SPACE_URL || 'https://hamzahf2-atlasboss.hf.space';
    try {
      const r = await fetch(HF_URL + '/health', {
        method: 'GET',
        signal: AbortSignal.timeout(10000),
      });
      console.log(`[cron] HF ping: ${r.status}`);
    } catch(e) {
      console.error(`[cron] HF ping failed: ${e.message}`);
    }
  }
};

function jsonResp(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' }
  });
}

// ============================================================
// D1 INIT TABLES
// ============================================================
async function initDB() {
  try {
    const tables = [
      "CREATE TABLE IF NOT EXISTS quizzes (id TEXT PRIMARY KEY, name TEXT, description TEXT, timer INTEGER DEFAULT 15, shuffle BOOLEAN DEFAULT 0, csv_data TEXT, tag TEXT DEFAULT '', exp_footer TEXT DEFAULT '', created_by INTEGER, created_at INTEGER DEFAULT (unixepoch()))",
      "CREATE TABLE IF NOT EXISTS quiz_results (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, user_name TEXT, quiz_id TEXT, right_count INTEGER DEFAULT 0, wrong_count INTEGER DEFAULT 0, skip_count INTEGER DEFAULT 0, total INTEGER, score TEXT, attempt INTEGER DEFAULT 1, created_at INTEGER DEFAULT (unixepoch()))",
      "CREATE TABLE IF NOT EXISTS quiz_leaderboard (quiz_id TEXT, user_id INTEGER, user_name TEXT, score TEXT, right_count INTEGER, total INTEGER, updated_at INTEGER, PRIMARY KEY (quiz_id, user_id))",
      "CREATE TABLE IF NOT EXISTS quiz_settings (id INTEGER PRIMARY KEY DEFAULT 1, tag TEXT DEFAULT '', exp_footer TEXT DEFAULT '')",
      "CREATE TABLE IF NOT EXISTS quiz_sessions (key TEXT PRIMARY KEY, data TEXT, updated_at INTEGER)",
      "CREATE TABLE IF NOT EXISTS quiz_preview (id INTEGER PRIMARY KEY DEFAULT 1, file_id TEXT)",
      "CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)",
      "CREATE TABLE IF NOT EXISTS bot_users (user_id INTEGER PRIMARY KEY, user_name TEXT, first_seen INTEGER, last_seen INTEGER)",
      "CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, title TEXT)",
      "CREATE TABLE IF NOT EXISTS quiz_question_results (id INTEGER PRIMARY KEY AUTOINCREMENT, result_id INTEGER, question_index INTEGER, result_type TEXT, quiz_id TEXT, user_id INTEGER, created_at INTEGER DEFAULT (unixepoch()))",
      "CREATE TABLE IF NOT EXISTS poll_sessions (poll_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, next_q_index INTEGER NOT NULL, session_uid INTEGER NOT NULL, created_at INTEGER DEFAULT (unixepoch()))",
      "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at INTEGER NOT NULL)",
      "CREATE TABLE IF NOT EXISTS poll_collection (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, poll_data TEXT, created_at INTEGER DEFAULT (unixepoch()))"
    ];
    for (const sql of tables) {
      await DB.exec(sql);
    }
    return jsonResp({ ok: true, message: 'All tables created!' });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// D1 RAW SQL QUERY (authenticated)
// ============================================================
async function d1Query(request) {
  try {
    const body = await request.json();
    const token = body.token || request.headers.get('X-D1-Token') || '';
    if (globalThis.D1_TOKEN && token !== globalThis.D1_TOKEN) {
      return jsonResp({ ok: false, error: 'Unauthorized' }, 401);
    }
    const sql = body.sql;
    const params = body.params || [];
    if (!sql) {
      return jsonResp({ ok: false, error: 'No SQL provided' }, 400);
    }
    let stmt = DB.prepare(sql);
    if (params.length > 0) {
      stmt = stmt.bind(...params);
    }
    const isSelect = sql.trim().toUpperCase().startsWith('SELECT');
    if (isSelect) {
      const result = await stmt.all();
      return jsonResp({ ok: true, results: result.results || [], meta: result.meta });
    } else {
      const result = await stmt.run();
      return jsonResp({ ok: true, meta: result.meta, changes: result.meta?.changes });
    }
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// D1 KEY-VALUE STORE
// ============================================================
async function d1Set(request) {
  try {
    const body = await request.json();
    const key = body.key;
    const value = JSON.stringify(body.value);
    const ttl = body.ttl || 86400;
    await DB.prepare(
      "INSERT OR REPLACE INTO kv_store (key, value, expires_at) VALUES (?1, ?2, ?3)"
    ).bind(key, value, Math.floor(Date.now() / 1000) + ttl).run();
    return jsonResp({ ok: true });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

async function d1Get(request) {
  try {
    const url = new URL(request.url);
    const key = url.searchParams.get('key');
    const row = await DB.prepare(
      "SELECT value, expires_at FROM kv_store WHERE key = ?1"
    ).bind(key).first();
    if (!row) return jsonResp({ ok: true, value: null });
    if (row.expires_at < Math.floor(Date.now() / 1000)) {
      await DB.prepare("DELETE FROM kv_store WHERE key = ?1").bind(key).run();
      return jsonResp({ ok: true, value: null });
    }
    return jsonResp({ ok: true, value: JSON.parse(row.value) });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

async function d1Del(request) {
  try {
    const body = await request.json();
    await DB.prepare("DELETE FROM kv_store WHERE key = ?1").bind(body.key).run();
    return jsonResp({ ok: true });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// TG API PROXY
// ============================================================
async function handleTgProxy(request, url) {
  try {
    const method = url.pathname.replace('/tg-proxy/', '');
    if (!method) return jsonResp({ ok: false, error: 'No method' });
    const token = globalThis.ATLAS_BOT_TOKEN;
    const body = await request.text();
    const contentType = request.headers.get('content-type') || 'application/json';
    const resp = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: 'POST',
      headers: { 'Content-Type': contentType },
      body: body
    });
    const result = await resp.text();
    return new Response(result, {
      status: resp.status,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// TG FILE DOWNLOAD PROXY
// ============================================================
async function handleTgFileProxy(request, url) {
  try {
    const filePath = url.searchParams.get('path');
    if (!filePath) return jsonResp({ ok: false, error: 'No path' }, 400);
    const token = globalThis.ATLAS_BOT_TOKEN;
    const resp = await fetch(`https://api.telegram.org/file/bot${token}/${filePath}`);
    if (!resp.ok) {
      return jsonResp({ ok: false, error: 'TG file fetch failed: ' + resp.status }, resp.status);
    }
    const contentType = resp.headers.get('content-type') || 'application/octet-stream';
    const fileBytes = await resp.arrayBuffer();
    return new Response(fileBytes, {
      status: 200,
      headers: {
        'Content-Type': contentType,
        'Content-Length': fileBytes.byteLength.toString(),
        'Cache-Control': 'no-cache'
      }
    });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// TG SEND PHOTO PROXY (multipart)
// ============================================================
async function handleTgSendPhoto(request) {
  try {
    const body = await request.json();
    const token = globalThis.ATLAS_BOT_TOKEN;
    const photoB64 = body.photo_b64;
    if (!photoB64) return jsonResp({ ok: false, error: 'No photo_b64' }, 400);

    const binaryStr = atob(photoB64);
    const bytes = new Uint8Array(binaryStr.length);
    for (let i = 0; i < binaryStr.length; i++) {
      bytes[i] = binaryStr.charCodeAt(i);
    }

    const formData = new FormData();
    formData.append('chat_id', String(body.chat_id));
    formData.append('caption', body.caption || '');
    formData.append('parse_mode', 'HTML');
    formData.append('photo', new Blob([bytes], { type: 'image/jpeg' }), 'page.jpg');
    if (body.reply_markup) formData.append('reply_markup', JSON.stringify(body.reply_markup));
    if (body.reply_to_message_id) formData.append('reply_to_message_id', String(body.reply_to_message_id));
    if (body.message_thread_id) formData.append('message_thread_id', String(body.message_thread_id));

    const resp = await fetch(`https://api.telegram.org/bot${token}/sendPhoto`, {
      method: 'POST', body: formData
    });
    const result = await resp.json();
    return new Response(JSON.stringify(result), {
      status: resp.status, headers: { 'Content-Type': 'application/json' }
    });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// TG SEND DOCUMENT PROXY (multipart)
// ============================================================
async function handleTgSendDoc(request) {
  try {
    const body = await request.json();
    const token = globalThis.ATLAS_BOT_TOKEN;
    const docB64 = body.doc_b64;
    if (!docB64) return jsonResp({ ok: false, error: 'No doc_b64' }, 400);

    const binaryStr = atob(docB64);
    const bytes = new Uint8Array(binaryStr.length);
    for (let i = 0; i < binaryStr.length; i++) {
      bytes[i] = binaryStr.charCodeAt(i);
    }

    const formData = new FormData();
    formData.append('chat_id', String(body.chat_id));
    formData.append('caption', body.caption || '');
    formData.append('document', new Blob([bytes], { type: body.mime_type || 'application/octet-stream' }), body.filename || 'file');

    const resp = await fetch(`https://api.telegram.org/bot${token}/sendDocument`, {
      method: 'POST', body: formData
    });
    const result = await resp.json();
    return new Response(JSON.stringify(result), {
      status: resp.status, headers: { 'Content-Type': 'application/json' }
    });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// WEB QUIZ — Bot ছাড়াই D1 থেকে quiz serve
// URL: /quiz/qz_XXXXX
// ============================================================
async function handleQuizData(request, url) {
  try {
    const id = url.searchParams.get('id');
    if (!id) return jsonResp({ ok: false, error: 'No id' }, 400);
    const row = await DB.prepare("SELECT * FROM quizzes WHERE id=?1").bind(id).first();
    if (!row) return jsonResp({ ok: false, error: 'Not found' }, 404);
    const questions = JSON.parse(row.csv_data || '[]');
    return jsonResp({ ok: true, name: row.name, timer: row.timer || 30, questions });
  } catch(e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

async function handleWebQuiz(request, url, env) {
  const quizId = url.pathname.replace('/quiz/', '').split('?')[0];
  if (!quizId) return new Response('Quiz ID missing', { status: 400 });

  const html = `<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ATLAS Quiz</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',sans-serif;background:#0f0f13;color:#e2e8f0;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:16px}
  .card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px;width:100%;max-width:520px;margin-top:16px}
  h1{font-size:18px;font-weight:800;color:#818CF8;text-align:center;margin-bottom:4px}
  .sub{font-size:11px;color:#64748b;text-align:center;margin-bottom:16px}
  .progress{height:4px;background:rgba(255,255,255,.08);border-radius:2px;margin-bottom:16px}
  .progress-bar{height:100%;background:#818CF8;border-radius:2px;transition:width .3s}
  .qnum{font-size:10px;color:#64748b;margin-bottom:8px}
  .question{font-size:14px;font-weight:700;line-height:1.6;margin-bottom:16px;color:#f1f5f9}
  .options{display:flex;flex-direction:column;gap:8px}
  .opt{padding:11px 14px;border-radius:10px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.03);cursor:pointer;font-size:13px;transition:all .15s;text-align:left;color:#e2e8f0}
  .opt:hover{border-color:#818CF8;background:rgba(129,140,248,.08)}
  .opt.correct{border-color:#10B981;background:rgba(16,185,129,.12);color:#10B981;font-weight:700}
  .opt.wrong{border-color:#EF4444;background:rgba(239,68,68,.1);color:#EF4444}
  .opt.reveal{border-color:#10B981;background:rgba(16,185,129,.08);color:#10B981}
  .opt:disabled{cursor:default}
  .explanation{margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.2);font-size:12px;color:#a5b4fc;line-height:1.6;display:none}
  .timer{text-align:center;font-size:22px;font-weight:900;font-family:monospace;color:#FBBF24;margin-bottom:12px}
  .timer.urgent{color:#EF4444;animation:blink .5s ease-in-out infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
  .next-btn{width:100%;margin-top:14px;padding:12px;border-radius:10px;background:#818CF8;color:#fff;border:none;font-size:14px;font-weight:800;cursor:pointer;display:none}
  .next-btn:hover{background:#6366F1}
  .result-card{text-align:center;padding:24px}
  .result-score{font-size:48px;font-weight:900;color:#818CF8;margin:12px 0}
  .result-detail{font-size:13px;color:#64748b;line-height:2}
  .restart-btn{margin-top:16px;padding:10px 24px;border-radius:10px;background:#10B981;color:#fff;border:none;font-size:13px;font-weight:800;cursor:pointer}
  .loading{text-align:center;padding:40px;color:#64748b}
  .error{text-align:center;padding:40px;color:#EF4444}
</style>
</head>
<body>
<div class="card" id="app">
  <div class="loading">⏳ লোড হচ্ছে...</div>
</div>
<script>
const QUIZ_ID = '${quizId}';
const BASE = location.origin;
let questions=[], current=0, score=0, wrong=0, skipped=0, timer=30, timerEl, interval;

async function init(){
  try{
    const r = await fetch(BASE+'/quiz-data?id='+QUIZ_ID);
    const d = await r.json();
    if(!d.ok){ document.getElementById('app').innerHTML='<div class="error">❌ Quiz পাওয়া যায়নি।</div>'; return; }
    questions = d.questions;
    timer = d.timer || 30;
    document.title = d.name + ' — ATLAS Quiz';
    document.getElementById('app').innerHTML = '<h1>'+escHtml(d.name)+'</h1><div class="sub">ATLAS Quiz · '+questions.length+' প্রশ্ন</div><div class="progress"><div class="progress-bar" id="pbar" style="width:0%"></div></div><div id="qarea"></div>';
    showQ();
  }catch(e){ document.getElementById('app').innerHTML='<div class="error">❌ Error: '+e.message+'</div>'; }
}

function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function showQ(){
  if(current>=questions.length){ showResult(); return; }
  const q = questions[current];
  const pct = (current/questions.length*100).toFixed(0);
  document.getElementById('pbar').style.width=pct+'%';
  const opts = q.options.map((o,i)=>
    '<button class="opt" id="opt'+i+'" onclick="pick('+i+')" data-i="'+i+'">'+escHtml(o)+'</button>'
  ).join('');
  document.getElementById('qarea').innerHTML =
    '<div class="timer" id="timer">'+timer+'</div>'+
    '<div class="qnum">প্রশ্ন '+(current+1)+' / '+questions.length+'</div>'+
    '<div class="question">'+escHtml(q.question)+'</div>'+
    '<div class="options">'+opts+'</div>'+
    '<div class="explanation" id="exp">'+escHtml(q.explanation||'')+'</div>'+
    '<button class="next-btn" id="nxt" onclick="next()">পরের প্রশ্ন →</button>';
  startTimer(q.answer_index);
}

function startTimer(ans){
  let t=timer;
  timerEl=document.getElementById('timer');
  interval=setInterval(()=>{
    t--;
    if(timerEl) timerEl.textContent=t;
    if(t<=5 && timerEl) timerEl.classList.add('urgent');
    if(t<=0){ clearInterval(interval); timeUp(ans); }
  },1000);
}

function timeUp(ans){
  skipped++;
  lockOpts(ans, -1);
}

function pick(i){
  clearInterval(interval);
  const q=questions[current];
  const ans=q.answer_index;
  if(i===ans) score++; else wrong++;
  lockOpts(ans, i);
}

function lockOpts(ans, chosen){
  document.querySelectorAll('.opt').forEach(b=>{
    b.onclick=null;
    const i=parseInt(b.dataset.i);
    if(i===ans) b.classList.add(chosen===-1?'reveal':'correct');
    else if(i===chosen) b.classList.add('wrong');
  });
  const exp=document.getElementById('exp');
  if(exp && exp.textContent.trim()) exp.style.display='block';
  const nxt=document.getElementById('nxt');
  if(nxt) nxt.style.display='block';
}

function next(){ current++; showQ(); }

function showResult(){
  const pct=Math.round(score/questions.length*100);
  document.getElementById('app').innerHTML =
    '<div class="result-card">'+
    '<h1>✅ Quiz সম্পন্ন!</h1>'+
    '<div class="result-score">'+pct+'%</div>'+
    '<div class="result-detail">'+
    '✅ সঠিক: <b>'+score+'</b><br>'+
    '❌ ভুল: <b>'+wrong+'</b><br>'+
    '⏭ Skip: <b>'+skipped+'</b><br>'+
    '📋 মোট: <b>'+questions.length+'</b>'+
    '</div>'+
    '<button class="restart-btn" onclick="restart()">🔄 আবার দাও</button>'+
    '</div>';
}

function restart(){ current=0; score=0; wrong=0; skipped=0; showQ(); }

init();
</script>
</body>
</html>`;

  return new Response(html, {
    status: 200,
    headers: { 'Content-Type': 'text/html;charset=UTF-8' }
  });
}
async function forwardToHF(request, env) {
  try {
    const hfUrl = (env.HF_SPACE_URL || 'https://hamzahf2-atlasboss.hf.space') + '/webhook';
    const body = await request.text();
    await fetch(hfUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body
    });
  } catch (e) {
    console.error('[HF Forward] Error:', e.message);
  }
  return new Response('OK');
}
