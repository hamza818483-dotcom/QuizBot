# ============================================================
# chorcha_parser.py
# chorcha.net থেকে save করা .mhtml / .html ফাইল থেকে
# প্রশ্ন-উত্তর (ক ভান্ডার / খ ভান্ডার / CQ) parse করার module
# ============================================================
import email
import re
import logging

from bs4 import BeautifulSoup

from atlas_mhtml import SUB_MAP, SUP_MAP, aggressive_clean

logger = logging.getLogger("atlas")


def _extract_html_from_bytes(raw: bytes) -> str:
    """
    Input bytes .mhtml বা plain .html হতে পারে।
    .mhtml হলে multipart/related parse করে আসল text/html part বের করে।
    """
    head = raw[:2000].lower()
    looks_like_mime = b"mime-version" in head or b"content-type: multipart" in head

    if looks_like_mime:
        try:
            msg = email.message_from_bytes(raw)
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        except Exception as e:
            logger.error(f"[ChorchaParser] mhtml parse error: {e}")

    # fallback: plain .html
    return raw.decode("utf-8", errors="replace")


def _clean_node_text(node) -> str:
    """KaTeX duplicate MathML সরিয়ে clean text বের করে (images রেখে দেয়, alt দিয়ে replace করে না)।
    <sub>/<sup> কে proper Unicode subscript/superscript এ convert করে (atlas_mhtml.py এর
    format_content() এর মতোই লজিক), তারপর aggressive_clean() দিয়ে chemistry/physics
    notation স্পেসিং ঠিক করে -- না হলে '80°C' -> '⁸⁰°C', 'SO₄²⁻' -> 'SO 4 2 −',
    '1s²2s²2p⁶' -> '( 1 s²2 s²2 p⁶' এর মতো ভুল csv তে যেত।"""
    if node is None:
        return ""
    node = node.__copy__()
    for m in node.select(".katex-mathml"):
        m.decompose()

    for sub in node.find_all(["sub", "msub"]):
        sub.replace_with(sub.get_text(strip=True).translate(SUB_MAP))
    for sup in node.find_all(["sup", "msup"]):
        sup_text = sup.get_text(strip=True)
        # degree-temperature exception: a <sup> digit immediately followed
        # by "°" text is NOT a math exponent (e.g. source wraps "20" in
        # <sup> right before separate "°C") -- keep it as plain text so
        # "20°C" never becomes "²⁰°C".
        next_sib = sup.next_sibling
        next_text = next_sib.strip() if isinstance(next_sib, str) else (next_sib.get_text() if next_sib else "")
        if sup_text.rstrip().endswith("°") or (next_text or "").lstrip().startswith("°"):
            sup.replace_with(sup_text)
        else:
            sup.replace_with(sup_text.translate(SUP_MAP))

    text = node.get_text(" ", strip=True)
    text = aggressive_clean(text)
    return text


def _extract_images(node) -> list:
    """node-এর ভিতরের সব <img> এর src list করে দেয় (svg icon বাদ দিয়ে, content image রেখে)"""
    if node is None:
        return []
    urls = []
    for img in node.select("img"):
        src = img.get("src", "")
        if not src:
            continue
        if "svgs/" in src or src.endswith(".svg"):
            continue  # UI icon, content না
        if src not in urls:
            urls.append(src)
    return urls


def _get_solution_text(section_node) -> str:
    """section.সল্যুশন এর ভিতরের answer div বের করে clean text দেয়"""
    if section_node is None:
        return ""
    ans_div = section_node.find("div", attrs={"class": re.compile(r"whitespace-pre-line")})
    if ans_div is None:
        # fallback: section এর সব div এর শেষ অংশ
        divs = section_node.find_all("div")
        ans_div = divs[-1] if divs else section_node
    return _clean_node_text(ans_div)


def _get_solution_images(section_node) -> list:
    if section_node is None:
        return []
    ans_div = section_node.find("div", attrs={"class": re.compile(r"whitespace-pre-line")})
    return _extract_images(ans_div if ans_div is not None else section_node)


def _get_tag(block) -> str:
    tag_span = block.select_one("span.tag")
    if tag_span:
        return tag_span.get_text(strip=True)
    return ""


def parse_chorcha_file(raw: bytes) -> dict:
    """
    Main entry point.
    Returns:
    {
        "page_title": str,
        "format": "qa" | "cq",
        "items": [
            # format == "qa":
            {"type": "qa", "no": int, "question": str, "q_images": [...],
             "tag": str, "answer": str, "a_images": [...]},
            # format == "cq":
            {"type": "cq", "no": int, "stem": str, "stem_images": [...], "tag": str,
             "subs": [{"label": "ক", "question": str, "q_images": [...],
                        "answer": str, "a_images": [...]}, ...]}
        ]
    }
    """
    html = _extract_html_from_bytes(raw)
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else "প্রশ্ন ব্যাংক"
    page_title = re.sub(r"\s*-\s*চর্চা\s*$", "", page_title).strip()

    blocks = soup.select("div.border.rounded-xl")
    # remove nested duplicates (defensive — keep only top-level matches)
    top_blocks = []
    seen_ids = set()
    for b in blocks:
        if id(b) in seen_ids:
            continue
        # if this block is contained inside another block already collected, skip
        is_nested = any(b in parent.descendants for parent in top_blocks)
        if is_nested:
            continue
        top_blocks.append(b)
        seen_ids.add(id(b))

    items = []
    detected_format = "qa"

    for i, block in enumerate(top_blocks, 1):
        sub_containers = block.select("div.mt-4.space-y-2 > div")

        if sub_containers:
            # ---------- CQ FORMAT ----------
            detected_format = "cq"
            stem_div = block.select_one("div.m-1") or block.select_one(
                "div.flex.flex-row.items-center.justify-between"
            )
            stem_text = _clean_node_text(stem_div)
            stem_text = re.sub(r"^\d+[\.\)](?!\d)\s*", "", stem_text)  # leading "1." strip
            stem_images = _extract_images(stem_div)
            tag = _get_tag(block)

            subs = []
            for sc in sub_containers:
                label_span = sc.select_one("span")
                label = label_span.get_text(strip=True).rstrip(".।") if label_span else ""
                q_container = sc.select_one("div.LatexRenderer-module__qDybqa__card")
                q_text = _clean_node_text(q_container)
                q_images = _extract_images(q_container)
                section = sc.select_one("section")
                a_text = _get_solution_text(section)
                a_images = _get_solution_images(section)
                if not q_text and not a_text:
                    continue
                subs.append({
                    "label": label or "",
                    "question": q_text,
                    "q_images": q_images,
                    "answer": a_text,
                    "a_images": a_images,
                })

            if subs:
                items.append({
                    "type": "cq",
                    "no": i,
                    "stem": stem_text,
                    "stem_images": stem_images,
                    "tag": tag,
                    "subs": subs,
                })
        else:
            # ---------- SHORT Q&A FORMAT ----------
            # v2: some pages put ONLY an image in the top-level question
            # card (e.g. "2." + <img>, no real question text), and the
            # actual MCQ text + options live in a nested sub-block
            # (div.px-4.pt-4.pb-6.border.rounded-xl inside div.space-y-6)
            # instead of directly in this block. If this block has no
            # direct option buttons of its own, look for that nested
            # sub-block and pull the real question+options+explanation
            # from there -- otherwise the image-only stem and its actual
            # question text/options were being silently dropped.
            direct_options = block.select(":scope > div.grid.grid-cols-1 > button")
            nested_subblock = None
            if not direct_options:
                nested_subblock = block.select_one("div.space-y-6 > div.px-4.pt-4.pb-6.border.rounded-xl")

            stem_container = block.select_one("div.LatexRenderer-module__qDybqa__card")
            stem_text = _clean_node_text(stem_container)
            stem_text = re.sub(r"^\d+[\.\)](?!\d)\s*", "", stem_text)  # leading "1." strip
            stem_images = _extract_images(stem_container)
            tag = _get_tag(block)

            if nested_subblock is not None:
                sub_q_container = nested_subblock.select_one("div.LatexRenderer-module__qDybqa__card")
                sub_q_text = _clean_node_text(sub_q_container)
                sub_q_images = _extract_images(sub_q_container)
                # combine stem (image-bearing) text with the real
                # question text found in the nested sub-block, so neither
                # gets lost
                q_text = (stem_text + " " + sub_q_text).strip() if stem_text else sub_q_text
                q_images = stem_images + [u for u in sub_q_images if u not in stem_images]
                section = nested_subblock.select_one("section")
                a_text = _get_solution_text(section)
                a_images = _get_solution_images(section)
            else:
                q_text = stem_text
                q_images = stem_images
                section = block.select_one("section")
                a_text = _get_solution_text(section)
                a_images = _get_solution_images(section)

            if not q_text and not a_text:
                continue

            items.append({
                "type": "qa",
                "no": i,
                "question": q_text,
                "q_images": q_images,
                "tag": tag,
                "answer": a_text,
                "a_images": a_images,
            })

    return {
        "page_title": page_title,
        "format": detected_format,
        "items": items,
    }
