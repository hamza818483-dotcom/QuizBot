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

    // Webhook → forward everything to HF Space
    if (url.pathname === '/webhook' || url.pathname.startsWith('/webhook/')) {
      return await forwardToHF(request, env);
    }

    return jsonResp({ ok: true, service: 'ATLAS Bot Proxy', version: '2.0' });
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
// WEBHOOK → FORWARD TO HF SPACE
// ============================================================
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
