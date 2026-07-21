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
    if (url.pathname === '/r2/put' && request.method === 'POST') return await r2Put(request, env);

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

    // v4.2: HF account permanently banned — these routes now go to Render.
    // NOTE: checked BEFORE the generic /api/exam/ prefix match below, since
    // /api/exam/result also starts with /api/exam/ and was being swallowed
    // by handleQuizData (which then misread "result" as a quiz id and 404'd) —
    // result-saving never reached Render at all, regardless of bot uptime.
    //
    // Backend host for exam-related write/read APIs. If this host is down,
    // we no longer just 502 — result/leaderboard/bookmark now fall back to
    // writing/reading D1 + both Supabase accounts directly from the Worker,
    // so the exam still "works" end-to-end even with the backend fully off.
    const HF_ONLY = ['/api/exam/result', '/api/new-exam', '/api/bookmark',
                     '/api/leaderboard', '/api/solve-pdf', '/api/tg-image', '/api/new-exam/status'];
    if (HF_ONLY.some(p => url.pathname.startsWith(p))) {
      const RENDER = env.RENDER_URL || env.HF_SPACE_URL || 'https://hamza-02-quizbot.hf.space';
      const renderReq = new Request(RENDER + url.pathname + url.search, {
        method: request.method,
        headers: request.headers,
        body: request.method !== 'GET' ? request.body : undefined,
      });
      try {
        const r = await fetch(renderReq, { signal: AbortSignal.timeout(20000) });
        if (r.ok) return r;
        // Backend responded but with an error (5xx) — still worth trying the
        // direct fallback path below rather than returning its error as-is.
      } catch(e) {
        console.warn('[HF_ONLY] backend unreachable, trying direct fallback:', e.message);
      }
      // ── Backend down/erroring: try direct D1 + Supabase(x2) fallback ──
      const fb = await handleExamBackendFallback(request, url, env);
      if (fb) return fb;
      return jsonResp({ ok: false, error: 'Backend unavailable, no fallback path for this route' }, 502);
    }

    // Web Quiz — CF serves index.html, API data via Render→Supabase→D1 chain
    if (url.pathname.startsWith('/quiz/')) return await handleWebQuiz(request, url, env);
    if (url.pathname.startsWith('/exam/')) return await handleWebQuiz(request, url, env);
    if (url.pathname.startsWith('/api/exam/')) return await handleQuizData(request, url, env, ctx);
    if (url.pathname === '/quiz-data' && request.method === 'GET') return await handleQuizData(request, url, env, ctx);

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
      ctx.waitUntil(forwardToHFWithFallback(forwardReq, bodyText, env));
      return new Response('OK');
    }

    // Health check
    if (url.pathname === '/health') return jsonResp({ ok: true, status: 'alive' });

    // ── /cron-check: Pages Functions e built-in Cron Trigger nai (Workers-only
    // feature), tai GitHub Actions scheduled workflow theke ei endpoint HTTP
    // diye hit kore, jeta ekhoni scheduled() function er shathe hubohu same
    // failover+keepalive logic run kore. Simple shared-secret diye protect. ──
    if (url.pathname === '/cron-check') {
      const provided = url.searchParams.get('key') || request.headers.get('X-Cron-Key') || '';
      const expected = env.CRON_SECRET || '';
      if (expected && provided !== expected) {
        return jsonResp({ ok: false, error: 'Unauthorized' }, 401);
      }
      ctx.waitUntil(runCronCheck(env));
      return jsonResp({ ok: true, message: 'Cron check triggered' });
    }

    return jsonResp({ ok: true, service: 'ATLAS Bot Proxy', version: '2.0' });
  },

  // ── Cron: Render keep-alive + Primary→Secondary failover + daily Supabase→D1 sync ──
  // (Pages e ei handler kokhono auto-fire hoy na — /cron-check HTTP route-i
  // actual trigger. Worker-e move korle eta nijei kaj korbe.)
  async scheduled(event, env) {
    await runCronCheck(env);
  }
};

async function runCronCheck(env) {
    const RENDER_URL   = env.RENDER_URL || env.HF_SPACE_URL || 'https://hamza-02-quizbot.hf.space';
    const RENDER_URL_2 = env.RENDER_URL_2 || '';
    const BOT_TOKEN     = env.ATLAS_BOT_TOKEN || env.QUIZ_BOT_TOKEN || '';
    const OWNER_ID      = env.OWNER_ID || '';
    const WEBHOOK_SECRET = env.WEBHOOK_SECRET || '';

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
              body: `url=${encodeURIComponent(RENDER_URL_2 + '/webhook')}&drop_pending_updates=false` + (WEBHOOK_SECRET ? `&secret_token=${encodeURIComponent(WEBHOOK_SECRET)}` : '')
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
            body: `url=${encodeURIComponent(RENDER_URL + '/webhook')}&drop_pending_updates=false` + (WEBHOOK_SECRET ? `&secret_token=${encodeURIComponent(WEBHOOK_SECRET)}` : '')
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

async function r2Put(request, env) {
  try {
    const body = await request.json();
    const token = body.token || request.headers.get('X-D1-Token') || '';
    if (globalThis.D1_TOKEN && token !== globalThis.D1_TOKEN) {
      return jsonResp({ ok: false, error: 'Unauthorized' }, 401);
    }
    const { id, name, mcqs, timer, extra } = body;
    if (!id || !mcqs) {
      return jsonResp({ ok: false, error: 'Missing id or mcqs' }, 400);
    }
    if (!env.PDF_BUCKET) {
      return jsonResp({ ok: false, error: 'R2 bucket not bound' }, 500);
    }
    await env.PDF_BUCKET.put(`quiz-backups/${id}.json`,
      JSON.stringify({ name: name || 'Quiz', mcqs, timer: timer || 30, extra: extra || {} }),
      { httpMetadata: { contentType: 'application/json' } }
    );
    return jsonResp({ ok: true });
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

    const filename = body.filename || 'file';
    const isAscii = /^[\x00-\x7F]*$/.test(filename);

    let resp;
    if (isAscii) {
      // Plain ASCII filename (e.g. English PDF names) — the built-in
      // FormData/Blob path works fine and is simpler, keep using it.
      const formData = new FormData();
      formData.append('chat_id', String(body.chat_id));
      formData.append('caption', body.caption || '');
      if (body.parse_mode) formData.append('parse_mode', body.parse_mode);
      formData.append('document', new Blob([bytes], { type: body.mime_type || 'application/octet-stream' }), filename);
      if (body.reply_to_message_id) formData.append('reply_to_message_id', String(body.reply_to_message_id));
      if (body.message_thread_id) formData.append('message_thread_id', String(body.message_thread_id));
      resp = await fetch(`https://api.telegram.org/bot${token}/sendDocument`, { method: 'POST', body: formData });
    } else {
      // Non-ASCII filename (Bengali/etc) — Cloudflare Workers' built-in
      // FormData serializes Content-Disposition's filename as a raw string
      // without RFC 5987 (filename*=UTF-8''...) encoding, which corrupts
      // multi-byte UTF-8 names (mojibake) once Telegram/intermediate proxies
      // re-interpret the bytes as Latin-1. Build the multipart body by hand
      // instead, with a properly percent-encoded filename* parameter so the
      // original Bengali name survives intact.
      const boundary = '----AtlasWM' + crypto.randomUUID().replace(/-/g, '');
      const encFilenameStar = encodeURIComponent(filename).replace(/'/g, '%27');
      const enc = new TextEncoder();
      const parts = [];
      const pushField = (name, value) => {
        parts.push(enc.encode(
          `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`
        ));
      };
      pushField('chat_id', String(body.chat_id));
      pushField('caption', body.caption || '');
      if (body.parse_mode) pushField('parse_mode', body.parse_mode);
      if (body.reply_to_message_id) pushField('reply_to_message_id', String(body.reply_to_message_id));
      if (body.message_thread_id) pushField('message_thread_id', String(body.message_thread_id));
      // File part: include BOTH a plain ASCII-safe fallback name and the
      // RFC 5987 filename* parameter (standard "belt and suspenders" pattern
      // for non-ASCII filenames in multipart uploads).
      parts.push(enc.encode(
        `--${boundary}\r\nContent-Disposition: form-data; name="document"; filename="file.pdf"; filename*=UTF-8''${encFilenameStar}\r\n` +
        `Content-Type: ${body.mime_type || 'application/octet-stream'}\r\n\r\n`
      ));
      parts.push(bytes);
      parts.push(enc.encode(`\r\n--${boundary}--\r\n`));

      const totalLen = parts.reduce((sum, p) => sum + p.length, 0);
      const fullBody = new Uint8Array(totalLen);
      let offset = 0;
      for (const p of parts) { fullBody.set(p, offset); offset += p.length; }

      resp = await fetch(`https://api.telegram.org/bot${token}/sendDocument`, {
        method: 'POST',
        headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}` },
        body: fullBody,
      });
    }

    const result = await resp.json();
    return new Response(JSON.stringify(result), {
      status: resp.status, headers: { 'Content-Type': 'application/json' }
    });
  } catch (e) {
    return jsonResp({ ok: false, error: e.message }, 500);
  }
}

// ============================================================
// EXAM BACKEND FALLBACK — direct D1 + Supabase(x2) writes/reads,
// used only when the primary HF backend is unreachable/erroring.
// Covers /api/exam/result (submit), /api/leaderboard (read),
// /api/bookmark (save/delete). /api/new-exam, /api/solve-pdf,
// /api/tg-image are NOT covered here (they need real PDF/AI
// generation work that only the Python backend can do) — those
// still fail with a clear error if the backend is down.
// ============================================================
async function handleExamBackendFallback(request, url, env) {
  const SB_URL  = 'https://wbdyjpjbczfunyhhmtry.supabase.co';
  const SB_KEY  = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndiZHlqcGpiY3pmdW55aGhtdHJ5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2OTI5ODAsImV4cCI6MjA5NjI2ODk4MH0.0WR1sgVsl_1XWZfSd0Pwoe6Uxp-2GMTksfseMn5aWjg';
  const SB2_URL = 'https://xnkuuzstschdovcyomfk.supabase.co';
  const SB2_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4';

  async function sbInsert(base, key, table, row, onConflict) {
    const q = onConflict ? `?on_conflict=${onConflict}` : '';
    return await fetch(`${base}/rest/v1/${table}${q}`, {
      method: 'POST',
      headers: {
        apikey: key, Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
        Prefer: onConflict ? 'resolution=merge-duplicates' : 'return=minimal',
      },
      body: JSON.stringify(row),
      signal: AbortSignal.timeout(10000),
    });
  }

  try {
    if (url.pathname.startsWith('/api/exam/result') && request.method === 'POST') {
      const data = await request.json();
      const wrong = data.wrong || 0, correct = data.correct || 0, total = data.total || 0;
      const negative = Math.round(wrong * 0.25 * 100) / 100;
      const final_score = Math.round((correct - negative) * 100) / 100;
      const row = {
        cache_id: data.cache_id, user_id: data.user_id, user_name: data.user_name || 'User',
        topic: data.topic || '', page_number: data.page || 0, total, correct, wrong,
        skipped: data.skipped || 0, negative_marks: negative, final_score,
        time_taken: data.time_taken || 0,
      };
      // D1 first (fastest, most reliable from inside the Worker itself)
      try {
        await env.DB.prepare(
          "CREATE TABLE IF NOT EXISTS web_exam_results (id INTEGER PRIMARY KEY AUTOINCREMENT, cache_id TEXT, user_id INTEGER, user_name TEXT, topic TEXT, page_number INTEGER, total INTEGER, correct INTEGER, wrong INTEGER, skipped INTEGER, negative_marks REAL, final_score REAL, time_taken INTEGER)"
        ).run();
        await env.DB.prepare(
          "INSERT INTO web_exam_results (cache_id,user_id,user_name,topic,page_number,total,correct,wrong,skipped,negative_marks,final_score,time_taken) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)"
        ).bind(row.cache_id, row.user_id, row.user_name, row.topic, row.page_number, row.total,
               row.correct, row.wrong, row.skipped, row.negative_marks, row.final_score, row.time_taken).run();
        // Leaderboard upsert too, so scores show up even in fallback mode
        await env.DB.prepare(
          "CREATE TABLE IF NOT EXISTS web_exam_leaderboard (cache_id TEXT, user_id INTEGER, user_name TEXT, final_score REAL, correct INTEGER, total INTEGER, updated_at INTEGER, PRIMARY KEY (cache_id, user_id))"
        ).run();
        await env.DB.prepare(
          "INSERT INTO web_exam_leaderboard (cache_id,user_id,user_name,final_score,correct,total,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(cache_id,user_id) DO UPDATE SET final_score=excluded.final_score, correct=excluded.correct, total=excluded.total, user_name=excluded.user_name, updated_at=excluded.updated_at"
        ).bind(row.cache_id, row.user_id, row.user_name, row.final_score, row.correct, row.total, Date.now()).run();
      } catch (e) {
        console.warn('[Fallback] D1 result write failed:', e.message);
      }
      // Best-effort mirror to both Supabase accounts too (non-blocking on each other)
      sbInsert(SB_URL, SB_KEY, 'web_exam_results', row).catch(e => console.warn('[Fallback] SB1 result write failed:', e.message));
      sbInsert(SB2_URL, SB2_KEY, 'web_exam_results', row).catch(e => console.warn('[Fallback] SB2 result write failed:', e.message));
      const pct = total ? Math.round((correct / total) * 100) : 0;
      return jsonResp({ ok: true, final_score, negative, pct, _source: 'fallback_direct' });
    }

    if (url.pathname.startsWith('/api/leaderboard/') && request.method === 'GET') {
      const cacheId = url.pathname.replace('/api/leaderboard/', '').split('?')[0].trim();
      // Try D1 first
      try {
        const { results } = await env.DB.prepare(
          "SELECT * FROM web_exam_leaderboard WHERE cache_id=?1 ORDER BY final_score DESC LIMIT 50"
        ).bind(cacheId).all();
        if (results && results.length > 0) {
          return jsonResp({ ok: true, data: results, _source: 'fallback_d1' });
        }
      } catch (e) {
        console.warn('[Fallback] D1 leaderboard read failed:', e.message);
      }
      // Then both Supabase accounts
      for (const [base, key] of [[SB_URL, SB_KEY], [SB2_URL, SB2_KEY]]) {
        try {
          const r = await fetch(
            `${base}/rest/v1/web_exam_leaderboard?cache_id=eq.${cacheId}&select=*&order=final_score.desc&limit=50`,
            { headers: { apikey: key, Authorization: `Bearer ${key}` }, signal: AbortSignal.timeout(10000) }
          );
          const data = await r.json();
          if (Array.isArray(data) && data.length > 0) {
            return jsonResp({ ok: true, data, _source: 'fallback_supabase' });
          }
        } catch (e) {
          console.warn('[Fallback] Supabase leaderboard read failed:', e.message);
        }
      }
      return jsonResp({ ok: true, data: [], _source: 'fallback_empty' });
    }

    if (url.pathname.startsWith('/api/bookmark')) {
      const data = await request.json().catch(() => ({}));
      if (request.method === 'POST') {
        try {
          await env.DB.prepare(
            "CREATE TABLE IF NOT EXISTS bookmarks (user_id INTEGER, cache_id TEXT, question_index INTEGER, question_data TEXT, topic TEXT, page_number INTEGER, PRIMARY KEY (user_id, cache_id, question_index))"
          ).run();
          await env.DB.prepare(
            "INSERT INTO bookmarks (user_id,cache_id,question_index,question_data,topic,page_number) VALUES (?1,?2,?3,?4,?5,?6) ON CONFLICT(user_id,cache_id,question_index) DO UPDATE SET question_data=excluded.question_data, topic=excluded.topic, page_number=excluded.page_number"
          ).bind(data.user_id, data.cache_id, data.question_index,
                 JSON.stringify(data.question_data || {}), data.topic, data.page).run();
          return jsonResp({ ok: true, _source: 'fallback_d1' });
        } catch (e) {
          return jsonResp({ ok: false, error: e.message }, 500);
        }
      }
      if (request.method === 'DELETE') {
        try {
          await env.DB.prepare(
            "DELETE FROM bookmarks WHERE user_id=?1 AND cache_id=?2 AND question_index=?3"
          ).bind(data.user_id, data.cache_id, data.question_index).run();
          return jsonResp({ ok: true, _source: 'fallback_d1' });
        } catch (e) {
          return jsonResp({ ok: false, error: e.message }, 500);
        }
      }
    }
  } catch (e) {
    console.error('[Fallback] handleExamBackendFallback error:', e.message);
    return jsonResp({ ok: false, error: e.message }, 500);
  }
  return null; // no fallback path matched (e.g. /api/new-exam, /api/solve-pdf, /api/tg-image)
}

// ============================================================
// WEB QUIZ — Same index.html style, runs entirely on CF
// ============================================================
async function handleQuizData(request, url, env, ctx) {
  try {
    let id = url.searchParams.get('id');
    if (!id) id = url.pathname.replace('/api/exam/', '').split('?')[0].trim();
    if (!id) return jsonResp({ ok: false, error: 'No id' }, 400);

    const ANS = ["A","B","C","D","E"];
    const SB_URL  = 'https://wbdyjpjbczfunyhhmtry.supabase.co';
    const SB_KEY  = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndiZHlqcGpiY3pmdW55aGhtdHJ5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2OTI5ODAsImV4cCI6MjA5NjI2ODk4MH0.0WR1sgVsl_1XWZfSd0Pwoe6Uxp-2GMTksfseMn5aWjg';
    const SB2_URL = 'https://xnkuuzstschdovcyomfk.supabase.co';
    const SB2_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4';
    const RENDER_URL   = (env && (env.RENDER_URL || env.HF_SPACE_URL)) || 'https://hamza-02-quizbot.hf.space';
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

    // ── Layer 1: Bot host (HF Space) check. Kept short (1 attempt, 3s
    //    timeout per host) — if the bot process is off, this must fail
    //    FAST so the person doesn't sit on a loading screen for minutes
    //    before Layer 1.5 (Supabase direct) kicks in. This host is only
    //    ever fresher than the DB layers when a quiz was JUST generated
    //    seconds ago and hasn't been mirrored yet — a single quick attempt
    //    covers that case without punishing the common bot-is-off scenario. ──
    const renderHosts = [RENDER_URL, RENDER_URL_2].filter(Boolean);
    for (const host of renderHosts) {
      try {
        const r = await fetch(`${host}/api/exam/${id}`, {
          signal: AbortSignal.timeout(3000)
        });
        if (r.ok) {
          const d = await r.json();
          if (d && d.mcqs && d.mcqs.length > 0) {
            ctx.waitUntil(backupToR2(env, id, d.topic, d.mcqs, d.timer, {
              tag: d.tag, exp_footer: d.exp_footer, channel_id: d.channel_id,
              image_msg_id: d.image_msg_id, end_msg_id: d.end_msg_id,
              image_file_id: d.image_file_id, is_new_gen: d.is_new_gen, page: d.page,
            }));
            return jsonResp(d);
          }
        }
      } catch(e) {
        console.warn(`[quiz] Render (${host}) quick-check failed, falling back to DB layers:`, e.message);
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
        const resp = {
          cache_id: id, topic: c.topic || 'Quiz', page: c.page_number || 1,
          mcqs: c.mcq_data || [], tag: '', exp_footer: '',
          channel_id: c.channel_id || '', image_msg_id: c.image_msg_id || null,
          end_msg_id: c.end_msg_id || null, image_file_id: c.image_file_id || null,
          is_new_gen: !!c.is_new_gen, timer: 30, _source: 'supabase_direct',
        };
        ctx.waitUntil(backupToR2(env, id, resp.topic, resp.mcqs, resp.timer, {
          tag: resp.tag, exp_footer: resp.exp_footer, channel_id: resp.channel_id,
          image_msg_id: resp.image_msg_id, end_msg_id: resp.end_msg_id,
          image_file_id: resp.image_file_id, is_new_gen: resp.is_new_gen, page: resp.page,
        }));
        return jsonResp(resp);
      }
    } catch(e) {
      console.warn('[quiz] Supabase pdf_mcq_cache direct failed:', e.message);
    }

    // ── Layer 2: D1 (any cache_id — /pdf caches are now mirrored here too) ──
    try {
      const row = await DB.prepare("SELECT * FROM quizzes WHERE id=?1").bind(id).first();
      if (row) {
        const questions = JSON.parse(row.csv_data || '[]');
        const mcqs = toMcqs(questions);
        ctx.waitUntil(backupToR2(env, id, row.name, mcqs, row.timer || 30, {}));
        return makeResp(id, row.name, mcqs, row.timer || 30, 'd1');
      }
    } catch(e) {
      console.error('[quiz] D1 failed:', e.message);
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
        ctx.waitUntil(backupToR2(env, id, b.name, toMcqs(b.questions), 30, {}));
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
        ctx.waitUntil(backupToR2(env, id, b.name, toMcqs(b.questions), 30, {}));
        return makeResp(id, b.name, toMcqs(b.questions), 30, 'supabase2');
      }
    } catch(e) {
      console.error('[quiz] Supabase secondary failed:', e.message);
    }

    // ── Layer 5: R2 (atlas-pdfs bucket) — final fallback if Render, both
    //    Supabase accounts, AND D1 are all unreachable/empty. Every quiz
    //    successfully resolved by ANY layer above is also written here
    //    (see backupToR2 calls below) so this layer stays populated. ──
    if (env.PDF_BUCKET) {
      try {
        const obj = await env.PDF_BUCKET.get(`quiz-backups/${id}.json`);
        if (obj) {
          const b = JSON.parse(await obj.text());
          return makeResp(id, b.name, b.mcqs, b.timer || 30, 'r2_backup', b.extra || {});
        }
      } catch(e) {
        console.error('[quiz] R2 backup read failed:', e.message);
      }
    }

    return jsonResp({ error: 'Quiz পাওয়া যায়নি' }, 404);
  } catch(e) {
    return jsonResp({ error: e.message }, 500);
  }
}

// Fire-and-forget write-through backup to R2 so Layer 5 above stays populated.
// Never blocks or fails the actual response — best-effort only.
async function backupToR2(env, id, name, mcqs, timer, extra) {
  if (!env.PDF_BUCKET) return;
  try {
    await env.PDF_BUCKET.put(`quiz-backups/${id}.json`, JSON.stringify({ name, mcqs, timer, extra }), {
      httpMetadata: { contentType: 'application/json' },
    });
  } catch(e) {
    console.warn('[quiz] R2 backup write failed:', e.message);
  }
}

async function handleWebQuiz(request, url, env) {
  const quizId = url.pathname.replace('/quiz/', '').replace('/exam/', '').split('?')[0];
  if (!quizId) return new Response('Quiz ID missing', { status: 400 });

  const uid  = url.searchParams.get('uid') || '0';
  const name = url.searchParams.get('name') || 'Student';

  // Bug fix: this was hardcoded to a stale/retired HF Space URL, which meant
  // every fetch the exam page makes (quiz data, results, leaderboard, bookmarks)
  // was pointed at a dead host — silently breaking the entire "bot is down"
  // fallback this Worker exists for. The Worker's own live origin (wherever
  // it's actually deployed/reached from) is always correct and always up,
  // since handleQuizData/handleWebQuiz live in this same file.
  const WORKER_ORIGIN = url.origin;
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
  // v-single-active: per DEPLOYMENT.md, only ONE backend may serve live
  // Telegram traffic at a time (dual-webhook/dual-instance previously caused
  // an account ban). PRIMARY is always used; SECONDARY (the old/standby
  // fallback space) is ONLY tried if PRIMARY genuinely fails -- it must never
  // receive live traffic on its own turn. The previous round-robin split sent
  // every-other update to the stale fallback space (broken webhook, 15s+
  // latency), which silently ate the first attempt of every command until
  // the user resent it and it happened to land on PRIMARY.
  const BOT_TOKEN    = env.ATLAS_BOT_TOKEN || env.QUIZ_BOT_TOKEN || '';
  const PRIMARY      = (env.RENDER_URL || env.HF_SPACE_URL || 'https://hamza-02-quizbot.hf.space') + '/webhook';
  const SECONDARY    = env.RENDER_URL_2 ? (env.RENDER_URL_2 + '/webhook') : '';
  const TG_API       = `https://api.telegram.org/bot${BOT_TOKEN}`;
  const body = await request.text();

  const picked = PRIMARY;
  const other = SECONDARY || null;

  async function tryTarget(target) {
    return await fetch(target, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '',
      },
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
    // No second instance (or it also failed) — retry the SAME target with
    // growing waits before giving up. Common single-instance case: HF Space
    // was asleep/cold on the first hit (timeout/connection refused mid-boot).
    // A cold HF Docker Space can take well over 60s to fully boot (image
    // pull + deps + app import), so a single 5s+60s retry sometimes wasn't
    // enough and the update was dropped for good (Telegram already got its
    // 200 OK earlier and never resends) — user had to send the command again
    // manually, by which point the container had finished booting from the
    // first attempt. Now retries 3x with increasing waits/timeouts to cover
    // slow cold boots within this single delivery.
    const retryWaits = [5000, 15000, 30000];
    const retryTimeouts = [60000, 75000, 90000];
    for (let i = 0; i < retryWaits.length; i++) {
      try {
        console.warn(`[webhook] waiting ${retryWaits[i]}ms then retrying ${picked} (attempt ${i + 2}, possible cold start)...`);
        await new Promise(r => setTimeout(r, retryWaits[i]));
        const rN = await fetch(picked, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Telegram-Bot-Api-Secret-Token': request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '',
          },
          body,
          signal: AbortSignal.timeout(retryTimeouts[i]),
        });
        if (rN.ok) return new Response('OK');
      } catch (eN) {
        console.warn(`[webhook] retry attempt ${i + 2} of ${picked} also failed:`, eN.message);
      }
    }
    return new Response('All Render instances unavailable', { status: 502 });
  }
}

// ============================================================
// D1 QUIZ — PERMANENT FALLBACK (works even if the bot backend is fully off)
// ============================================================
// When both Render/HF backends are unreachable, the bot itself can't respond
// to any Telegram update. But a /start <quiz_id> deep-link (D1 quiz share
// link) doesn't need the bot's full logic — it just needs the quiz's
// questions sent as Telegram native quiz-type polls. Telegram's own poll UI
// shows correct/wrong + explanation instantly, with ZERO backend tracking
// needed after sending. So this Pages Worker (always-on, independent of the
// Python backend) can serve that specific case directly from D1, keeping D1
// quizzes permanently usable (without score/leaderboard tracking) even if
// the Python bot is fully down.
async function forwardToHFWithFallback(request, bodyText, env) {
  const r = await forwardToHF(request, env);
  if (r.status !== 502) return r; // backend handled it fine, nothing more to do

  // Backend is fully down — check if this update is a /start <quiz_id> deep-link.
  try {
    const update = JSON.parse(bodyText);
    const msg = update.message;
    const text = (msg && msg.text) || '';
    const m = text.match(/^\/start\s+([A-Za-z0-9_]+)/);
    if (!m) return r; // not a quiz deep-link — nothing this Worker can do
    let quizId = m[1];
    // Deep-links can carry pdf_/poll_/premium_ prefixes for other features —
    // only D1-quiz plain IDs are handled here; strip a bare "d1_" prefix if present.
    if (quizId.startsWith('d1_')) quizId = quizId.slice(3);

    const chatId = msg.chat.id;
    await sendD1QuizFallbackPoll(quizId, chatId, env);
  } catch (e) {
    console.warn('[fallback] quiz-poll fallback failed:', e.message);
  }
  return r;
}

async function sendD1QuizFallbackPoll(quizId, chatId, env) {
  const BOT_TOKEN = env.ATLAS_BOT_TOKEN || env.QUIZ_BOT_TOKEN || '';
  const TG_API = `https://api.telegram.org/bot${BOT_TOKEN}`;

  const row = await env.DB.prepare("SELECT * FROM quizzes WHERE id = ?1").bind(quizId).first();
  if (!row) return; // quiz not found in D1 — silently skip, nothing to notify with (bot is down)

  let questions;
  try {
    questions = JSON.parse(row.csv_data);
  } catch (e) {
    return;
  }
  if (!Array.isArray(questions) || questions.length === 0) return;

  // Let the person know upfront this is limited-mode (no scoring), since the
  // bot itself is unreachable to explain further.
  await fetch(`${TG_API}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      text: `📚 ${row.name || 'Quiz'}\n\n⚠️ Bot এখন সাময়িক বন্ধ — score/leaderboard track হবে না, কিন্তু প্রশ্নগুলো normal quiz poll হিসেবে solve করতে পারবে।`,
    }),
  });

  const MAX_POLL_QUESTION = 300; // Telegram limit
  const MAX_POLL_OPTION = 100;   // Telegram limit

  for (const q of questions) {
    const options = (q.options || []).map(o => String(o).slice(0, MAX_POLL_OPTION));
    if (options.length < 2) continue;
    const correctIdx = typeof q.answer_index === 'number' ? q.answer_index : 0;
    const explanation = (q.explanation || '').slice(0, 200); // Telegram poll explanation limit

    try {
      await fetch(`${TG_API}/sendPoll`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: chatId,
          question: String(q.question || '').slice(0, MAX_POLL_QUESTION),
          options: JSON.stringify(options),
          type: 'quiz',
          correct_option_id: correctIdx,
          explanation: explanation || undefined,
          is_anonymous: false,
        }),
      });
    } catch (e) {
      console.warn('[fallback] sendPoll failed:', e.message);
    }
    // Small delay to avoid Telegram flood limits when a quiz has many questions.
    await new Promise(res => setTimeout(res, 350));
  }

  await fetch(`${TG_API}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      text: `✅ শেষ! Bot চালু হলে আবার এই লিংকেই score-tracked mode এ practice করতে পারবে।`,
    }),
  });
}


