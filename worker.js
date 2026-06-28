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

    // Web Quiz — same index.html style, CF থেকেই চলে, HF লাগে না
    if (url.pathname.startsWith('/quiz/')) return await handleWebQuiz(request, url, env);
    if (url.pathname.startsWith('/api/exam/')) return await handleQuizData(request, url);
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
    const HF_URL = env.HF_SPACE_URL || 'https://hamzahf1-atlasboss.hf.space';
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
// WEB QUIZ — Same index.html style, runs entirely on CF
// ============================================================
async function handleQuizData(request, url) {
  try {
    let id = url.searchParams.get('id');
    if (!id) id = url.pathname.replace('/api/exam/', '');
    if (!id) return jsonResp({ ok: false, error: 'No id' }, 400);
    if (!id.startsWith('qz_')) return jsonResp({ ok: false, error: 'Invalid quiz id' }, 400);

    const ANS = ["A","B","C","D","E"];

    // ── Layer 1: D1 ──
    let row = null;
    try {
      row = await DB.prepare("SELECT * FROM quizzes WHERE id=?1").bind(id).first();
    } catch(e) {
      console.error('[quiz] D1 failed:', e.message);
    }

    // ── Layer 2: Supabase backup (D1 fail বা empty হলে) ──
    if (!row) {
      try {
        const SB_URL = typeof SUPABASE_URL !== 'undefined' ? SUPABASE_URL : '';
        const SB_KEY = typeof SUPABASE_KEY !== 'undefined' ? SUPABASE_KEY : '';
        if (SB_URL && SB_KEY) {
          const r = await fetch(
            `${SB_URL}/rest/v1/quiz_backups?quiz_id=eq.${id}&select=*`,
            { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } }
          );
          const data = await r.json();
          if (data && data[0]) {
            const b = data[0];
            // D1 তে re-import করো (future এর জন্য)
            try {
              await DB.prepare(
                "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)"
              ).bind(id, b.name, '', 30, 0, JSON.stringify(b.questions), '', '', 0).run();
            } catch(_) {}
            // Supabase data দিয়ে serve করো
            const mcqs = b.questions.map(q => ({
              question: q.question || '',
              options: q.options || [],
              answer: ANS[q.answer_index ?? 0] || 'A',
              explanation: q.explanation || '',
            }));
            return jsonResp({
              cache_id: id, topic: b.name || 'Quiz', page: 1,
              mcqs, tag: '', exp_footer: '', channel_id: '',
              image_msg_id: null, end_msg_id: null,
              image_file_id: null, is_new_gen: false, timer: 30,
              _source: 'supabase_backup',
            });
          }
        }
      } catch(e) {
        console.error('[quiz] Supabase fallback failed:', e.message);
      }
    }

    if (!row) return jsonResp({ error: 'Not found' }, 404);

    const questions = JSON.parse(row.csv_data || '[]');
    const mcqs = questions.map(q => ({
      question: q.question || '',
      options: q.options || [],
      answer: ANS[q.answer_index ?? 0] || 'A',
      explanation: q.explanation || '',
    }));

    return jsonResp({
      cache_id: id, topic: row.name || 'Quiz', page: 1,
      mcqs, tag: row.tag || '', exp_footer: row.exp_footer || '',
      channel_id: '', image_msg_id: null, end_msg_id: null,
      image_file_id: null, is_new_gen: false, timer: row.timer || 30,
    });
  } catch(e) {
    return jsonResp({ error: e.message }, 500);
  }
}

async function handleWebQuiz(request, url, env) {
  const quizId = url.pathname.replace('/quiz/', '').split('?')[0];
  if (!quizId) return new Response('Quiz ID missing', { status: 400 });

  const uid  = url.searchParams.get('uid') || '0';
  const name = url.searchParams.get('name') || 'Student';

  // index.html GitHub raw থেকে fetch করো
  const WORKER_ORIGIN = `https://atlasquizbotpro.hamza818483.workers.dev`;
  try {
    const r = await fetch(
      'https://raw.githubusercontent.com/hamza818483-dotcom/QuizBot/main/index.html',
      { cf: { cacheEverything: true, cacheTtl: 300 } }
    );
    if (!r.ok) throw new Error('index.html fetch failed');

    let html = await r.text();

    // Template variables replace — HF_SPACE_URL কে worker নিজেই handle করবে
    html = html.replace(/\{\{CACHE_ID\}\}/g,      quizId);
    html = html.replace(/\{\{USER_ID\}\}/g,       uid);
    html = html.replace(/\{\{USER_NAME\}\}/g,     encodeURIComponent(name));
    html = html.replace(/\{\{HF_SPACE_URL\}\}/g,  WORKER_ORIGIN);
    html = html.replace(/\{\{SUPABASE_URL\}\}/g,  '');
    html = html.replace(/\{\{SUPABASE_KEY\}\}/g,  '');

    return new Response(html, {
      status: 200,
      headers: { 'Content-Type': 'text/html;charset=UTF-8' }
    });
  } catch(e) {
    return new Response(`<h2 style="font-family:sans-serif;color:#EF4444;padding:24px">❌ Quiz load failed: ${e.message}</h2>`, {
      status: 500,
      headers: { 'Content-Type': 'text/html' }
    });
  }
}
async function forwardToHF(request, env) {
  try {
    const hfUrl = (env.HF_SPACE_URL || 'https://hamzahf1-atlasboss.hf.space') + '/webhook';
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
