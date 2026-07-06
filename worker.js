// ============================================================
// ATLAS BOT — Cloudflare Worker (Proxy + D1)
// Minimal proxy: TG API, file download, photo/doc upload, D1 SQL
// All quiz logic lives in Python (app.py on HF Space)
// ============================================================

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
      });
    }
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

    // Web Quiz — CF serves index.html, API data via HF→D1→Supabase chain
    if (url.pathname.startsWith('/quiz/')) return await handleWebQuiz(request, url, env);
    if (url.pathname.startsWith('/exam/')) return await handleWebQuiz(request, url, env);
    if (url.pathname.startsWith('/api/exam/')) return await handleQuizData(request, url, env);
    if (url.pathname === '/quiz-data' && request.method === 'GET') return await handleQuizData(request, url, env);

    // v4.2: HF account permanently banned — these routes now go to Render.
    const HF_ONLY = ['/api/exam/result', '/api/new-exam', '/api/bookmark',
                     '/api/leaderboard', '/api/solve-pdf', '/api/tg-image', '/api/new-exam/status'];
    if (HF_ONLY.some(p => url.pathname.startsWith(p))) {
      const RENDER = env.RENDER_URL || 'https://quizbot-s482.onrender.com';
      const renderReq = new Request(RENDER + url.pathname + url.search, {
        method: request.method,
        headers: request.headers,
        body: request.method !== 'GET' ? request.body : undefined,
      });
      try {
        return await fetch(renderReq, { signal: AbortSignal.timeout(20000) });
      } catch(e) {
        return jsonResp({ ok: false, error: 'Render unavailable: ' + e.message }, 502);
      }
    }

    // Webhook → ack Telegram INSTANTLY, forward to Render in background.
    // v4.4: previously this awaited Render synchronously, so a cold Render
    // start (30-50s) meant Telegram itself waited that long for every
    // command — the "1 min delay" bug. Telegram only needs a 200 quickly;
    // it doesn't care when the actual processing happens.
    if (url.pathname === '/webhook' || url.pathname.startsWith('/webhook/')) {
      const bodyText = await request.text();
      const forwardReq = new Request(request.url, {
        method: request.method,
        headers: request.headers,
        body: bodyText,
      });
      ctx.waitUntil(forwardToHF(forwardReq, env));
      return new Response('OK');
    }

    // Health check
    if (url.pathname === '/health') return jsonResp({ ok: true, status: 'alive' });

    return jsonResp({ ok: true, service: 'ATLAS Bot Proxy', version: '2.0' });
  },

  // ── Cron: Render keep-alive + Primary→Secondary failover + daily Supabase→D1 sync ──
  async scheduled(event, env) {
    const RENDER_URL   = env.RENDER_URL   || 'https://quizbot-s482.onrender.com';
    const RENDER_URL_2 = env.RENDER_URL_2 || '';
    const BOT_TOKEN     = env.ATLAS_BOT_TOKEN || env.QUIZ_BOT_TOKEN || '';
    const OWNER_ID      = env.OWNER_ID || '';

    // Render ping — 15min sleep না করতে (HF permanently banned, removed)
    let primaryStatus = 0;
    try {
      const r2 = await fetch(RENDER_URL + '/health', { signal: AbortSignal.timeout(10000) });
      primaryStatus = r2.status;
      console.log(`[cron] Render ping: ${r2.status}`);
    } catch(e) {
      console.warn(`[cron] Render ping failed: ${e.message}`);
    }

    // ── SaveContentAtlas cross-ping — 15min sleep prevent (internal, no external login needed) ──
    try {
      const scaR = await fetch('https://savecontentatlas.onrender.com', { signal: AbortSignal.timeout(10000) });
      console.log(`[cron] SaveContentAtlas ping: ${scaR.status}`);
    } catch(e) {
      console.warn(`[cron] SaveContentAtlas ping failed: ${e.message}`);
    }

    // ── Confirmed Primary→Secondary failover (independent 2nd system, mirrors GitHub Actions watchdog-1) ──
    if (RENDER_URL_2 && BOT_TOKEN) {
      try {
        // Confirm down: retry twice more (3 total) before acting — avoid false trigger on blip
        let downCount = primaryStatus === 200 ? 0 : 1;
        for (let i = 0; i < 2 && downCount > 0; i++) {
          await new Promise(r => setTimeout(r, 4000));
          try {
            const retry = await fetch(RENDER_URL + '/health', { signal: AbortSignal.timeout(10000) });
            if (retry.status === 200) { downCount = 0; break; }
            downCount++;
          } catch (_) { downCount++; }
        }
        const primaryConfirmedDown = downCount >= 2; // at least 2 of 3 checks failed

        const webhookInfoRes = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo`);
        const webhookInfo = await webhookInfoRes.json();
        const currentUrl = webhookInfo?.result?.url || '';
        const primaryHost = RENDER_URL.replace(/^https?:\/\//, '');
        const secondaryHost = RENDER_URL_2.replace(/^https?:\/\//, '');

        if (primaryConfirmedDown && currentUrl.includes(primaryHost)) {
          // Verify Secondary healthy before switching
          const secCheck = await fetch(RENDER_URL_2 + '/health', { signal: AbortSignal.timeout(10000) }).catch(() => null);
          if (secCheck && secCheck.status === 200) {
            await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/setWebhook`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body: `url=${encodeURIComponent(RENDER_URL_2 + '/webhook')}&drop_pending_updates=false`
            });
            await new Promise(r => setTimeout(r, 2000));
            const confirmRes = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo`);
            const confirmInfo = await confirmRes.json();
            const confirmedSwitched = (confirmInfo?.result?.url || '').includes(secondaryHost);
            console.log(`[cron][CF-failover] Primary down confirmed, switch to Secondary: ${confirmedSwitched}`);
            if (OWNER_ID) {
              const msg = confirmedSwitched
                ? `🔄 CF WORKER FAILOVER (independent check)!%0A%0APrimary Render DOWN confirmed by Cloudflare Worker.%0ASecondary te switch hoyeche + verify kora hoyeche.%0A%0APrimary: ${RENDER_URL}%0ASecondary: ${RENDER_URL_2}`
                : `🚨 CF WORKER FAILOVER ATTEMPT FAILED!%0A%0APrimary DOWN but webhook switch confirm hoyni (CF check).%0ACurrent: ${confirmInfo?.result?.url || 'unknown'}`;
              await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: `chat_id=${OWNER_ID}&text=${msg}`
              }).catch(() => {});
            }
          } else if (OWNER_ID) {
            const msg = `🚨 CRITICAL (CF check): BOTH Render DOWN!%0A%0APrimary: ${RENDER_URL}%0ASecondary: ${RENDER_URL_2}%0A%0AManual check needed NOW.`;
            await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body: `chat_id=${OWNER_ID}&text=${msg}`
            }).catch(() => {});
          }
        } else if (!primaryConfirmedDown && currentUrl.includes(secondaryHost)) {
          // Primary recovered — switch back
          await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/setWebhook`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: `url=${encodeURIComponent(RENDER_URL + '/webhook')}&drop_pending_updates=false`
          });
          console.log('[cron][CF-failover] Primary recovered, switched back');
          if (OWNER_ID) {
            const msg = `✅ CF WORKER: PRIMARY RECOVERED!%0A%0AWebhook Primary te switch back hoyeche (Cloudflare independent check).`;
            await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body: `chat_id=${OWNER_ID}&text=${msg}`
            }).catch(() => {});
          }
        }
      } catch (e) {
        console.error(`[cron][CF-failover] error: ${e.message}`);
      }
    }

    const SB_URL = 'https://wbdyjpjbczfunyhhmtry.supabase.co';
    const SB_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndiZHlqcGpiY3pmdW55aGhtdHJ5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2OTI5ODAsImV4cCI6MjA5NjI2ODk4MH0.0WR1sgVsl_1XWZfSd0Pwoe6Uxp-2GMTksfseMn5aWjg';

    // প্রতিদিন রাত 12টায় Supabase→D1 sync (cron: 0 0 * * *)
    // সব quiz_backups D1 তে আছে কিনা check করে, না থাকলে restore করে
    const now = new Date();
    if (now.getUTCHours() === 0 && now.getUTCMinutes() < 5) {
      try {
        const r = await fetch(
          `${SB_URL}/rest/v1/quiz_backups?select=quiz_id,name,questions,created_by`,
          { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } }
        );
        const backups = await r.json();
        let synced = 0;
        for (const b of (backups || [])) {
          try {
            const existing = await env.DB.prepare("SELECT id FROM quizzes WHERE id=?1").bind(b.quiz_id).first();
            if (!existing) {
              await env.DB.prepare(
                "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)"
              ).bind(b.quiz_id, b.name, '', 30, 0, JSON.stringify(b.questions), '', '', b.created_by || 0).run();
              synced++;
            }
          } catch(_) {}
        }
        console.log(`[cron] Daily sync: ${synced} quizzes restored to D1`);
      } catch(e) {
        console.error(`[cron] Daily sync failed: ${e.message}`);
      }
    }
  }
};

function jsonResp(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    }
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
      "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT)",
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
    if (body.parse_mode) formData.append('parse_mode', body.parse_mode);
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
async function handleQuizData(request, url, env) {
  try {
    let id = url.searchParams.get('id');
    if (!id) id = url.pathname.replace('/api/exam/', '').split('?')[0].trim();
    if (!id) return jsonResp({ ok: false, error: 'No id' }, 400);

    const ANS = ["A","B","C","D","E"];
    const SB_URL  = 'https://wbdyjpjbczfunyhhmtry.supabase.co';
    const SB_KEY  = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndiZHlqcGpiY3pmdW55aGhtdHJ5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2OTI5ODAsImV4cCI6MjA5NjI2ODk4MH0.0WR1sgVsl_1XWZfSd0Pwoe6Uxp-2GMTksfseMn5aWjg';
    const SB2_URL = 'https://xnkuuzstschdovcyomfk.supabase.co';
    const SB2_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4';
    const RENDER_URL   = (env && env.RENDER_URL)   || 'https://quizbot-s482.onrender.com';
    const RENDER_URL_2 = (env && env.RENDER_URL_2) || '';

    function toMcqs(questions) {
      return questions.map(q => ({
        question: q.question || '',
        options: q.options || [],
        answer: ANS[q.answer_index ?? 0] || 'A',
        explanation: q.explanation || '',
      }));
    }

    function makeResp(id, name, mcqs, timer=30, source='d1', extra={}) {
      return jsonResp({
        cache_id: id, topic: name || 'Quiz', page: extra.page || 1,
        mcqs, tag: extra.tag || '', exp_footer: extra.exp_footer || '',
        channel_id: extra.channel_id || '',
        image_msg_id: extra.image_msg_id || null,
        end_msg_id: extra.end_msg_id || null,
        image_file_id: extra.image_file_id || null,
        is_new_gen: extra.is_new_gen || false,
        timer, _source: source,
      });
    }

    // ── Layer 1: Render primary + secondary account (each retried 3x/25s before moving on) ──
    const renderHosts = [RENDER_URL, RENDER_URL_2].filter(Boolean);
    for (const host of renderHosts) {
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const r = await fetch(`${host}/api/exam/${id}`, {
            signal: AbortSignal.timeout(25000)
          });
          if (r.ok) {
            const d = await r.json();
            if (d && d.mcqs && d.mcqs.length > 0) {
              return jsonResp(d);
            }
            break; // this host answered but no mcqs — real 404 on this host, try next host
          }
        } catch(e) {
          console.warn(`[quiz] Render (${host}) attempt ${attempt} failed:`, e.message);
          if (attempt < 3) await new Promise(res => setTimeout(res, 1500));
        }
      }
    }

    // ── Layer 1.5: Supabase project 1 direct — pdf_mcq_cache table (covers /img, /pdf, /csv sources) ──
    try {
      const r = await fetch(
        `${SB_URL}/rest/v1/pdf_mcq_cache?id=eq.${id}&select=*`,
        { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` }, signal: AbortSignal.timeout(10000) }
      );
      const data = await r.json();
      if (data && data[0]) {
        const c = data[0];
        return jsonResp({
          cache_id: id, topic: c.topic || 'Quiz', page: c.page_number || 1,
          mcqs: c.mcq_data || [], tag: '', exp_footer: '',
          channel_id: c.channel_id || '', image_msg_id: c.image_msg_id || null,
          end_msg_id: c.end_msg_id || null, image_file_id: c.image_file_id || null,
          is_new_gen: !!c.is_new_gen, timer: 30, _source: 'supabase_direct',
        });
      }
    } catch(e) {
      console.warn('[quiz] Supabase pdf_mcq_cache direct failed:', e.message);
    }

    // ── Layer 2: D1 (qz_ prefix quizzes only) ──
    if (id.startsWith('qz_')) {
      try {
        const row = await DB.prepare("SELECT * FROM quizzes WHERE id=?1").bind(id).first();
        if (row) {
          const questions = JSON.parse(row.csv_data || '[]');
          return makeResp(id, row.name, toMcqs(questions), row.timer || 30, 'd1');
        }
      } catch(e) {
        console.error('[quiz] D1 failed:', e.message);
      }
    }

    // ── Layer 3: Supabase primary account quiz_backups ──
    try {
      const r = await fetch(
        `${SB_URL}/rest/v1/quiz_backups?quiz_id=eq.${id}&select=*`,
        { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` }, signal: AbortSignal.timeout(10000) }
      );
      const data = await r.json();
      if (data && data[0]) {
        const b = data[0];
        if (id.startsWith('qz_')) {
          try {
            await DB.prepare(
              "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)"
            ).bind(id, b.name, '', 30, 0, JSON.stringify(b.questions), '', '', 0).run();
          } catch(_) {}
        }
        return makeResp(id, b.name, toMcqs(b.questions), 30, 'supabase');
      }
    } catch(e) {
      console.error('[quiz] Supabase primary failed:', e.message);
    }

    // ── Layer 4: Supabase Secondary account (backup of backup) ──
    try {
      const r = await fetch(
        `${SB2_URL}/rest/v1/quiz_backups?quiz_id=eq.${id}&select=*`,
        { headers: { apikey: SB2_KEY, Authorization: `Bearer ${SB2_KEY}` }, signal: AbortSignal.timeout(10000) }
      );
      const data = await r.json();
      if (data && data[0]) {
        const b = data[0];
        if (id.startsWith('qz_')) {
          try {
            await DB.prepare(
              "INSERT OR REPLACE INTO quizzes (id,name,description,timer,shuffle,csv_data,tag,exp_footer,created_by) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)"
            ).bind(id, b.name, '', 30, 0, JSON.stringify(b.questions), '', '', 0).run();
          } catch(_) {}
        }
        return makeResp(id, b.name, toMcqs(b.questions), 30, 'supabase2');
      }
    } catch(e) {
      console.error('[quiz] Supabase secondary failed:', e.message);
    }

    return jsonResp({ error: 'Quiz পাওয়া যায়নি' }, 404);
  } catch(e) {
    return jsonResp({ error: e.message }, 500);
  }
}

async function handleWebQuiz(request, url, env) {
  const quizId = url.pathname.replace('/quiz/', '').replace('/exam/', '').split('?')[0];
  if (!quizId) return new Response('Quiz ID missing', { status: 400 });

  const uid  = url.searchParams.get('uid') || '0';
  const name = url.searchParams.get('name') || 'Student';

  // index.html fetch করো — raw.githubusercontent rate-limit (429) হলে GH Pages fallback + retry
  const WORKER_ORIGIN = `https://atlasquizbotpro.hamza818483.workers.dev`;
  const HTML_SOURCES = [
    'https://raw.githubusercontent.com/hamza818483-dotcom/QuizBot/main/index.html',
    'https://hamza818483-dotcom.github.io/QuizBot/index.html',
  ];
  try {
    let html = null;
    let lastErr = 'unknown';
    for (const src of HTML_SOURCES) {
      for (let attempt = 1; attempt <= 2; attempt++) {
        try {
          const r = await fetch(src, {
            cf: { cacheEverything: true, cacheTtl: 300 },
            signal: AbortSignal.timeout(8000)
          });
          if (r.ok) { html = await r.text(); break; }
          lastErr = `HTTP ${r.status} from ${src}`;
        } catch (e) {
          lastErr = `${e.message} from ${src}`;
        }
        if (!html) await new Promise(res => setTimeout(res, 400));
      }
      if (html) break;
    }
    if (!html) throw new Error(lastErr);


    // Template variables replace — HF_SPACE_URL কে worker নিজেই handle করবে
    html = html.replace(/\{\{CACHE_ID\}\}/g,      quizId);
    html = html.replace(/\{\{USER_ID\}\}/g,       uid);
    html = html.replace(/\{\{USER_NAME\}\}/g,     encodeURIComponent(name));
    html = html.replace(/\{\{HF_SPACE_URL\}\}/g,  WORKER_ORIGIN);
    html = html.replace(/\{\{SUPABASE_URL\}\}/g,  '');
    html = html.replace(/\{\{SUPABASE_KEY\}\}/g,  '');

    // autostart=1 inject করো যাতে pre-screen skip হয়ে direct exam শুরু হয়
    html = html.replace('</head>', `<script>history.replaceState(null,'',location.pathname+'?autostart=1');</script></head>`);

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
  // v-loadsplit: real per-request load splitting across primary + secondary
  // Render (not just failover-on-crash) -- alternates requests between the
  // two so under high concurrent traffic (e.g. 100 users at once) neither
  // single 512MB instance carries the full load alone. Falls back instantly
  // to the other instance if the picked one fails, so no request is lost.
  const BOT_TOKEN    = env.ATLAS_BOT_TOKEN || env.QUIZ_BOT_TOKEN || '';
  const PRIMARY      = (env.RENDER_URL   || 'https://quizbot-s482.onrender.com') + '/webhook';
  const SECONDARY    = env.RENDER_URL_2 ? (env.RENDER_URL_2 + '/webhook') : '';
  const TG_API       = `https://api.telegram.org/bot${BOT_TOKEN}`;
  const body = await request.text();

  const targets = SECONDARY ? [PRIMARY, SECONDARY] : [PRIMARY];
  // Simple alternating pick using a Worker-global counter (best-effort even
  // split across isolates -- doesn't need to be perfectly exact, just spread
  // load roughly evenly instead of hammering one instance).
  globalThis.__rrCounter = ((globalThis.__rrCounter || 0) + 1) % targets.length;
  const picked = targets[globalThis.__rrCounter];
  const other = targets.find(t => t !== picked);

  async function tryTarget(target) {
    return await fetch(target, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: AbortSignal.timeout(45000),
    });
  }

  try {
    const r = await tryTarget(picked);
    if (r.ok) return new Response('OK');
    throw new Error(`status ${r.status}`);
  } catch (e) {
    console.warn(`[webhook] ${picked} failed (${e.message}), trying fallback...`);
    if (other) {
      try {
        const r2 = await tryTarget(other);
        if (r2.ok) return new Response('OK');
      } catch (e2) {
        console.warn(`[webhook] fallback ${other} also failed:`, e2.message);
      }
    }
    return new Response('All Render instances unavailable', { status: 502 });
  }
}


