#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - Print Style-01 Handler (3 Formats from PDFExporter)"""

import re

# ============================================================
# PRINT STYLE-01 FORMAT NAMES
# ============================================================
PRINT_FORMAT_NAMES = {
    'print_p1': '🖨️ Study Material (প্রশ্ন + উত্তর + ব্যাখ্যা)',
    'print_p2': '🖨️ Exam Style (প্রশ্ন + Answer Table)',
    'print_p3': '🖨️ Compact Exam (Horizontal Answer Key)',
}

# ============================================================
# PRINT STYLE-01 CSS
# ============================================================
PRINT_CSS = """<style>
@page{size:A4 portrait;margin:10mm 10mm;@top-center{content:none}@bottom-center{content:none}}
body{font-family:'Noto Sans Bengali','SolaimanLipi',Arial,sans-serif;font-size:12pt;line-height:1.2;color:#000;margin:0;padding:10px;width:210mm;max-width:210mm}
.exam-header{text-align:center;border:2px solid #4169E1;background-color:#F0F8FF;border-radius:6px;padding:10px;margin-bottom:15px}
.exam-header h1{color:#191970;margin:0;font-size:15pt;font-weight:bold}
.content-columns{column-count:2;column-gap:15px;column-fill:balance;column-rule:1px solid #ddd}
.question{margin-bottom:7px;break-inside:avoid;page-break-inside:avoid}
.question-header{margin-bottom:4px;display:flex;align-items:flex-start}
.question-num{font-family:'Times New Roman',serif;font-weight:bold;color:#1E64B7;font-size:12pt;margin-right:5px;white-space:nowrap;flex-shrink:0}
.question-text{flex:1;line-height:1.4;font-size:13pt;color:#000;word-wrap:break-word}
.options-table-short{width:100%;border-collapse:collapse;margin:4px 0 4px 8px;table-layout:fixed}
.options-table-short td{border:none;padding:2px 8px 2px 0;vertical-align:top;font-size:13pt;color:#000;width:40%}
.options-table-short td.answer-col{display:flex;justify-content:center;align-items:center;vertical-align:middle;font-family:'Poppins',sans-serif;font-weight:600;font-size:12pt;color:#000;padding-left:10px}
.answer-circle{font-weight:300;font-family:'Poppins',sans-serif;font-size:12pt;line-height:1}
.options-list{margin:4px 0 4px 8px;padding:0;list-style:none}
.options-list li{margin:1px 0;font-size:13pt;color:#000;word-wrap:break-word}
.option-with-answer{display:flex;justify-content:space-between;align-items:flex-start}
.explanation{margin:4px 0 2px 8px;padding:4px;color:#000;background-color:rgba(66,153,225,0.1);border-left:3px solid #4299e1;font-size:12pt;font-style:italic;break-inside:avoid}
.explanation-label{font-weight:bold;color:#2c5282}
.page-break{page-break-before:always;break-before:page}
.answers-section{column-count:1;margin-top:0}
.answer-table{width:100%;border-collapse:collapse;margin-top:0;border:1px solid #333}
.answer-table th,.answer-table td{border:1px solid #333;padding:6px;text-align:left;vertical-align:top;word-wrap:break-word}
.answer-table th{background-color:#f5f5f5;font-weight:bold;text-align:center;font-size:13pt}
.qno-col{width:8%;text-align:center}.ans-col{width:8%;text-align:center;font-weight:bold;font-size:14pt}.exp-col{width:84%;font-size:12pt}
.answer-key-section{margin-top:20px;page-break-inside:avoid}
.answer-key-header{text-align:center;font-weight:bold;font-size:13pt;margin-bottom:10px;color:#000}
.answer-key-table{width:100%;border-collapse:collapse;border:1px solid #333;margin:0 auto}
.answer-key-table th,.answer-key-table td{border:1px solid #333;padding:6px;text-align:center;font-size:11pt}
.answer-key-table th{background-color:#f5f5f5;font-weight:bold}
img{max-width:35%!important;height:auto!important;vertical-align:middle}
@media print{@page{size:A4 portrait;margin:10mm 10mm;@top-center{content:none}@bottom-center{content:none}}body{-webkit-print-color-adjust:exact;color-adjust:exact;width:210mm;max-width:210mm}.question{break-inside:avoid;page-break-inside:avoid}.explanation{break-inside:avoid;page-break-inside:avoid}.content-columns{column-rule:1px solid #ddd}}
</style>"""

# ============================================================
# HELPER FUNCTION
# ============================================================
def check_short_option(opts):
    """Check if options are short (<=16 chars)"""
    for v in opts:
        if v:
            clean = re.sub(r'<[^>]+>', '', str(v)).strip()
            if len(clean) > 16:
                return False
    return True

# ============================================================
# FORMAT P1: Study Material
# ============================================================
def build_print_p1(data, heading):
    """Questions + Answers + Explanations inline"""
    css = PRINT_CSS
    body = f'<div class="exam-header"><h1>{heading} - Practice Sheet</h1></div><div class="content-columns">'
    for d in data:
        is_short = check_short_option(d["opts"])
        body += f'<div class="question"><div class="question-header"><span class="question-num">{d["n"]:02d}.</span><div class="question-text">{d["q"]}{d["qi"]}</div></div>'
        ans_circle = f'[{[chr(97+d["ai"])] if d["ai"]>=0 else "?"}]'
        if is_short:
            body += f'<table class="options-table-short"><tr><td class="option-col">(A) {d["opts"][0]}{d["oimgs"][0]}</td><td class="option-col">(B) {d["opts"][1]}{d["oimgs"][1]}</td><td rowspan="2" class="answer-col"><span class="answer-circle">{ans_circle}</span></td></tr><tr><td class="option-col">(C) {d["opts"][2]}{d["oimgs"][2]}</td><td class="option-col">(D) {d["opts"][3]}{d["oimgs"][3]}</td></tr></table>'
        else:
            body += f'<ul class="options-list"><li>(A) {d["opts"][0]}{d["oimgs"][0]}</li><li>(B) {d["opts"][1]}{d["oimgs"][1]}</li><li>(C) {d["opts"][2]}{d["oimgs"][2]}</li><li class="option-with-answer"><span>(D) {d["opts"][3]}{d["oimgs"][3]}</span><span class="answer-circle">{ans_circle}</span></li></ul>'
        if d['exp']:
            body += f'<div class="explanation"><span class="explanation-label">ব্যাখ্যা:</span> {d["exp"]}{d["ei"]}</div>'
        body += '</div>'
    body += '</div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{css}</head><body>{body}</body></html>'

# ============================================================
# FORMAT P2: Exam Style (Questions + Answer Table)
# ============================================================
def build_print_p2(data, heading):
    """Page 1: Questions only, Page 2: Answer Table"""
    css = PRINT_CSS
    body = f'<div class="exam-header"><h1>{heading} - Questions</h1></div><div class="content-columns">'
    for d in data:
        is_short = check_short_option(d["opts"])
        body += f'<div class="question"><div class="question-header"><span class="question-num">{d["n"]:02d}.</span><div class="question-text">{d["q"]}{d["qi"]}</div></div>'
        if is_short:
            body += f'<table class="options-table-short"><tr><td>(A) {d["opts"][0]}{d["oimgs"][0]}</td><td>(B) {d["opts"][1]}{d["oimgs"][1]}</td></tr><tr><td>(C) {d["opts"][2]}{d["oimgs"][2]}</td><td>(D) {d["opts"][3]}{d["oimgs"][3]}</td></tr></table>'
        else:
            body += f'<ul class="options-list"><li>(A) {d["opts"][0]}{d["oimgs"][0]}</li><li>(B) {d["opts"][1]}{d["oimgs"][1]}</li><li>(C) {d["opts"][2]}{d["oimgs"][2]}</li><li>(D) {d["opts"][3]}{d["oimgs"][3]}</li></ul>'
        body += '</div>'
    body += '</div><div class="page-break"></div><div class="answers-section"><table class="answer-table"><thead><tr><th class="qno-col">Q.No.</th><th class="ans-col">Ans</th><th class="exp-col">Explanation</th></tr></thead><tbody>'
    for d in data:
        body += f'<tr><td class="qno-col">{d["n"]}</td><td class="ans-col">{d["al"]}</td><td class="exp-col">{d["exp"] if d["exp"] else "-"}</td></tr>'
    body += '</tbody></table></div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{css}</head><body>{body}</body></html>'

# ============================================================
# FORMAT P3: Compact Exam (Horizontal Answer Key)
# ============================================================
def build_print_p3(data, heading):
    """Questions 2-col + Horizontal Answer Key at bottom"""
    css = PRINT_CSS
    body = f'<div class="exam-header"><h1>{heading}</h1></div><div class="content-columns">'
    for d in data:
        is_short = check_short_option(d["opts"])
        body += f'<div class="question"><div class="question-header"><span class="question-num">{d["n"]}.</span><div class="question-text">{d["q"]}{d["qi"]}</div></div>'
        if is_short:
            body += f'<table class="options-table-short"><tr><td>(a) {d["opts"][0]}{d["oimgs"][0]}</td><td>(b) {d["opts"][1]}{d["oimgs"][1]}</td></tr><tr><td>(c) {d["opts"][2]}{d["oimgs"][2]}</td><td>(d) {d["opts"][3]}{d["oimgs"][3]}</td></tr></table>'
        else:
            body += f'<ul class="options-list"><li>(a) {d["opts"][0]}{d["oimgs"][0]}</li><li>(b) {d["opts"][1]}{d["oimgs"][1]}</li><li>(c) {d["opts"][2]}{d["oimgs"][2]}</li><li>(d) {d["opts"][3]}{d["oimgs"][3]}</li></ul>'
        body += '</div>'
    body += '</div><div class="answer-key-section"><div class="answer-key-header">সঠিক উত্তর যাচাই কর :)</div><table class="answer-key-table"><thead><tr><th class="qno-cell">প্রশ্ন</th>'
    for d in data:
        body += f'<th class="qno-cell">{d["n"]}</th>'
    body += '</tr></thead><tbody><tr><th class="ans-cell">উত্তর</th>'
    for d in data:
        body += f'<td class="ans-cell">{d["al"]}</td>'
    body += '</tr></tbody></table></div>'
    return f'<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8">{css}</head><body>{body}</body></html>'

# ============================================================
# BUILDERS DICT
# ============================================================
PRINT_BUILDERS = {
    'print_p1': build_print_p1,
    'print_p2': build_print_p2,
    'print_p3': build_print_p3,
}
