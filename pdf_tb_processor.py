"""
PDF → Trial Balance (Excel) Converter
======================================
Standalone module for the CA Toolkit. Reads a Trial Balance PDF (Tally / Busy /
Marg / other Indian accounting-software exports) and produces a clean, editable
.xlsx with Debit/Credit columns, group sections, group totals via SUM formulas,
and a Grand Total.

DESIGN GUARANTEES
-----------------
1. STANDALONE — does NOT import from processor.py or tb_processor.py, and is not
   imported by them. Adding, editing, or deleting this file leaves the year-shift
   tool and the TB-to-BS injection tool untouched.

2. NO MERGED CELLS in the output. Title text sits in column A only; column B/C
   for those rows is empty. This keeps the sheet fully editable (insert/delete
   row works everywhere).

3. INDIAN NUMBER FORMAT preserved via the `#,##0.00;-#,##0.00;-` display
   format. Values are stored as real numbers, not strings.

4. FORMULAS not hard-coded totals. Each "Total :" row is a `=SUM(range)` and the
   Grand Total is a `=SUM(...)` across all group totals, so any user edit in the
   detail rows flows through automatically.

5. TWO FORMAT FAMILIES supported out of the box:
   A) GROUP-WISE with "Total :" line under each group (Busy / Marg / most GST
      software). Sub-groups render as "PARENT ------ ( CHILD )".
   B) TALLY hierarchical, where each group carries its subtotal on the same
      row and child accounts are shown indented below. Indentation is
      recovered from the pdf-native x-coordinate of the first word on each line.

6. UNIVERSAL FALLBACK — if neither Family A nor Family B is confidently
   detected, the extractor falls back to a flat "one row per account" layout
   using column boundaries derived from the "Debit"/"Credit" header.

v1.0  2026-09-23  initial release
"""

from __future__ import annotations

import re
import io
import os
from typing import List, Dict, Tuple, Optional, Any
from collections import defaultdict

# ─── Third-party (already pinned in requirements.txt for existing tools) ────
import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

# Indian rupee number format (matches the target Guru Kirpa xlsx exactly)
_INR_FMT = '#,##0.00;-#,##0.00;-'

# Y-tolerance for grouping words into the same visual line
_Y_TOL = 2.5

# X-tolerance for the "same column" test (points)
_X_TOL = 8.0

# Patterns to strip page headers/footers regardless of format family
_JUNK_PATTERNS = [
    re.compile(r'^\s*Continue to next page', re.I),
    re.compile(r'^\s*Continued\s+On\s+Page\b', re.I),   # Super Scales / other exports
    re.compile(r'^\s*continued\s*\.{2,}\s*$', re.I),
    re.compile(r'^\s*Page\s*:?\s*\d+\s*(/\s*\d+)?\s*$', re.I),
    re.compile(r'^\s*Page\s+\d+\s*$', re.I),
    re.compile(r'\bPage\s+No\s*\.?\s*\d', re.I),         # "Page No. 1" without colon
    re.compile(r'^\s*C\s*a\s*r\s*r\s*i\s*e\s*d\s+O\s*v\s*e\s*r\b', re.I),
    re.compile(r'^\s*B\s*r\s*o\s*u\s*g\s*h\s*t\s+F\s*o\s*r\s*w\s*a\s*r\s*d\b', re.I),
    # Column-header rows come in many shapes across accounting-software exports:
    #   "Particulars Debit Credit"
    #   "P a r t i c u l a r s   Station"     (spaced letter-glyphs, right-side
    #                                          "Station" hint from some Busy exports)
    #   "P a r t i c u l a r s"               (letter-spaced Particulars alone)
    # We match the letter-spaced form ANYWHERE in the line so any trailing
    # word like "Station" or "Debit" or "Credit" doesn't defeat the filter.
    re.compile(r'\bP\s+a\s+r\s+t\s+i\s+c\s+u\s+l\s+a\s+r\s+s\b', re.I),
    re.compile(r'^\s*Particulars\b', re.I),
    re.compile(r'^\s*Debit\s*$', re.I),
    re.compile(r'^\s*Credit\s*$', re.I),
    re.compile(r'^\s*Debit\s+Credit\s*$', re.I),
    re.compile(r'^\s*Closing\s+Balance\b', re.I),
    re.compile(r'^\s*Opening\s+Balance\b', re.I),
    re.compile(r'^\s*Station\s*$', re.I),
    # Report-title bands that repeat on every page.  These lines carry the
    # page number ("Page: 1 / 7") so the "no numbers" heuristic in
    # _looks_like_page_top_matter cannot catch them — match them by text.
    re.compile(r'\b(?:Group\s+Wise\s+)?Trial\s+Balance\b', re.I),
    re.compile(r'\bPage\s*:\s*\d', re.I),
    re.compile(r'\bAs\s+at\b', re.I),
    re.compile(r'\bAS\s+On\b', re.I),
]

# The "Grand Total" marker in various forms.  Tally spaces the glyphs
# ("G r a n d  T o t a l"), Busy/Marg print it as "Grand Total :".  Trailing
# numeric values are allowed because on many PDFs the row's balance columns
# sit on the same visual line as the label.
_GRAND_TOTAL_RX = re.compile(
    r'^\s*(G\s*r\s*a\s*n\s*d\s+T\s*o\s*t\s*a\s*l|GRAND\s+TOTAL|Grand\s+Total)'
    r'\s*:?\s*(?:[\d,.\-()\s]*)?$',
    re.I
)

# The per-group "Total :" marker. In many PDFs the row shows
#   "Total :   10,127.00   3,69,980.00"
# so the same visual line carries the totals. We accept anything numeric-and-
# separators after the colon.
_TOTAL_RX = re.compile(r'^\s*Total\s*:\s*(?:[\d,.\-()\s]*)?$', re.I)

# Format-A sub-group separator: "PARENT ------ ( CHILD )"
_SUBGROUP_RX = re.compile(r'^(.+?)\s*-{3,}\s*\(\s*(.+?)\s*\)\s*$')

# Amount token: Indian or Western comma grouping, optional decimal, optional sign
_AMOUNT_RX = re.compile(r'^-?\d[\d,]*(?:\.\d+)?$|^\(-?\d[\d,]*(?:\.\d+)?\)$')

# Numeric-only token (used when we must decide "is this word a value?")
_NUM_TEST_RX = re.compile(r'^\(?-?[\d,]+(?:\.\d+)?\)?$')

# ── Format C (DOSPrinter / columnar-ASCII-table) detection ────────────────
# These PDFs are produced by old DOS-era accounting software (e.g. DOSPrinter)
# and render as a character-art table with "!" column separators and "=====/-----"
# row dividers.  pdfplumber emits the separators as single long word-tokens.
_DOSPRINTER_DIVIDER_RX = re.compile(
    r'^[!=\-|]{10,}$'   # long runs of !  = or - characters
)
# "GROUP TOTAL OF …" or "TOTAL OF …" lines in the DOSPrinter format
_C_GROUP_TOTAL_RX = re.compile(
    r'^\s*(?:GROUP\s+)?TOTAL\s+OF\b', re.I
)
# Grand total marker in DOSPrinter: "T O T A L" (letter-spaced) or "TOTAL"
_C_GRAND_TOTAL_RX = re.compile(
    r'^\s*(?:T\s+O\s+T\s+A\s+L|TOTAL)\s*$', re.I
)


# ═══════════════════════════════════════════════════════════════════════════
# WORD / LINE UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def _lines_from_words(words: List[dict]) -> List[List[dict]]:
    """
    Cluster pdfplumber words into visual lines by 'top' coordinate.
    Words within _Y_TOL points are considered same line. Lines returned in
    top-to-bottom order; each line's words are left-to-right.
    """
    if not words:
        return []
    ws = sorted(words, key=lambda w: (w['top'], w['x0']))
    lines: List[List[dict]] = []
    cur: List[dict] = [ws[0]]
    cur_top = ws[0]['top']
    for w in ws[1:]:
        if abs(w['top'] - cur_top) <= _Y_TOL:
            cur.append(w)
        else:
            cur.sort(key=lambda x: x['x0'])
            lines.append(cur)
            cur = [w]
            cur_top = w['top']
    cur.sort(key=lambda x: x['x0'])
    lines.append(cur)
    return lines


def _line_text(line: List[dict]) -> str:
    """Concatenate words of a line into a single string, single-spaced."""
    return ' '.join(w['text'] for w in line)


def _is_amount(text: str) -> bool:
    """True if the text is purely a number (Indian or Western comma format)."""
    t = text.strip()
    if not t:
        return False
    # Reject numbers with 10+ digits and NO commas — these are account numbers,
    # phone numbers, or IDs, not rupee amounts.
    digits_only = t.replace('-', '').replace('(', '').replace(')', '').replace('.', '')
    if len(digits_only) >= 10 and ',' not in t:
        return False
    t = t.replace(',', '')
    if t.startswith('(') and t.endswith(')'):
        t = t[1:-1]
    if t.startswith('-'):
        t = t[1:]
    return bool(t) and t.replace('.', '', 1).isdigit()


def _parse_amount(text: str) -> Optional[float]:
    """
    Parse '1,17,16,097.50' or '(1,234.50)' → float.
    Returns None on failure. Parentheses mean negative (accounting style).
    """
    t = text.strip()
    if not t:
        return None
    neg = False
    if t.startswith('(') and t.endswith(')'):
        neg = True
        t = t[1:-1]
    t = t.replace(',', '').replace(' ', '')
    if t.startswith('-'):
        neg = True
        t = t[1:]
    try:
        v = float(t)
        return -v if neg else v
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# COLUMN BOUNDARY DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def _find_column_boundaries(all_lines: List[List[dict]]) -> Optional[Tuple[float, float]]:
    """
    Locate the 'Debit' and 'Credit' header words anywhere in the doc.
    Returns (debit_right_x1, credit_right_x1) — the right edges we use to
    classify subsequent value words into columns.

    Some exports write the header as 'Debit Amount' / 'Credit Amount' rather
    than plain 'Debit' / 'Credit'; we detect BOTH forms by walking the
    header line and taking:
      • debit_right  = x1 of the last word between "Debit" and "Credit"
                       (that's "Debit" itself, or "Amount" after it, etc.)
      • credit_right = x1 of the last word on the line
                       (that's "Credit" itself, or "Amount" after it, etc.)

    We take the FIRST such header we find; every page in a TB export uses
    the same column geometry, so one is enough.
    """
    for line in all_lines:
        debit_idx = None
        credit_idx = None
        for i, w in enumerate(line):
            t = w['text'].strip().lower()
            if t == 'debit' and debit_idx is None:
                debit_idx = i
            elif t == 'credit' and credit_idx is None:
                credit_idx = i
        if debit_idx is None or credit_idx is None or credit_idx <= debit_idx:
            continue
        # last word in the debit column = word immediately before Credit
        debit_right = line[credit_idx - 1]['x1']
        # last word in the credit column = last word on the line
        credit_right = line[-1]['x1']
        if credit_right > debit_right:
            return (debit_right, credit_right)
    return None


def _classify_by_column(line: List[dict],
                         debit_right: float,
                         credit_right: float) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Split a line's words into (particulars_text, debit_value, credit_value).

    Value words are those whose text is purely numeric. The rest is
    particulars. A value is assigned to the Debit column iff its right edge
    x1 is within tolerance of the Debit column right edge (or clearly left of
    the Credit column right edge); otherwise Credit.
    """
    part_words: List[dict] = []
    debit_words: List[dict] = []
    credit_words: List[dict] = []

    debit_col_center = debit_right - 20  # heuristic centre
    credit_col_center = credit_right - 20

    for w in line:
        t = w['text']
        if _is_amount(t):
            # Decide column by right edge distance
            d_dist = abs(w['x1'] - debit_right)
            c_dist = abs(w['x1'] - credit_right)
            if d_dist <= c_dist and d_dist <= (credit_right - debit_right) * 0.7:
                debit_words.append(w)
            else:
                credit_words.append(w)
        else:
            part_words.append(w)

    part_text = ' '.join(w['text'] for w in part_words).strip()

    def _agg(words: List[dict]) -> Optional[float]:
        if not words:
            return None
        # A cell can have multiple word-tokens if pdfplumber split "1,17,16,097.50"
        # by whitespace. Concatenate right-to-left in x order and re-parse.
        words = sorted(words, key=lambda x: x['x0'])
        joined = ''.join(w['text'] for w in words)
        return _parse_amount(joined)

    return part_text, _agg(debit_words), _agg(credit_words)


# ═══════════════════════════════════════════════════════════════════════════
# LINE FILTERING (junk / header / footer)
# ═══════════════════════════════════════════════════════════════════════════

def _is_junk_line(text: str) -> bool:
    """True for page headers, footers, pagination markers, section labels."""
    if not text or not text.strip():
        return True
    for pat in _JUNK_PATTERNS:
        if pat.match(text):
            return True
    return False


def _looks_like_page_top_matter(line: List[dict], page_height: float) -> bool:
    """
    A line is 'top matter' (company letterhead / report title) if any of:

    * It sits in the top 10 % of the page AND starts centred (x0 >= 180).
      This catches the store code line (e.g. "211") which is a bare number
      but visually centred under the company name.
    * It sits in the top 15 % of the page AND has no numeric tokens AND
      starts centred.
    * It sits in the top 20 % of the page AND begins at a centred x0 (>= 180)
      AND contains one of the report-title keywords.

    Data lines always start at x0 ≈ 39-75 (never centred), so requiring a
    centred x0 is safe.
    """
    if not line:
        return True

    top = line[0]['top']
    x0 = line[0]['x0']

    # Top 10 % — the letterhead band. Anything centred here is skipped
    # regardless of what tokens it contains (store code, phone number, …).
    if top <= page_height * 0.10 and x0 >= 180:
        return True

    # Top 10 % — Tally-style continuation pages put the company name
    # left-aligned at x0 ≈ 39 instead of centred (page 2 onwards).  A line
    # in the very top band with NO numeric tokens is letterhead regardless
    # of x0; if there are numbers, the junk-pattern filter (Trial Balance,
    # Page:, Brought Forward, …) is expected to catch it.
    if top <= page_height * 0.10:
        has_num = any(_is_amount(w['text']) for w in line)
        if not has_num:
            return True

    # Top 15 % — company sub-lines (address, station) with no numbers.
    if top <= page_height * 0.15 and x0 >= 180:
        for w in line:
            if _is_amount(w['text']):
                # Might be a data line pretending to be centred — bail out.
                return False
        return True

    # Top 20 % — the report title band ("Trial Balance", "As On …",
    # "Closing Balance …", "Page: N / M").  Caught by the junk filter as
    # well, but we return early here so downstream code never has to look at
    # it.
    if top <= page_height * 0.20:
        txt = _line_text(line)
        if re.search(r'trial\s+balance|closing\s+balance|opening\s+balance|'
                      r'\bpage\s*:', txt, re.I):
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def _detect_format(all_lines: List[List[dict]]) -> str:
    """
    Return 'A' for group-wise (with EITHER "Total :" markers OR unlabeled
    bare-number subtotals like Marg / Super Scales exports), 'B' for Tally
    hierarchical, 'C' for DOSPrinter columnar ASCII-table, or 'flat' as a
    last-resort fallback.
    """
    total_hits = 0
    subgroup_hits = 0
    numeric_only_hits = 0
    divider_hits = 0       # long !===/ !--- lines from DOSPrinter

    left_x_seen: List[float] = []

    for line in all_lines:
        txt = _line_text(line).strip()
        if _TOTAL_RX.match(txt):
            total_hits += 1
        if _SUBGROUP_RX.match(txt):
            subgroup_hits += 1
        if line and line[0]['x0'] < 200:  # ignore letterhead
            left_x_seen.append(line[0]['x0'])
        # Unlabeled numeric-only line — Marg / Super Scales subtotal signal.
        # Must be non-empty and every word must be an amount.
        if line and all(_is_amount(w['text']) for w in line):
            numeric_only_hits += 1
        # DOSPrinter divider rows: pdfplumber emits "!======!===…" as one token
        if len(line) == 1 and _DOSPRINTER_DIVIDER_RX.match(txt):
            divider_hits += 1

    # Format C signal: many character-art divider rows (DOSPrinter / ASCII-table)
    if divider_hits >= 3:
        return 'C'

    # Group-wise signal: many "Total :" lines OR many unlabeled subtotals
    if total_hits >= 3 or numeric_only_hits >= 3:
        return 'A'

    # Format B signal: multiple distinct indent levels among data lines
    if left_x_seen:
        # Round to nearest 3pt bucket and count distinct
        buckets = set(round(x / 3) * 3 for x in left_x_seen)
        if len(buckets) >= 3:
            return 'B'

    return 'flat'


# ═══════════════════════════════════════════════════════════════════════════
# METADATA EXTRACTION (company name, address, report title)
# ═══════════════════════════════════════════════════════════════════════════

def _extract_metadata(all_pages_lines: List[List[List[dict]]]) -> Dict[str, str]:
    """
    Pull company name, address, and report title from the FIRST page's
    letterhead block. Fall back gracefully if any piece is missing.
    """
    meta = {
        'company': '',
        'address': '',
        'title': '',
        'subtitle': '',
    }
    if not all_pages_lines:
        return meta

    first = all_pages_lines[0]

    letterhead: List[str] = []
    report_lines: List[str] = []

    def _strip_page_marker(text: str) -> str:
        # Strip trailing page markers in every form Indian TB PDFs emit:
        #   "... Page: 1 / 7"      (Busy / Marg with colon)
        #   "... Page 2"           (Tally continuation)
        #   "... Page No. 1"       (Super Scales / other exports)
        return re.sub(
            r'\s*Page\s*(?:No\s*\.?)?\s*:?\s*\d+\s*(?:/\s*\d+)?\s*$',
            '', text).strip()

    for line in first:
        if not line:
            continue
        txt = _line_text(line).strip()
        if not txt:
            continue
        top = line[0]['top']
        x0 = line[0]['x0']
        has_num = any(_is_amount(w['text']) for w in line)

        # Classify what this line represents BEFORE deciding which bucket
        # it goes into.  A report-title / period line must always land in
        # report_lines even if it sits inside the letterhead y-band —
        # otherwise "TRIAL BALANCE" gets glued onto the address.
        # A column-caption line ("Particulars", "Debit Credit",
        # "Debit Amount Credit Amount") is dropped entirely.
        has_dr_and_cr = (bool(re.search(r'\bDebit\b', txt, re.I))
                         and bool(re.search(r'\bCredit\b', txt, re.I)))
        is_column_caption = bool(re.search(r'\bparticulars\b', txt, re.I)) \
                             or has_dr_and_cr
        is_report_title = bool(re.search(r'trial\s+balance|group\s+wise',
                                          txt, re.I))
        is_period_line = bool(re.search(
            r'closing\s+balance|opening\s+balance|\bas\s+on\b|\bas\s+at\b',
            txt, re.I))
        is_period_range = bool(re.match(
            r'^\s*\d{1,2}-\w{3,}-\d{2,4}\s+to\s+\d{1,2}-\w{3,}-\d{2,4}\s*$',
            txt, re.I))

        if is_column_caption:
            # "Particulars Debit Amount Credit Amount" is a table header,
            # not metadata — drop it completely.
            continue

        if is_report_title or is_period_line or is_period_range:
            if 0 < top < 250:
                report_lines.append(_strip_page_marker(txt))
            continue

        # Letterhead band (top ~15 % of page, centred).  Company name +
        # optional shop code + address (typically 3–4 lines).  We keep pure-
        # numeric lines (shop code like "211") as a prefix — the reference
        # Guru Kirpa sample includes them in the address.
        if top < 120 and x0 >= 180 and len(letterhead) < 4:
            letterhead.append(txt)
            continue

    if letterhead:
        meta['company'] = letterhead[0].strip()
        if len(letterhead) > 1:
            # Fold any short numeric-only line (shop code / building no.)
            # into the address prefix rather than keeping it on its own row.
            addr_parts: List[str] = []
            for piece in letterhead[1:]:
                # Strip trailing commas + whitespace from each source line so
                # our ", " join doesn't produce "Market,, Near".
                p = piece.strip().rstrip(',').strip()
                if not p:
                    continue
                if addr_parts and addr_parts[-1].replace(',', '').replace(
                        ' ', '').isdigit():
                    # Previous line was a bare number — glue this one to it.
                    addr_parts[-1] = addr_parts[-1] + ', ' + p
                else:
                    addr_parts.append(p)
            meta['address'] = ', '.join(addr_parts)
    if report_lines:
        # De-dup while preserving order
        seen = set()
        uniq: List[str] = []
        for r in report_lines:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        meta['title'] = uniq[0]
        if len(uniq) > 1:
            meta['subtitle'] = uniq[1]
    if not meta['title']:
        meta['title'] = 'Trial Balance'
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT A PARSER  — Group-wise with "Total :" lines (Busy / Marg / GST software)
# ═══════════════════════════════════════════════════════════════════════════

def _parse_format_a(all_pages_lines: List[List[List[dict]]],
                     debit_right: float,
                     credit_right: float,
                     page_heights: List[float]) -> List[Dict[str, Any]]:
    """
    Walk lines page-by-page, emit a flat list of section events:
      {'kind': 'group', 'name': str}
      {'kind': 'leaf', 'name': str, 'debit': float|None, 'credit': float|None}
      {'kind': 'grand_total'}  # marker; values recomputed on output

    Group headers introduce a section; the following leaves belong to it.
    "Total :" lines are consumed as section terminators (we regenerate them
    on output with formulas).
    """
    events: List[Dict[str, Any]] = []
    current_group_open = False

    # Track deferred merging: pdfplumber sometimes places "Total :" text at
    # y=168 and its numeric row at y=169. We merge lines that are ≤2pt apart
    # AND where the earlier line has no numbers and the later has only numbers.
    for page_idx, page_lines in enumerate(all_pages_lines):
        ph = page_heights[page_idx] if page_idx < len(page_heights) else 842
        merged = _merge_split_totals(page_lines)

        for line in merged:
            if not line:
                continue
            if _looks_like_page_top_matter(line, ph):
                continue

            txt = _line_text(line).strip()
            if _is_junk_line(txt):
                continue

            # Bottom-of-page footers (Continue to next page…)
            if line[-1]['bottom'] > ph * 0.94 and 'continue' in txt.lower():
                continue

            # Grand Total?
            if _GRAND_TOTAL_RX.match(txt.replace('Total :', 'Total').strip()) or \
               re.match(r'^\s*Grand\s+Total\s*:', txt, re.I):
                events.append({'kind': 'grand_total'})
                current_group_open = False
                continue

            # "Total :" for a group
            if _TOTAL_RX.match(txt):
                current_group_open = False
                continue

            # Line values
            name, debit, credit = _classify_by_column(line, debit_right, credit_right)

            # Unlabeled subtotal / grand total — Marg / Super Scales style.
            # Line has amount(s) at the right column edges but no text at
            # all in the particulars column.  Silently drop it; the group's
            # Total row (with a live SUM formula) and the Grand Total row
            # (sum of Totals) will be regenerated on output.
            if not name.strip() and (debit is not None or credit is not None):
                current_group_open = False
                continue

            # Group header? (no values, and not indented far right)
            has_values = (debit is not None) or (credit is not None)

            if not has_values:
                # Sub-group form?
                m = _SUBGROUP_RX.match(name)
                if m:
                    parent, child = m.group(1).strip(), m.group(2).strip()
                    events.append({'kind': 'group',
                                    'name': f'{parent} ------ ( {child} )'})
                else:
                    events.append({'kind': 'group', 'name': name})
                current_group_open = True
                continue

            # Leaf entry
            events.append({
                'kind': 'leaf',
                'name': name,
                'debit': debit,
                'credit': credit,
            })

    return events


def _merge_split_totals(page_lines: List[List[dict]]) -> List[List[dict]]:
    """
    Some PDFs emit 'Total :' text at y=168 and its numeric values at y=169.
    Merge such adjacent lines when the earlier line ends with 'Total :' and
    the next line is 1-3pt below and consists only of numeric words.
    """
    if not page_lines:
        return page_lines
    out: List[List[dict]] = []
    i = 0
    while i < len(page_lines):
        cur = page_lines[i]
        if i + 1 < len(page_lines):
            nxt = page_lines[i + 1]
            if cur and nxt:
                cur_txt = _line_text(cur).strip()
                nxt_all_num = all(_is_amount(w['text']) for w in nxt)
                y_gap = nxt[0]['top'] - cur[-1]['top']
                if (_TOTAL_RX.match(cur_txt)
                        and nxt_all_num
                        and 0 <= y_gap <= 4):
                    # Merge
                    merged = cur + nxt
                    merged.sort(key=lambda w: (w['top'], w['x0']))
                    out.append(merged)
                    i += 2
                    continue
        out.append(cur)
        i += 1
    return out


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT B PARSER  — Tally hierarchical
# ═══════════════════════════════════════════════════════════════════════════

def _parse_format_b(all_pages_lines: List[List[List[dict]]],
                     debit_right: float,
                     credit_right: float,
                     page_heights: List[float]) -> List[Dict[str, Any]]:
    """
    Tally-style: each row has an indent level determined by x0 of its first
    word; parent rows carry subtotals; child rows carry leaf values.

    Strategy:
      1. Walk lines, keep a stack of open groups keyed by x0-bucket.
      2. When we see a new line at x0 <= stack.top: pop until we're at the
         right parent level.
      3. If the line's next-neighbour indent is greater → this line is a
         group (push it and DON'T emit as a leaf).
      4. If the line's next-neighbour indent is same or less → this line is
         a leaf under its current parent; emit accordingly.

    Output is the same event stream as Format A: 'group', 'leaf',
    'grand_total'. Group titles use "PARENT ------ ( CHILD )" when nesting is
    present.
    """
    # 1) Flatten all data lines across pages (letterhead + junk stripped)
    flat: List[Tuple[List[dict], str, Optional[float], Optional[float]]] = []
    for page_idx, page_lines in enumerate(all_pages_lines):
        ph = page_heights[page_idx] if page_idx < len(page_heights) else 842
        for line in page_lines:
            if not line:
                continue
            if _looks_like_page_top_matter(line, ph):
                continue
            txt = _line_text(line).strip()
            if _is_junk_line(txt):
                continue

            # Grand total marker — end of data
            if _GRAND_TOTAL_RX.match(txt):
                flat.append((line, '__GT__', None, None))
                continue

            name, debit, credit = _classify_by_column(line, debit_right, credit_right)
            if not name and debit is None and credit is None:
                continue
            flat.append((line, name, debit, credit))

    if not flat:
        return []

    # 2) Determine indent buckets from all first-word x0 values.
    #    Cluster nearby x0s into levels.
    xs = sorted({round(l[0][0]['x0']) for l in flat if l[1] != '__GT__' and l[0]})
    levels: List[float] = []
    for x in xs:
        if not levels or x - levels[-1] > 4:
            levels.append(x)
    # levels[0] = level 0, levels[1] = level 1, ...

    def _level_of(x0: float) -> int:
        best = 0
        best_d = 1e9
        for i, l in enumerate(levels):
            d = abs(x0 - l)
            if d < best_d:
                best_d = d
                best = i
        return best

    # 3) Walk and emit
    events: List[Dict[str, Any]] = []
    n = len(flat)
    # Stack of (level, name) for currently-open groups
    stack: List[Tuple[int, str]] = []

    def _open_group(level: int, name: str):
        # Pop deeper-or-equal levels first
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, name))
        # Emit as group with breadcrumb if inside another
        if len(stack) == 1:
            events.append({'kind': 'group', 'name': name})
        else:
            parent = stack[-2][1]
            events.append({'kind': 'group',
                           'name': f'{parent} ------ ( {name} )'})

    def _emit_leaf(level: int, name: str, d: Optional[float], c: Optional[float]):
        # Ensure stack is trimmed so the leaf sits under the right group
        while stack and stack[-1][0] >= level:
            stack.pop()
        # If nothing open yet, synthesize a bucket
        if not stack:
            events.append({'kind': 'group', 'name': 'Uncategorised'})
            stack.append((0, 'Uncategorised'))
        events.append({'kind': 'leaf', 'name': name, 'debit': d, 'credit': c})

    for i, (line, name, debit, credit) in enumerate(flat):
        if name == '__GT__':
            events.append({'kind': 'grand_total'})
            continue

        level = _level_of(line[0]['x0'])

        # Look ahead to decide group vs leaf
        j = i + 1
        next_level = -1
        while j < n:
            nxt = flat[j]
            if nxt[1] == '__GT__':
                break
            next_level = _level_of(nxt[0][0]['x0'])
            break

        is_group = (next_level > level)

        if is_group:
            # Groups sometimes carry their own subtotal on the same row; that
            # subtotal is recomputed from children, so we IGNORE the values
            # attached to a group row.
            _open_group(level, name)
        else:
            _emit_leaf(level, name, debit, credit)

    return events


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT C PARSER  — DOSPrinter / columnar ASCII-table
# ═══════════════════════════════════════════════════════════════════════════
#
# These PDFs are produced by DOS-era accounting software (DOSPrinter, etc.)
# and look like character-art tables:
#
#   ! Code ! Description          ! Station    !     Debit !    Credit !
#   !======!=====================!============!===========!===========!
#   !      ! CAPITAL A/C         !            !           !           !
#   !      !                     !            !           !           !
#   ! J016 ! SH.JASHANJOT SINGH  !            !           ! 7342734.94!
#   !------!---------------------!------------!-----------!-----------!
#   !      ! TOTAL OF CAPITAL A/C!            !      0.00 ! 7342734.94!
#   !------!---------------------!------------!-----------!-----------!
#
# pdfplumber emits each "!======…" divider row as a SINGLE long word-token.
# Real data rows always have multiple word-tokens, with "!" pipe characters
# embedded as their own tokens (x0≈28, x0≈69, x0≈276, x0≈362, x0≈455,
# x0≈547 in this specific PDF family).
#
# Detection is column-position based.  The "Debit" and "Credit" column
# right-edges are already found by _find_column_boundaries; we re-use them.
# Numbers appear as tokens whose x1 ends with "!" attached (e.g. "4743850.00!")
# — pdfplumber glues the trailing pipe onto the number.  We strip the "!".

def _strip_pipe(text: str) -> str:
    """Remove a trailing '!' that pdfplumber sometimes attaches to a number."""
    return text.rstrip('!')


def _is_amount_c(text: str) -> bool:
    """Like _is_amount but first strips a trailing pipe '!'."""
    return _is_amount(_strip_pipe(text))


def _parse_amount_c(text: str) -> Optional[float]:
    """Like _parse_amount but first strips a trailing pipe '!'."""
    return _parse_amount(_strip_pipe(text))


def _classify_by_column_c(line: List[dict],
                            debit_right: float,
                            credit_right: float) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Column classifier for Format C rows.

    The DOSPrinter table has six columns separated by "!" characters:
      Col 1 (x0≈28–34):   row-start "!"  — always a lone "!" token
      Col 2 (x0≈40–63):   Code (4-char account code, or blank)
      Col 3 (x0≈69–74):   separator "!"
      Col 4 (x0≈80–265):  Description / group name / total label
      Col 5 (x0≈276–282): separator "!"
      Col 6 (x0≈299–362): Station (city) — often embedded as "!LUDHIANA"
      Col 7 (x0≈362–368): separator "!"
      Col 8 (x0≈370–460): Debit value (ends with "!")
      Col 9 (x0≈455–460): separator "!"
      Col 10 (x0≈483–553): Credit value (ends with "!")

    We ignore Code and Station; we collect Description words (x0 < ~270)
    and then classify numeric tokens by which column right-edge they are
    closest to, after stripping trailing "!".
    """
    # x-coordinate boundaries derived from the column header positions
    # (same as what _find_column_boundaries already determined):
    #   debit_right  ≈ 460.8  (x1 of the "Debit" header word)
    #   credit_right ≈ 553.0  (x1 of the "Credit" header word)
    #
    # Token classification:
    #   x0 <  75  → row/col separator "!" — skip
    #   x0  75–270 → Description text     — keep
    #   x0 270–370 → Station col or sep   — skip
    #   x0 370–465 → Debit value          — numeric
    #   x0 465–560 → Credit value         — numeric

    DESC_X_MAX = 270.0
    STATION_X_MAX = 370.0

    part_words: List[str] = []
    debit_val: Optional[float] = None
    credit_val: Optional[float] = None

    for w in line:
        x0 = w['x0']
        t = w['text']

        # Skip pure separator tokens (lone "!")
        if t.strip() == '!':
            continue

        # Station column sometimes appears as "!LUDHIANA" — strip leading "!"
        clean = t.lstrip('!')

        if x0 < DESC_X_MAX:
            # Could be separator chars (===/---) — skip those
            if _DOSPRINTER_DIVIDER_RX.match(clean):
                continue
            # Skip the bare code column (x0 40–63) — we don't need it
            # Actually: include it only if it looks like text, not a 4-char code
            # We skip account codes (4-char alphanumeric starting at x0≈40)
            if 38 <= x0 <= 66 and re.match(r'^[A-Z]\d{3}$', clean):
                continue
            part_words.append(clean)

        elif x0 < STATION_X_MAX:
            # Station column — skip (city names go into Station, not TB)
            # But if it has "!LUDHIANA"-style prefix, city is here — ignore
            continue

        else:
            # Numeric column (Debit or Credit)
            num_str = _strip_pipe(clean)
            if not _is_amount(num_str):
                continue
            val = _parse_amount(num_str)
            if val is None:
                continue
            # Assign to Debit vs Credit by x1 proximity
            x1 = w['x1']
            d_dist = abs(x1 - debit_right)
            c_dist = abs(x1 - credit_right)
            if d_dist <= c_dist and d_dist <= (credit_right - debit_right) * 0.7:
                # Accumulate (rare case of split token — just take last)
                debit_val = val
            else:
                credit_val = val

    part_text = ' '.join(part_words).strip()
    return part_text, debit_val, credit_val


def _parse_format_c(all_pages_lines: List[List[List[dict]]],
                     debit_right: float,
                     credit_right: float,
                     page_heights: List[float]) -> List[Dict[str, Any]]:
    """
    Parse a DOSPrinter / columnar ASCII-table Trial Balance PDF.

    Row classification:
      • Single-token line matching _DOSPRINTER_DIVIDER_RX  → divider, skip
      • Line where description matches _C_GRAND_TOTAL_RX   → grand_total event
      • Line where description matches _C_GROUP_TOTAL_RX   → total consumed
        (we regenerate totals with SUM formulas; ignore the PDF values)
      • Line with no values and description is non-blank   → group header
      • Line with no values and description is blank       → skip (empty row)
      • Line with values                                   → leaf entry

    Sub-groups: in DOSPrinter format the sub-group name appears as a separate
    all-caps line just before its leaves, e.g.:
        ! ! CAPITAL A/C   !  (sub-group name, no values)
        ! J016 ! SH.JASHANJOT SINGH … !  (leaf)
    Section headings (containing "A/C's", "BALANCE SHEET", etc.) open a new
    top-level group; everything else inside a section is a sub-group.

    Page-break suppression: DOSPrinter repeats the current section header and
    current sub-group header at the top of the continuation page.  We track
    the last-seen group and sub-group names and silently skip duplicates.

    DOSPrinter watermark: "DOSPrinter N.N DEMO" appears at the very bottom of
    the page (top > 820pt on an A4 PDF).  We skip bottom-of-page lines.
    """
    events: List[Dict[str, Any]] = []
    current_section: Optional[str] = None   # e.g. "BALANCE SHEET A/C's"
    current_subgroup: Optional[str] = None  # e.g. "CAPITAL A/C"
    saw_grand_total = False

    for page_idx, page_lines in enumerate(all_pages_lines):
        ph = page_heights[page_idx] if page_idx < len(page_heights) else 842

        for line in page_lines:
            if not line:
                continue

            top = line[0]['top']

            # ── Skip DOSPrinter watermark at very bottom of page ───────────
            # The "DOSPrinter 3.4 DEMO" line sits at top ≈ 829pt on A4 (842pt)
            if top > ph * 0.97:
                continue

            # ── Letterhead / page-top: company name + report title ─────────
            # DOSPrinter repeats these at the top of every page (top < ~100pt).
            if top < 100:
                continue

            txt = _line_text(line).strip()

            # ── Single-token divider rows (!=====…, !-----…) ──────────────
            if len(line) == 1 and _DOSPRINTER_DIVIDER_RX.match(txt):
                continue

            # ── Column-header row ("! Code ! Description ! Station ! …") ──
            if re.search(r'\bCode\b.*\bDescription\b', txt, re.I):
                continue

            # ── Classify the line's columns ────────────────────────────────
            desc, debit, credit = _classify_by_column_c(line, debit_right, credit_right)
            desc = desc.strip()

            # Skip blank rows (empty separator rows with only "!" tokens)
            if not desc and debit is None and credit is None:
                continue

            # ── Grand total ("T O T A L" / "TOTAL") ───────────────────────
            # Must be checked BEFORE the leaf branch because the grand-total
            # row also carries numeric values on the same line.
            if _C_GRAND_TOTAL_RX.match(desc):
                if not saw_grand_total:
                    events.append({'kind': 'grand_total'})
                    saw_grand_total = True
                current_section = None
                current_subgroup = None
                continue

            # ── Group / section total ("TOTAL OF …" / "GROUP TOTAL OF …") ─
            # Must be checked BEFORE the leaf branch: these rows always carry
            # D=0.0 or a sum value, so they would otherwise be emitted as
            # leaf entries.  We discard the PDF totals and let _render_workbook
            # regenerate them as live SUM formulas.
            # "GROUP TOTAL OF …" marks the end of a top-level section; clear
            # current_section so the next occurrence of the same section label
            # (e.g. a second "PROFIT & LOSS A/C's" block) is treated as a new
            # group rather than being skipped as a page-break duplicate.
            if _C_GROUP_TOTAL_RX.match(desc):
                current_subgroup = None
                if desc.upper().startswith('GROUP TOTAL'):
                    current_section = None
                continue

            # ── Has numeric values → leaf entry ───────────────────────────
            if debit is not None or credit is not None:
                if desc:
                    events.append({
                        'kind': 'leaf',
                        'name': desc,
                        'debit': debit,
                        'credit': credit,
                    })
                continue

            # ── No values, non-blank description → section or sub-group ───
            if desc:
                # Section-level headings are broad accounting section labels:
                #   "PROFIT & LOSS A/C's"  "BALANCE SHEET A/C's"  "TRADING A/C's"
                # Sub-group headings are specific account categories:
                #   "CAPITAL A/C"  "FIXED ASSETS"  "SUNDRY CREDITORS"
                # Key distinction: section headings contain "A/C's" (possessive
                # plural) OR contain "BALANCE SHEET" or "PROFIT & LOSS" as the
                # dominant phrase.  "CAPITAL A/C" only has singular "A/C".
                is_section = bool(re.search(
                    r"A/C'?s\b|BALANCE\s+SHEET\s+A|PROFIT\s*&\s*LOSS\s+A|TRADING\s+A/C",
                    desc, re.I))

                if is_section:
                    # Page-break duplicate: same section repeated at top of a
                    # continuation page — skip it but reset sub-group tracking
                    # so the next sub-group header is correctly emitted.
                    if desc == current_section:
                        current_subgroup = None
                        continue
                    # Genuine new section (different label, or first time)
                    events.append({'kind': 'group', 'name': desc})
                    current_section = desc
                    current_subgroup = None
                else:
                    # Sub-group heading inside the current section.
                    # Page-break duplicate: same sub-group name repeated at the
                    # top of the next page — skip it, leaves will follow.
                    if desc == current_subgroup:
                        continue
                    # New sub-group: emit with section as parent
                    parent = current_section if current_section is not None else desc
                    if current_section is not None:
                        events.append({
                            'kind': 'group',
                            'name': f'{parent} ------ ( {desc} )',
                        })
                    else:
                        events.append({'kind': 'group', 'name': desc})
                    current_subgroup = desc
                continue

    return events


# ═══════════════════════════════════════════════════════════════════════════
# FLAT FALLBACK PARSER  — last resort
# ═══════════════════════════════════════════════════════════════════════════

def _parse_flat(all_pages_lines: List[List[List[dict]]],
                debit_right: float,
                credit_right: float,
                page_heights: List[float]) -> List[Dict[str, Any]]:
    """
    Emit every non-junk, non-top-matter line as a leaf under a single
    "Trial Balance" group. Loses hierarchy but never loses data.
    """
    events: List[Dict[str, Any]] = [{'kind': 'group', 'name': 'Trial Balance'}]
    for page_idx, page_lines in enumerate(all_pages_lines):
        ph = page_heights[page_idx] if page_idx < len(page_heights) else 842
        for line in page_lines:
            if not line:
                continue
            if _looks_like_page_top_matter(line, ph):
                continue
            txt = _line_text(line).strip()
            if _is_junk_line(txt):
                continue
            if _GRAND_TOTAL_RX.match(txt):
                events.append({'kind': 'grand_total'})
                continue
            if _TOTAL_RX.match(txt):
                continue
            name, debit, credit = _classify_by_column(line, debit_right, credit_right)
            if name and (debit is not None or credit is not None):
                events.append({'kind': 'leaf', 'name': name,
                                'debit': debit, 'credit': credit})
    return events


# ═══════════════════════════════════════════════════════════════════════════
# WORKBOOK RENDERER
# ═══════════════════════════════════════════════════════════════════════════

def _render_workbook(events: List[Dict[str, Any]],
                      meta: Dict[str, str],
                      output_path: str) -> Dict[str, Any]:
    """
    Turn the event stream into an Excel workbook and save it.

    Layout (NO merged cells anywhere):
      Row 1: Company name (col A)
      Row 2: Address     (col A)
      Row 3: Report title (col A)
      Row 4: Subtitle    (col A)
      Row 5: (blank)
      Row 6: 'Particulars' | 'Debit' | 'Credit'
      Row 7+: sections — group header (col A only), then leaves, then
              'Total :' row with SUM formula per column.
      Last row: 'Grand Total' with a SUM across all group totals.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = 'Trial Balance'

    # ── styles ────────────────────────────────────────────────────────────
    font_title = Font(name='Arial', size=13, bold=True)
    font_sub = Font(name='Arial', size=10, bold=True)
    font_hd = Font(name='Arial', size=11, bold=True)
    font_group = Font(name='Arial', size=10, bold=True)
    font_body = Font(name='Arial', size=10)
    font_total = Font(name='Arial', size=10, bold=True)

    fill_hd = PatternFill('solid', fgColor='D9E1F2')
    fill_group = PatternFill('solid', fgColor='F2F2F2')
    fill_total = PatternFill('solid', fgColor='F2F2F2')
    fill_grand = PatternFill('solid', fgColor='FFF2CC')

    align_left = Alignment(horizontal='left', vertical='center', wrap_text=False)
    align_center = Alignment(horizontal='center', vertical='center')
    align_right = Alignment(horizontal='right', vertical='center')

    thin = Side(style='thin', color='BFBFBF')
    border_hd = Border(top=thin, bottom=thin, left=thin, right=thin)

    # ── title block (rows 1-4) — no merges! ───────────────────────────────
    ws['A1'] = meta.get('company', '') or ''
    ws['A1'].font = font_title
    ws['A1'].alignment = align_left

    ws['A2'] = meta.get('address', '') or ''
    ws['A2'].font = font_body
    ws['A2'].alignment = align_left

    ws['A3'] = meta.get('title', 'Trial Balance')
    ws['A3'].font = font_sub
    ws['A3'].alignment = align_left

    ws['A4'] = meta.get('subtitle', '') or ''
    ws['A4'].font = font_body
    ws['A4'].alignment = align_left

    # ── header row 6 ──────────────────────────────────────────────────────
    ws['A6'] = 'Particulars'
    ws['B6'] = 'Debit'
    ws['C6'] = 'Credit'
    for coord in ('A6', 'B6', 'C6'):
        c = ws[coord]
        c.font = font_hd
        c.fill = fill_hd
        c.alignment = align_center
        c.border = border_hd

    # ── data rows starting row 7 ──────────────────────────────────────────
    row = 7
    total_rows: List[int] = []          # row indices of "Total :" rows
    current_group_first_leaf: Optional[int] = None
    current_group_last_leaf: Optional[int] = None
    saw_grand_total = False

    def _close_group():
        nonlocal row, current_group_first_leaf, current_group_last_leaf
        if current_group_first_leaf is None:
            return
        # Emit "Total :" row with SUM formulas
        ws.cell(row=row, column=1, value='Total :').font = font_total
        ws.cell(row=row, column=1).fill = fill_total
        ws.cell(row=row, column=1).alignment = align_right
        b_formula = f'=SUM(B{current_group_first_leaf}:B{current_group_last_leaf})'
        c_formula = f'=SUM(C{current_group_first_leaf}:C{current_group_last_leaf})'
        b = ws.cell(row=row, column=2, value=b_formula)
        c = ws.cell(row=row, column=3, value=c_formula)
        for cell in (b, c):
            cell.font = font_total
            cell.fill = fill_total
            cell.number_format = _INR_FMT
            cell.alignment = align_right
        total_rows.append(row)
        row += 1
        current_group_first_leaf = None
        current_group_last_leaf = None

    for ev in events:
        if ev['kind'] == 'group':
            _close_group()
            ws.cell(row=row, column=1, value=ev['name']).font = font_group
            ws.cell(row=row, column=1).fill = fill_group
            ws.cell(row=row, column=1).alignment = align_left
            row += 1
        elif ev['kind'] == 'leaf':
            ws.cell(row=row, column=1, value=ev['name']).font = font_body
            ws.cell(row=row, column=1).alignment = align_left
            if ev['debit'] is not None:
                cell = ws.cell(row=row, column=2, value=ev['debit'])
                cell.font = font_body
                cell.number_format = _INR_FMT
                cell.alignment = align_right
            if ev['credit'] is not None:
                cell = ws.cell(row=row, column=3, value=ev['credit'])
                cell.font = font_body
                cell.number_format = _INR_FMT
                cell.alignment = align_right
            if current_group_first_leaf is None:
                current_group_first_leaf = row
            current_group_last_leaf = row
            row += 1
        elif ev['kind'] == 'grand_total':
            _close_group()
            # Grand total = sum of each Total row (avoids double-counting
            # if any sub-groups are collapsed).
            ws.cell(row=row, column=1, value='Grand Total').font = font_total
            ws.cell(row=row, column=1).fill = fill_grand
            ws.cell(row=row, column=1).alignment = align_right
            if total_rows:
                b_ref = '+'.join(f'B{r}' for r in total_rows)
                c_ref = '+'.join(f'C{r}' for r in total_rows)
                b_formula = f'={b_ref}'
                c_formula = f'={c_ref}'
            else:
                b_formula = '=0'
                c_formula = '=0'
            b = ws.cell(row=row, column=2, value=b_formula)
            c = ws.cell(row=row, column=3, value=c_formula)
            for cell in (b, c):
                cell.font = font_total
                cell.fill = fill_grand
                cell.number_format = _INR_FMT
                cell.alignment = align_right
            row += 1
            saw_grand_total = True

    _close_group()

    # If parser never encountered a Grand Total marker, synthesize one so
    # the sheet is complete.
    if not saw_grand_total and total_rows:
        ws.cell(row=row, column=1, value='Grand Total').font = font_total
        ws.cell(row=row, column=1).fill = fill_grand
        ws.cell(row=row, column=1).alignment = align_right
        b_ref = '+'.join(f'B{r}' for r in total_rows)
        c_ref = '+'.join(f'C{r}' for r in total_rows)
        b = ws.cell(row=row, column=2, value=f'={b_ref}')
        c = ws.cell(row=row, column=3, value=f'={c_ref}')
        for cell in (b, c):
            cell.font = font_total
            cell.fill = fill_grand
            cell.number_format = _INR_FMT
            cell.alignment = align_right
        row += 1

    # ── column widths ─────────────────────────────────────────────────────
    ws.column_dimensions['A'].width = 55
    ws.column_dimensions['B'].width = 20
    ws.column_dimensions['C'].width = 20

    # ── freeze the header row for easy scrolling ──────────────────────────
    ws.freeze_panes = 'A7'

    wb.save(output_path)

    # Verify — no merged cells (safety net)
    assert len(ws.merged_cells.ranges) == 0, \
        f'Unexpected merged cells: {list(ws.merged_cells.ranges)}'

    leaf_count = sum(1 for e in events if e['kind'] == 'leaf')
    group_count = sum(1 for e in events if e['kind'] == 'group')
    return {
        'rows_written': row - 1,
        'leaf_count': leaf_count,
        'group_count': group_count,
        'total_rows': len(total_rows),
    }


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def convert_pdf_to_tb_excel(pdf_path: str,
                              output_path: str) -> Dict[str, Any]:
    """
    Convert a Trial Balance PDF to a clean editable .xlsx.

    Args
    ----
    pdf_path    : path to the input PDF
    output_path : path where the .xlsx will be written

    Returns
    -------
    dict with keys:
      status         : 'success' | 'error'
      format         : 'A' | 'B' | 'flat' (which parser was used)
      pages          : int, number of pages read
      groups         : int, number of section headers written
      leaves         : int, number of account rows written
      total_rows     : int, number of "Total :" rows written
      output_path    : echo of the destination path
      message        : (only on error) description of what went wrong
    """
    try:
        if not os.path.exists(pdf_path):
            return {'status': 'error',
                    'message': f'Input PDF not found: {pdf_path}'}

        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return {'status': 'error',
                        'message': 'PDF has no pages.'}

            all_pages_lines: List[List[List[dict]]] = []
            page_heights: List[float] = []
            all_lines_flat: List[List[dict]] = []

            for page in pdf.pages:
                page_heights.append(float(page.height))
                words = page.extract_words(
                    keep_blank_chars=False,
                    use_text_flow=True,
                    extra_attrs=['fontname']
                )
                lines = _lines_from_words(words)
                all_pages_lines.append(lines)
                all_lines_flat.extend(lines)

            # Detect format first so we can apply format-specific column logic
            fmt = _detect_format(all_lines_flat)

            bounds = _find_column_boundaries(all_lines_flat)
            if bounds is None:
                if fmt == 'C':
                    # DOSPrinter PDFs have "Debit" and "Credit" as standalone
                    # words on the header row; if _find_column_boundaries still
                    # returns None (e.g. "!" is fused to the header word), fall
                    # back to the known fixed right-edges for this format family.
                    # These values match the x1 coordinates observed in the PDF.
                    debit_right = 460.8
                    credit_right = 553.0
                else:
                    return {'status': 'error',
                            'message': ("Could not detect Debit / Credit column "
                                        "headers in the PDF. The file may be a "
                                        "scanned image (no text layer) or use "
                                        "column names other than 'Debit' and "
                                        "'Credit'.")}
            else:
                debit_right, credit_right = bounds

            if fmt == 'A':
                events = _parse_format_a(all_pages_lines, debit_right,
                                          credit_right, page_heights)
            elif fmt == 'B':
                events = _parse_format_b(all_pages_lines, debit_right,
                                          credit_right, page_heights)
            elif fmt == 'C':
                events = _parse_format_c(all_pages_lines, debit_right,
                                          credit_right, page_heights)
            else:
                events = _parse_flat(all_pages_lines, debit_right,
                                      credit_right, page_heights)

            meta = _extract_metadata(all_pages_lines)

        stats = _render_workbook(events, meta, output_path)
        return {
            'status': 'success',
            'format': fmt,
            'pages': len(page_heights),
            'groups': stats['group_count'],
            'leaves': stats['leaf_count'],
            'total_rows': stats['total_rows'],
            'output_path': output_path,
        }

    except Exception as exc:
        import traceback
        return {
            'status': 'error',
            'message': f'{type(exc).__name__}: {exc}',
            'trace': traceback.format_exc(),
        }


# ── CLI for local sanity checks (not used by the Flask app) ────────────────
if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('Usage: python pdf_tb_processor.py <input.pdf> <output.xlsx>')
        sys.exit(1)
    result = convert_pdf_to_tb_excel(sys.argv[1], sys.argv[2])
    import json as _json
    print(_json.dumps({k: v for k, v in result.items() if k != 'trace'},
                       indent=2))
    if result.get('status') == 'error' and 'trace' in result:
        print(result['trace'], file=sys.stderr)
        sys.exit(2)
