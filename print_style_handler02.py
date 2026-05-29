#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - Print Style-02 Handler (3 Formats)"""

import re

# ============================================================
# FORMAT NAMES
# ============================================================
PRINT2_FORMAT_NAMES = {
    'print2_p1': '🖨️ Practice Style (প্রশ্ন + Answer Table)',
    'print2_p2': '🖨️ Preparation Style (উত্তর + ব্যাখ্যা inline)',
    'print2_p3': '🖨️ Preparation Style-02 (English + Bengali)',
}

# ============================================================
# COMMON HEADER CSS (P1 & P2)
# ============================================================
HEADER_CSS = """
.exam-header{width:100%;border-radius:8px;background:#1B4332;padding:12px 16px;margin-bottom:14px;text-align:center}
.exam-header h1{color:#fff;font-size:26pt;font-weight:bold;margin:0}
.exam-header p{color:#fff;font-size:11pt;margin:4px 0 0 0}
"""

# ============================================================
# P3 HEADER CSS (Pink/Maroon Theme)
# ============================================================
P3_HEADER_CSS = """
.exam-header-p3{width:100%;border-radius:8px;background:linear-gradient(135deg, #F4E6E5 0%, #F0D8D6 50%, #E8CCCA 100%);border:2px solid #FBBAB8;padding:12px 16px;margin-bottom:14px;text-align:center}
.exam-header-p3 h1{color:#6E0607;font-size:26pt;font-weight:bold;margin:0}
.exam-header-p3 p{color:#6E0607;font-size:11pt;margin:4px 0 0 0}
"""

# ============================================================
# COMMON CSS (P1 & P2)
# ============================================================
COMMON_CSS = """<style>
@page{size:A4 portrait;margin:10mm 10mm}
body{font-family:'Noto Sans Bengali','SolaimanLipi',Arial,sans-serif;font-size:12pt;line-height:1.4;color:#000;margin:0;padding:0}
""" + HEADER_CSS + """
.footer-pg{text-align:center;font-size:9pt;color:#999;margin-top:10px;padding:5px}
.content-columns{column-count:2;column-gap:14px;column-rule:1px solid #e5e7eb;column-fill:balance}
.question{break-inside:avoid;page-break-inside:avoid;margin-bottom:10px}
.question-num{font-family:'Times New Roman',serif;font-weight:bold;font-size:12pt;color:#000}
.question-text{font-size:12pt;line-height:1.4}
.options-table-short{width:100%;table-layout:fixed;border-collapse:collapse;margin:4px 0}
.options-table-short td{border:none;padding:2px 4px;font-size:12pt;vertical-align:top}
.opt-col{width:43%}
.answer-col{width:14%;text-align:center;vertical-align:middle!important;font-size:13pt;font-weight:600}
.options-list{list-style:none;padding:0;margin:4px 0 4px 8px}
.options-list li{font-size:12pt;margin:2px 0}
.option-flex{display:flex;justify-content:space-between;align-items:flex-start}
.explanation-box{background:#D1FAE5;border-radius:6px;padding:6px 10px;margin-top:4px;break-inside:avoid}
.explanation-label{font-weight:bold;color:#065F46;font-size:12pt}
.explanation-text{font-size:11pt;color:#000}
.answer-table{width:100%;border-collapse:collapse;border:1px solid #333;margin-top:10px}
.answer-table th{background:#1B4332;color:#fff;font-weight:bold;font-size:13pt;padding:6px;border:1px solid #333}
.answer-table td{border:1px solid #333;padding:6px;font-size:12pt}
.qno-col{width:8%;text-align:center}
.ans-col{width:8%;text-align:center;font-weight:bold;font-size:14pt}
.exp-col{width:84%}
.page-break{page-break-before:always}
.bengali-hint{font-style:italic;font-size:10pt;color:#555;margin-top:2px}
img{max-width:100%!important;height:auto!important;display:inline-block;vertical-align:middle}
.q-img{max-height:80px;max-width:60%!important;margin:0 2px}
.opt-img{max-height:60px;max-width:80%!important}
.exp-img{max-height:60px;max-width:80%!important}
@media print{body{-webkit-print-color-adjust:exact;color-adjust:exact}.question{break-inside:avoid}.answer-table thead{display:table-header-group}}
</style>"""

# ============================================================
# P3 CSS (Pink/Maroon Theme)
# ============================================================
P3_CSS = """<style>
@page{size:A4 portrait;margin:10mm 10mm}
body{font-family:'Noto Sans Bengali','SolaimanLipi',Arial,sans-serif;font-size:12pt;line-height:1.4;color:#1A1A1A;margin:0;padding:0}
""" + P3_HEADER_CSS + """
.footer-pg{text-align:center;font-size:9pt;color:#999;margin-top:10px;padding:5px}
.content-columns{column-count:2;column-gap:14px;column-rule:1px solid #e5e7eb;column-fill:balance}
.question{break-inside:avoid;page-break-inside:avoid;margin-bottom:10px}
.question-num{font-family:'Times New Roman',serif;font-weight:bold;font-size:12pt;color:#6E0607}
.question-text{font-size:12pt;line-height:1.4;color:#1A1A1A}
.options-table-short{width:100%;table-layout:fixed;border-collapse:collapse;margin:4px 0}
.options-table-short td{border:none;padding:2px 4px;font-size:12pt;vertical-align:top;color:#1A1A1A}
.opt-col{width:43%}
.answer-col{width:14%;text-align:center;vertical-align:middle!important;font-size:13pt;font-weight:600;color:#555555}
.options-list{list-style:none;padding:0;margin:4px 0 4px 8px}
.options-list li{font-size:12pt;margin:2px 0;color:#1A1A1A}
.option-flex{display:flex;justify-content:space-between;align-items:flex-start}
.explanation-box-p3{background:#F0E0E0;border-radius:6px;padding:6px 10px;margin-top:4px;break-inside:avoid}
.explanation-label-p3{font-weight:bold;color:#6E090F;font-size:12pt}
.explanation-text-p3{font-size:11pt;color:#1A1A1A}
.bengali-hint{font-style:italic;font-size:10pt;color:#555;margin-top:2px}
img{max-width:100%!important;height:auto!important;display:inline-block;vertical-align:middle}
.q-img{max-height:80px;max-width:60%!important;margin:0 2px}
.opt-img{max-height:60px;max-width:80%!important}
.exp-img{max-height:60px;max-width:80%!important}
@media print{body{-webkit-print-color-adjust:exact;color-adjust:exact}.question{break-inside:avoid}}
</style>"""

FOOTER_TEXT = 'সেরা গাইডলাইনে গোছানো প্রস্তুতি-এটলাস'

# ============================================================
# HELPER
# ============================================================
def check_short_option(opts):
    for v in opts:
        if v:
            clean = re.sub(r'<[^>]+>', '', str(v)).strip()
            if len(clean) > 16:
                return False
    return True

def wrap_img(tag):
    if not tag:
        return ''
    return re.sub(r'<img\s', '<img class="opt-img" ', str(tag))

def wrap_q_img(tag):
    if not tag:
        return ''
    return re.sub(r'<img\s', '<img class="q-img" ', str(tag))

def wrap_exp_img(tag):
    if not tag:
        return ''
    return re.sub(r'<img\s', '<img class="exp-img" ', str(tag))

# ============================================================
# FORMAT P1: Practice Style (No answer inline, Answer Table at end)
# ============================================================
def build_print2_p1(data, heading):
    body = (
        f'<div class="exam-header"><h1>{heading}</h1>'
        f'<p>প্রশ্নপত্র – উত্তর ও ব্যাখ্যা শেষ পৃষ্ঠায়</p></div>'
        f'<div class="content-columns">'
    )
    for d in data:
        is_short = check_short_option(d['opts'])
        qi  = wrap_q_img(d.get('qi', ''))
        oi  = [wrap_img(x) for x in d.get('oimgs', ['','','',''])]
        body += (
            f'<div class="question">'
            f'<span class="question-num">{d["n"]}.</span> '
            f'<span class="question-text">{d["q"]}{qi}</span>'
        )
        if is_short:
            body += (
                f'<table class="options-table-short">'
                f'<tr>'
                f'<td class="opt-col">ⓐ {d["opts"][0]}{oi[0]}</td>'
                f'<td class="opt-col">ⓑ {d["opts"][1]}{oi[1]}</td>'
                f'</tr><tr>'
                f'<td class="opt-col">ⓒ {d["opts"][2]}{oi[2]}</td>'
                f'<td class="opt-col">ⓓ {d["opts"][3]}{oi[3]}</td>'
                f'</tr></table>'
            )
        else:
            body += (
                f'<ul class="options-list">'
                f'<li>ⓐ {d["opts"][0]}{oi[0]}</li>'
                f'<li>ⓑ {d["opts"][1]}{oi[1]}</li>'
                f'<li>ⓒ {d["opts"][2]}{oi[2]}</li>'
                f'<li>ⓓ {d["opts"][3]}{oi[3]}</li>'
                f'</ul>'
            )
        body += '</div>'
    body += '</div><div class="page-break"></div>'

    # Answer Table
    body += (
        f'<div class="exam-header"><h1>{heading} - উত্তর ও ব্যাখ্যা</h1></div>'
        f'<table class="answer-table">'
        f'<thead><tr>'
        f'<th class="qno-col">প্রশ্ন নং</th>'
        f'<th class="ans-col">উত্তর</th>'
        f'<th class="exp-col">ব্যাখ্যা</th>'
        f'</tr></thead><tbody>'
    )
    for d in data:
        ei  = wrap_exp_img(d.get('ei', ''))
        exp = f'{d["exp"]}{ei}' if d.get('exp') else 'no explanation can be made'
        body += (
            f'<tr>'
            f'<td class="qno-col">{d["n"]}</td>'
            f'<td class="ans-col">{d["al"]}</td>'
            f'<td class="exp-col">{exp}</td>'
            f'</tr>'
        )
    body += f'</tbody></table><div class="footer-pg">{FOOTER_TEXT}</div>'

    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{COMMON_CSS}</head><body>{body}</body></html>'


# ============================================================
# FORMAT P2: Preparation Style (Answer + Explanation inline)
# ============================================================
def build_print2_p2(data, heading):
    body = (
        f'<div class="exam-header"><h1>{heading}</h1>'
        f'<p>সকল প্রশ্নের সাথে উত্তর ও ব্যাখ্যা সম্বলিত সলভ শিট</p></div>'
        f'<div class="content-columns">'
    )
    circles = ['ⓐ', 'ⓑ', 'ⓒ', 'ⓓ']
    for d in data:
        is_short  = check_short_option(d['opts'])
        ans_circle = circles[d['ai']] if 0 <= d.get('ai', -1) <= 3 else '?'
        qi  = wrap_q_img(d.get('qi', ''))
        oi  = [wrap_img(x) for x in d.get('oimgs', ['','','',''])]
        ei  = wrap_exp_img(d.get('ei', ''))
        body += (
            f'<div class="question">'
            f'<span class="question-num">{d["n"]}.</span> '
            f'<span class="question-text">{d["q"]}{qi}</span>'
        )
        if is_short:
            body += (
                f'<table class="options-table-short">'
                f'<tr>'
                f'<td class="opt-col">(A) {d["opts"][0]}{oi[0]}</td>'
                f'<td class="opt-col">(B) {d["opts"][1]}{oi[1]}</td>'
                f'<td rowspan="2" class="answer-col">{ans_circle}</td>'
                f'</tr><tr>'
                f'<td class="opt-col">(C) {d["opts"][2]}{oi[2]}</td>'
                f'<td class="opt-col">(D) {d["opts"][3]}{oi[3]}</td>'
                f'</tr></table>'
            )
        else:
            body += (
                f'<ul class="options-list">'
                f'<li>(A) {d["opts"][0]}{oi[0]}</li>'
                f'<li>(B) {d["opts"][1]}{oi[1]}</li>'
                f'<li>(C) {d["opts"][2]}{oi[2]}</li>'
                f'<li class="option-flex">'
                f'<span>(D) {d["opts"][3]}{oi[3]}</span>'
                f'<span class="answer-col">{ans_circle}</span>'
                f'</li></ul>'
            )
        if d.get('exp'):
            body += (
                f'<div class="explanation-box">'
                f'<span class="explanation-label">ব্যাখ্যা:</span> '
                f'<span class="explanation-text">{d["exp"]}{ei}</span>'
                f'</div>'
            )
        body += '</div>'
    body += f'</div><div class="footer-pg">{FOOTER_TEXT}</div>'

    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{COMMON_CSS}</head><body>{body}</body></html>'


# ============================================================
# FORMAT P3: Preparation Style-02 (English + Bengali) — Pink/Maroon Theme
# ============================================================
def build_print2_p3(data, heading):
    body = (
        f'<div class="exam-header-p3"><h1>{heading}</h1>'
        f'<p>Practice Sheet By ATLAS</p></div>'
        f'<div class="content-columns">'
    )
    circles = ['ⓐ', 'ⓑ', 'ⓒ', 'ⓓ']
    for d in data:
        is_short   = check_short_option(d['opts'])
        ans_circle = circles[d['ai']] if 0 <= d.get('ai', -1) <= 3 else '?'
        qi   = wrap_q_img(d.get('qi', ''))
        oi   = [wrap_img(x) for x in d.get('oimgs', ['','','',''])]
        ei   = wrap_exp_img(d.get('ei', ''))
        hint = d.get('hint', '')
        body += (
            f'<div class="question">'
            f'<span class="question-num">{d["n"]:02d}.</span> '
            f'<span class="question-text">{d["q"]}{qi}</span>'
        )
        if hint:
            body += f'<div class="bengali-hint">{hint}</div>'
        if is_short:
            body += (
                f'<table class="options-table-short">'
                f'<tr>'
                f'<td class="opt-col">(A) {d["opts"][0]}{oi[0]}</td>'
                f'<td class="opt-col">(B) {d["opts"][1]}{oi[1]}</td>'
                f'<td rowspan="2" class="answer-col">{ans_circle}</td>'
                f'</tr><tr>'
                f'<td class="opt-col">(C) {d["opts"][2]}{oi[2]}</td>'
                f'<td class="opt-col">(D) {d["opts"][3]}{oi[3]}</td>'
                f'</tr></table>'
            )
        else:
            body += (
                f'<ul class="options-list">'
                f'<li>(A) {d["opts"][0]}{oi[0]}</li>'
                f'<li>(B) {d["opts"][1]}{oi[1]}</li>'
                f'<li>(C) {d["opts"][2]}{oi[2]}</li>'
                f'<li class="option-flex">'
                f'<span>(D) {d["opts"][3]}{oi[3]}</span>'
                f'<span class="answer-col">{ans_circle}</span>'
                f'</li></ul>'
            )
        if d.get('exp'):
            body += (
                f'<div class="explanation-box-p3">'
                f'<span class="explanation-label-p3">ব্যাখ্যা:</span> '
                f'<span class="explanation-text-p3">{d["exp"]}{ei}</span>'
                f'</div>'
            )
        body += '</div>'
    body += f'</div><div class="footer-pg">{FOOTER_TEXT}</div>'

    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{P3_CSS}</head><body>{body}</body></html>'


# ============================================================
# BUILDERS DICT
# ============================================================
PRINT2_BUILDERS = {
    'print2_p1': build_print2_p1,
    'print2_p2': build_print2_p2,
    'print2_p3': build_print2_p3,
}
