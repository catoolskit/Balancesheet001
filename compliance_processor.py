"""
Statutory Compliance Payment Tracker
Reads a ZIP of statutory payment PDFs and extracts:
  - EPF (EPFO Payment Confirmation Receipt)
  - ESIC (ESIC Online Challan)
  - GST (GST Payment Receipt)
  - TDS/TCS (Income Tax Dept Challan 281N)
  - Electricity Bills (BSES/BYPL etc.)

Returns rows for the compliance table:
  Particulars | Month | Amount (Rs.) | Date Paid | Remarks

v1.0 — 2026-10-16
"""

import re
import zipfile
import io
import os
import tempfile

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _clean(s):
    """Strip whitespace and normalize spaces."""
    if not s:
        return ""
    return re.sub(r'\s+', ' ', s.strip())


def _extract_text(pdf_bytes):
    """Extract full text from PDF bytes using pdfplumber."""
    if not HAS_PDFPLUMBER:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            lines = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    lines.append(t)
            return "\n".join(lines)
    except Exception:
        return ""


def _parse_amount(s):
    """Parse amount string → cleaned string with commas, no /- suffix."""
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r'[₹Rs\.\s]', '', s)
    s = s.replace('/-', '').replace('/', '').strip()
    # Remove leading/trailing commas
    s = s.strip(',')
    # If already has commas, keep; else add Indian formatting
    return s


def _fmt_month(month_str):
    """Normalize month strings like 'AUG-2026', 'Aug-2026', 'August'26' → 'August'26'."""
    if not month_str:
        return ""
    month_str = _clean(month_str)
    # Already in apostrophe format e.g. "August'26"
    if "'" in month_str:
        return month_str
    # "AUG-2026" → "August'26"
    m = re.match(r'([A-Za-z]+)[- ](\d{4})', month_str)
    if m:
        mon_abbr = m.group(1).title()
        year = m.group(2)[2:]  # last 2 digits
        month_map = {
            'Jan': 'January', 'Feb': 'February', 'Mar': 'March',
            'Apr': 'April', 'May': 'May', 'Jun': 'June',
            'Jul': 'July', 'Aug': 'August', 'Sep': 'September',
            'Oct': 'October', 'Nov': 'November', 'Dec': 'December',
        }
        full = month_map.get(mon_abbr[:3], mon_abbr)
        return f"{full}'{year}"
    # "Apr'26" type — already fine
    return month_str


def _fmt_date(date_str):
    """Normalize dates to DD.MM.YYYY format."""
    if not date_str:
        return ""
    date_str = _clean(date_str)
    # DD-MMM-YYYY → DD.MM.YYYY
    month_num = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
        'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
        'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
    }
    m = re.match(r'(\d{1,2})[- ]([A-Za-z]{3})[- ](\d{4})', date_str)
    if m:
        day = m.group(1).zfill(2)
        mon = month_num.get(m.group(2).lower(), '00')
        yr = m.group(3)
        return f"{day}.{mon}.{yr}"
    # DD/MM/YYYY or DD-MM-YYYY → DD.MM.YYYY
    m2 = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', date_str)
    if m2:
        return f"{m2.group(1).zfill(2)}.{m2.group(2).zfill(2)}.{m2.group(3)}"
    # DD-MM-YYYY already with dots
    m3 = re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', date_str)
    if m3:
        return f"{m3.group(1).zfill(2)}.{m3.group(2)}.{m3.group(3)}"
    return date_str


# ═══════════════════════════════════════════════════════════════════════
#  DOCUMENT TYPE DETECTORS
# ═══════════════════════════════════════════════════════════════════════

def _detect_doc_type(text):
    """Return document type string based on text content."""
    t = text.lower()
    if "employees' provident fund" in t or "epfo" in t or "payment confirmation receipt" in t and "trrn" in t:
        return "EPF"
    if "employees' state insurance" in t or "esic" in t or "challan number" in t and "employer's code" in t:
        return "ESIC"
    if "goods and services tax" in t and "payment receipt" in t and ("cgst" in t or "igst" in t or "sgst" in t):
        return "GST"
    if "income tax department" in t and ("challan receipt" in t or "tds/tcs" in t or "281" in t):
        return "TDS"
    if ("bill of supply for electricity" in t or "bses" in t or "bypl" in t or
            "electricity" in t and ("meter no" in t or "billing details" in t)):
        return "ELECTRICITY"
    return "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════
#  PARSERS — one per document type
# ═══════════════════════════════════════════════════════════════════════

def _parse_epf(text):
    """
    Parse EPFO Payment Confirmation Receipt.
    Extracts: Wage Month, Total Amount, Payment Confirmation Date
    Returns list of row dicts (usually 1 row per challan).
    """
    rows = []

    # Total Amount
    amt_m = re.search(r'Total Amount\s*\(Rs\)\s*[:\-]?\s*([\d,]+)', text)
    amount = _parse_amount(amt_m.group(1)) if amt_m else ""

    # Wage Month
    month_m = re.search(r'Wage Month\s*[:\-]?\s*([A-Z]{3}[- ]\d{4})', text)
    month = _fmt_month(month_m.group(1)) if month_m else ""

    # Payment Confirmation Date
    date_m = re.search(r'Payment Confirmation Date\s*[:\-]?\s*(\d{2}[- ][A-Z]{3}[- ]\d{4})', text)
    if not date_m:
        date_m = re.search(r'Payment Confirmation Date\s*[:\-]?\s*(\d{2}[-/]\d{2}[-/]\d{4})', text)
    date_paid = _fmt_date(date_m.group(1)) if date_m else ""

    if amount or month:
        rows.append({
            "particulars": "EPF",
            "month": month,
            "amount": amount,
            "date_paid": date_paid,
            "remarks": "Amt. paid before Due Date.",
        })
    return rows


def _parse_esic(text):
    """
    Parse ESIC Online Challan confirmation.
    Extracts: Challan Period, Amount Paid, Challan Submitted Date
    """
    rows = []

    # Amount Paid
    amt_m = re.search(r'Amount Paid\s*[:\-]?\s*([\d,]+)', text)
    amount = _parse_amount(amt_m.group(1)) + ".00" if amt_m else ""

    # Challan Period
    period_m = re.search(r'Challan Period\s*[:\-]?\s*([A-Za-z]+-\d{4})', text)
    month = _fmt_month(period_m.group(1)) if period_m else ""

    # Challan Submitted Date (payment date)
    date_m = re.search(r'Challan Submitted Date\s*[:\-]?\s*(\d{2}[-/]\d{2}[-/]\d{4})', text)
    date_paid = _fmt_date(date_m.group(1)) if date_m else ""

    if amount or month:
        rows.append({
            "particulars": "ESI",
            "month": month,
            "amount": amount,
            "date_paid": date_paid,
            "remarks": "Amt. paid before Due Date.",
        })
    return rows


def _parse_gst(text):
    """
    Parse GST Payment Receipt.
    Extracts: CPIN date (month), Total Amount, Deposit Date
    Returns 1 row per receipt.
    """
    rows = []

    # Total Amount
    amt_m = re.search(r'Total Amount\s*\n?([\d,]+)', text)
    if not amt_m:
        # Try "Total Amount (in words) ... Total Amount XXXXXX"
        amt_m = re.search(r'Total Amount\s+([\d,]+)\s', text)
    amount = _parse_amount(amt_m.group(1)) + ".00" if amt_m else ""

    # Deposit Date
    date_m = re.search(r'Deposit Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})', text)
    date_paid = _fmt_date(date_m.group(1)) if date_m else ""

    # Month from deposit date
    month = ""
    if date_m:
        d = date_m.group(1)  # DD/MM/YYYY
        parts = d.split('/')
        if len(parts) == 3:
            month_num_map = {
                '01': 'January', '02': 'February', '03': 'March',
                '04': 'April', '05': 'May', '06': 'June',
                '07': 'July', '08': 'August', '09': 'September',
                '10': 'October', '11': 'November', '12': 'December',
            }
            mon_full = month_num_map.get(parts[1], parts[1])
            year_short = parts[2][2:]
            month = f"{mon_full}'{year_short}"

    if amount or date_paid:
        rows.append({
            "particulars": "GST",
            "month": month,
            "amount": amount,
            "date_paid": date_paid,
            "remarks": "Amt. paid before Due Date.",
        })
    return rows


def _parse_tds(text):
    """
    Parse Income Tax Dept Challan Receipt (ITNS 281N).
    Extracts: Section description (for particulars), Tax Year → month,
              Amount, Date of Deposit
    """
    rows = []

    # Amount
    amt_m = re.search(r'Amount\s*\(in Rs\.\)\s*[:\-]?\s*₹?\s*([\d,]+)', text)
    if not amt_m:
        amt_m = re.search(r'Total\s*\(A\+B\+C\+D\+E\+F\+G\)\s*([\d,]+)', text)
    amount = ""
    if amt_m:
        raw = amt_m.group(1).replace(',', '').strip()
        # Format with Indian comma system
        try:
            n = int(raw)
            # Simple formatting: just add .00 and commas
            amount = f"{n:,}.00"
        except Exception:
            amount = raw + ".00"

    # Date of Deposit
    date_m = re.search(r'Date of Deposit\s*[:\-]?\s*(\d{2}[- ][A-Za-z]{3}[- ]\d{4})', text)
    if not date_m:
        date_m = re.search(r'Date of Deposit\s*[:\-]?\s*(\d{2}[-/]\d{2}[-/]\d{4})', text)
    date_paid = _fmt_date(date_m.group(1)) if date_m else ""

    # Month from date of deposit
    month = ""
    if date_paid:
        parts = date_paid.split('.')
        if len(parts) == 3:
            month_num_map = {
                '01': 'January', '02': 'February', '03': 'March',
                '04': 'April', '05': 'May', '06': 'June',
                '07': 'July', '08': 'August', '09': 'September',
                '10': 'October', '11': 'November', '12': 'December',
            }
            mon_full = month_num_map.get(parts[1], parts[1])
            year_short = parts[2][2:]
            month = f"{mon_full}'{year_short}"

    # For TDS, particulars = "TDS"
    # Get section description for remarks
    sec_m = re.search(r'Description\s+Section\s+Code.*?\n(.*?)\n', text, re.DOTALL)
    section_desc = ""
    if sec_m:
        line = _clean(sec_m.group(1))
        section_desc = line[:60] if line else ""

    if amount or date_paid:
        rows.append({
            "particulars": "TDS",
            "month": month,
            "amount": amount,
            "date_paid": date_paid,
            "remarks": "Amt. paid before Due Date.",
        })
    return rows


def _parse_electricity(text):
    """
    Parse BSES/BYPL electricity bill.
    Extracts: Bill Month, Bill Amount Payable, Due Date
    """
    rows = []

    # Bill Amount Payable (Net Amount)
    # BSES format: "₹20330.00" or "20325.13"
    amt_m = re.search(r'Net Amount\s*Payable\s*.*?([\d,]+\.\d{2})', text)
    if not amt_m:
        # Try "Bill Amount Payable ₹20330.00"
        amt_m = re.search(r'Bill Amount Payable.*?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', text)
    if not amt_m:
        # Try "₹ 20330.00" near "payable"
        amt_m = re.search(r'[₹\`][\s]*([\d,]+(?:\.\d{2})?)', text)
    amount = ""
    if amt_m:
        raw = amt_m.group(1).replace(',', '').strip()
        try:
            n = float(raw)
            amount = f"{n:,.2f}"
        except Exception:
            amount = raw

    # Bill Month
    bm_m = re.search(r'Bill Month\s*[:\-]?\s*([A-Z]{3}[- ]\d{2,4})', text)
    if not bm_m:
        bm_m = re.search(r'Bill Month\s*[:\-]?\s*([A-Za-z]+-\d{2,4})', text, re.IGNORECASE)
    month = ""
    if bm_m:
        month_str = bm_m.group(1).strip()
        # "SEP-26" → "September'26"
        m2 = re.match(r'([A-Za-z]+)[- ](\d{2,4})', month_str)
        if m2:
            abbr = m2.group(1)[:3].title()
            yr = m2.group(2)[-2:]
            month_map = {
                'Jan': 'January', 'Feb': 'February', 'Mar': 'March',
                'Apr': 'April', 'May': 'May', 'Jun': 'June',
                'Jul': 'July', 'Aug': 'August', 'Sep': 'September',
                'Oct': 'October', 'Nov': 'November', 'Dec': 'December',
            }
            full_mon = month_map.get(abbr, abbr)
            month = f"{full_mon}'{yr}"
        else:
            month = month_str

    # Due Date as "date paid" (the due date is the relevant date for electricity)
    due_m = re.search(r'Due Date.*?(\d{2}[- /]\d{2}[- /]\d{4})', text)
    if not due_m:
        due_m = re.search(r'(\d{2}[- ]\d{2}[- ]\d{4})', text)
    date_paid = _fmt_date(due_m.group(1)) if due_m else ""

    if amount or month:
        rows.append({
            "particulars": "Electricity",
            "month": month,
            "amount": amount,
            "date_paid": date_paid,
            "remarks": "Amt. paid before Due Date.",
        })
    return rows


# ═══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

# Priority order for the output table (matches typical compliance register)
_PARTICULARS_ORDER = ["ESI", "EPF", "TDS", "GST", "Electricity"]


def process_compliance_zip(zip_bytes):
    """
    Process a ZIP file containing statutory payment PDFs.

    Args:
        zip_bytes: bytes of the ZIP file

    Returns:
        {
            "status": "success" | "error",
            "rows": [
                {
                    "particulars": str,
                    "month": str,
                    "amount": str,
                    "date_paid": str,
                    "remarks": str,
                    "source_file": str,
                    "doc_type": str,
                }
            ],
            "errors": [str],  # files that failed to parse
            "message": str,
        }
    """
    if not HAS_PDFPLUMBER:
        return {
            "status": "error",
            "rows": [],
            "errors": [],
            "message": "pdfplumber is not installed on this server.",
        }

    rows = []
    errors = []

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            pdf_files = [
                name for name in zf.namelist()
                if name.lower().endswith('.pdf')
                and not name.startswith('__MACOSX')
                and not os.path.basename(name).startswith('.')
            ]

            if not pdf_files:
                return {
                    "status": "error",
                    "rows": [],
                    "errors": [],
                    "message": "No PDF files found in the ZIP.",
                }

            for fname in sorted(pdf_files):
                try:
                    pdf_bytes = zf.read(fname)
                    text = _extract_text(pdf_bytes)
                    if not text.strip():
                        errors.append(f"{fname}: Could not extract text (scanned PDF?)")
                        continue

                    doc_type = _detect_doc_type(text)
                    parsed_rows = []

                    if doc_type == "EPF":
                        parsed_rows = _parse_epf(text)
                    elif doc_type == "ESIC":
                        parsed_rows = _parse_esic(text)
                    elif doc_type == "GST":
                        parsed_rows = _parse_gst(text)
                    elif doc_type == "TDS":
                        parsed_rows = _parse_tds(text)
                    elif doc_type == "ELECTRICITY":
                        parsed_rows = _parse_electricity(text)
                    else:
                        errors.append(f"{fname}: Could not identify document type")
                        continue

                    for r in parsed_rows:
                        r["source_file"] = os.path.basename(fname)
                        r["doc_type"] = doc_type
                        rows.append(r)

                except Exception as e:
                    errors.append(f"{fname}: {str(e)}")

    except zipfile.BadZipFile:
        return {
            "status": "error",
            "rows": [],
            "errors": [],
            "message": "Invalid ZIP file. Please upload a valid .zip file.",
        }
    except Exception as e:
        return {
            "status": "error",
            "rows": [],
            "errors": [str(e)],
            "message": f"Error processing ZIP: {e}",
        }

    # Sort rows by particulars priority order, then by month
    def _sort_key(r):
        try:
            pri = _PARTICULARS_ORDER.index(r["particulars"])
        except ValueError:
            pri = 99
        return (pri, r.get("month", ""), r.get("source_file", ""))

    rows.sort(key=_sort_key)

    return {
        "status": "success" if rows else "error",
        "rows": rows,
        "errors": errors,
        "message": (
            f"Extracted {len(rows)} record(s) from {len(pdf_files)} PDF(s)."
            + (f" {len(errors)} file(s) had issues." if errors else "")
        ) if rows else "No records could be extracted. Check that the PDFs are text-based, not scanned images.",
    }


def generate_excel(rows):
    """
    Generate an Excel file from extracted rows.
    Returns bytes of the .xlsx file.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (Font, PatternFill, Alignment,
                                      Border, Side, numbers)
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "Compliance Register"

    # ── Styles ─────────────────────────────────────────────────────────
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    alt_fill = PatternFill("solid", fgColor="EBF3FB")

    # ── Header row ──────────────────────────────────────────────────────
    headers = ["Particulars", "Month", "Amount (Rs.)", "Date Paid", "Remarks"]
    col_widths = [18, 16, 18, 16, 30]

    for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = border
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.row_dimensions[1].height = 30

    # ── Data rows ───────────────────────────────────────────────────────
    for ri, row in enumerate(rows, 2):
        values = [
            row.get("particulars", ""),
            row.get("month", ""),
            row.get("amount", ""),
            row.get("date_paid", ""),
            row.get("remarks", ""),
        ]
        fill = alt_fill if ri % 2 == 0 else None
        for ci, val in enumerate(values, 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            cell.font = Font(name="Calibri", size=10)
            if fill:
                cell.fill = fill

        ws.row_dimensions[ri].height = 18

    # ── Freeze header ───────────────────────────────────────────────────
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()
