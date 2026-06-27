# ============================================================
# chorcha_pdf.py
# chorcha_parser.py এর parsed data থেকে Premium PDF বানানোর module।
# Format অনুযায়ী (qa / cq) আলাদা box layout, Q/A আলাদা color,
# image embed (base64 data URI করে — chromium-এ network নির্ভরতা থাকে না)।
# ============================================================
import asyncio
import base64
import html
import logging

import httpx

logger = logging.getLogger("atlas.chorcha_pdf")


async def _fetch_image_as_data_uri(url: str, client: httpx.AsyncClient) -> str:
    """Image URL download করে base64 data URI বানায়। Fail হলে empty string।"""
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return ""
        content_type = r.headers.get("content-type", "image/png").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/png"
        b64 = base64.b64encode(r.content).decode()
        return f"data:{content_type};base64,{b64}"
    except Exception as e:
        logger.warning(f"[ChorchaPDF] image fetch failed: {url} -> {e}")
        return ""


async def _preload_images(data: dict) -> dict:
    """সব image URL data-URI তে map করে cache dict রিটার্ন করে।"""
    urls = set()
    for item in data["items"]:
        if item["type"] == "qa":
            urls.update(item.get("q_images", []))
            urls.update(item.get("a_images", []))
        else:
            urls.update(item.get("stem_images", []))
            for s in item.get("subs", []):
                urls.update(s.get("q_images", []))
                urls.update(s.get("a_images", []))

    cache = {}
    if not urls:
        return cache

    async with httpx.AsyncClient() as client:
        tasks = {url: _fetch_image_as_data_uri(url, client) for url in urls}
        results = await asyncio.gather(*tasks.values())
        for url, result in zip(tasks.keys(), results):
            if result:
                cache[url] = result
    return cache


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _render_images_html(urls: list, img_cache: dict) -> str:
    out = ""
    for u in urls:
        data_uri = img_cache.get(u)
        if data_uri:
            out += f'<div class="q-img-wrap"><img src="{data_uri}" class="q-img"></div>'
    return out


def _render_text(text: str) -> str:
    """Newline কে <br> করে দেয়, escape করে।"""
    return _esc(text).replace("\n", "<br>")


def _build_qa_box(item: dict, img_cache: dict, idx: int) -> str:
    tag_html = f'<span class="tag-chip">{_esc(item["tag"])}</span>' if item.get("tag") else ""
    q_imgs = _render_images_html(item.get("q_images", []), img_cache)
    a_imgs = _render_images_html(item.get("a_images", []), img_cache)
    return f"""
<div class="qa-box">
  <div class="q-row">
    <span class="q-no">{idx}.</span>
    <div class="q-text">{_render_text(item['question'])}{q_imgs}</div>
    {tag_html}
  </div>
  <div class="a-row">
    <span class="a-label">উত্তর:</span>
    <div class="a-text">{_render_text(item['answer'])}{a_imgs}</div>
  </div>
</div>"""


def _build_cq_box(item: dict, img_cache: dict, idx: int) -> str:
    tag_html = f'<span class="tag-chip">{_esc(item["tag"])}</span>' if item.get("tag") else ""
    stem_imgs = _render_images_html(item.get("stem_images", []), img_cache)
    if item.get("stem") or stem_imgs:
        stem_html = f"""<div class="stem-row">
    <span class="q-no">{idx}.</span>
    <div class="stem-text">{_render_text(item.get('stem',''))}{stem_imgs}</div>
    {tag_html}
  </div>"""
    else:
        stem_html = f'<div class="stem-row"><span class="q-no">{idx}.</span>{tag_html}</div>'

    subs_html = ""
    for sub in item.get("subs", []):
        q_imgs = _render_images_html(sub.get("q_images", []), img_cache)
        a_imgs = _render_images_html(sub.get("a_images", []), img_cache)
        subs_html += f"""
  <div class="sub-box">
    <div class="q-row">
      <span class="sub-label">{_esc(sub['label'])}.</span>
      <div class="q-text">{_render_text(sub['question'])}{q_imgs}</div>
    </div>
    <div class="a-row">
      <span class="a-label">উত্তর:</span>
      <div class="a-text">{_render_text(sub['answer'])}{a_imgs}</div>
    </div>
  </div>"""

    return f"""
<div class="cq-box">
  {stem_html}
  {subs_html}
</div>"""


PDF_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;500;600;700;800&display=swap');
@page { size: A4; margin: 12mm 10mm; }
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: 'Noto Sans Bengali', sans-serif;
  background: #fff;
  color: #1a1a2e;
  font-size: 12.5px;
}
.hdr {
  text-align: center;
  padding: 16px 18px;
  background: linear-gradient(135deg, #0d2438 0%, #1a3a5c 100%);
  color: #fff;
  border-radius: 10px;
  margin-bottom: 14px;
}
.hdr h1 { font-size: 19px; font-weight: 800; letter-spacing: 0.3px; }
.hdr .sub { font-size: 12px; color: #aecbe8; margin-top: 5px; }
.hdr .brand { font-size: 10px; color: #6f93b8; margin-top: 3px; }

/* ---- QA (short answer) boxes ---- */
.qa-box {
  border: 1.5px solid #d4dce6;
  border-radius: 9px;
  margin-bottom: 10px;
  overflow: hidden;
  break-inside: avoid;
  page-break-inside: avoid;
}
.q-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  background: #eef4fb;
  padding: 9px 12px;
  border-bottom: 1px solid #d4dce6;
}
.q-no { font-weight: 800; color: #0d4a8f; flex-shrink: 0; }
.q-text { color: #0d2438; font-weight: 600; line-height: 1.6; flex: 1; }
.tag-chip {
  background: #0d4a8f; color: #fff; font-size: 10px; font-weight: 700;
  padding: 2px 9px; border-radius: 20px; white-space: nowrap; flex-shrink: 0;
}
.a-row {
  display: flex; align-items: flex-start; gap: 8px;
  background: #fff8ec; padding: 10px 12px;
}
.a-label { font-weight: 800; color: #a15c00; flex-shrink: 0; }
.a-text { color: #4a3000; line-height: 1.7; flex: 1; }

/* ---- CQ boxes ---- */
.cq-box {
  border: 2px solid #c7d6e8;
  border-radius: 10px;
  margin-bottom: 14px;
  padding: 10px;
  break-inside: avoid;
  page-break-inside: avoid;
  background: #fbfdff;
}
.stem-row {
  display: flex; align-items: flex-start; gap: 8px;
  background: #dde9f7; padding: 9px 12px; border-radius: 7px; margin-bottom: 8px;
}
.stem-text { color: #0d2438; font-weight: 700; line-height: 1.65; flex: 1; }
.sub-box {
  border: 1px solid #d4dce6;
  border-radius: 8px;
  margin-bottom: 8px;
  overflow: hidden;
  break-inside: avoid;
  page-break-inside: avoid;
}
.sub-box .q-row { background: #eef4fb; }
.sub-label { font-weight: 800; color: #0d4a8f; flex-shrink: 0; min-width: 18px; }

.q-img-wrap { margin-top: 6px; }
.q-img {
  max-width: 100%; max-height: 320px; display: block;
  margin: 6px auto 0; border-radius: 6px; border: 1px solid #d4dce6;
}

.footer {
  text-align: center; font-size: 9.5px; color: #9aa5b1;
  margin-top: 14px; padding-top: 10px; border-top: 1px solid #e3e8ee;
}
"""


async def build_chorcha_pdf_html(data: dict) -> str:
    """parse_chorcha_file() এর output থেকে full printable HTML বানায়।"""
    img_cache = await _preload_images(data)

    boxes_html = ""
    for idx, item in enumerate(data["items"], 1):
        if item["type"] == "qa":
            boxes_html += _build_qa_box(item, img_cache, idx)
        else:
            boxes_html += _build_cq_box(item, img_cache, idx)

    total = len(data["items"])
    fmt_label = "CQ (সৃজনশীল)" if data["format"] == "cq" else "জ্ঞানভিত্তিক প্রশ্নোত্তর"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>{PDF_CSS}</style></head>
<body>
<div class="hdr">
  <h1>📚 {_esc(data['page_title'])}</h1>
  <div class="sub">🧩 {fmt_label} &nbsp;|&nbsp; 📝 মোট: {total} টি প্রশ্ন</div>
  <div class="brand">🚀 ATLAS APP — Premium Question Bank PDF</div>
</div>
{boxes_html}
<div class="footer">এটলাস এডুকেশন প্ল্যাটফর্ম — Atlascourses.com</div>
</body></html>"""