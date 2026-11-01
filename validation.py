"""Task 3 - Data validation. Pure functions (no DB) so they are trivial to unit test.

All money math uses Decimal. Each issue: {code, severity: error|warning, field, message}.
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from extraction import norm_name, norm_number, normalize_date

REQUIRED_ERROR = ("invoice_number", "invoice_date", "supplier_name", "total")
REQUIRED_WARN = ("customer_name", "currency", "subtotal", "tax")
REVIEW_CODES = {"LOW_CONFIDENCE", "POSSIBLE_DUPLICATE"}


def D(x):
    return None if x is None else Decimal(str(x))


def _issue(code, severity, field, message):
    return {"code": code, "severity": severity, "field": field, "message": message}


def validate_invoice(inv, duplicates=(), tol=Decimal("0.02"), min_conf=0.6, today=None):
    """`inv`: dict with the 8 fields, line_items, confidence, parse_errors.
    `duplicates`: earlier invoices with the same invoice number (dicts with supplier_name/total/
    filename)."""
    issues = []
    today = today or date.today()
    conf = inv.get("confidence") or {}

    # ---- missing fields
    for f in REQUIRED_ERROR:
        if inv.get(f) in (None, ""):
            issues.append(_issue("MISSING_FIELD", "error", f, f"Required field '{f}' is missing"))
    for f in REQUIRED_WARN:
        if inv.get(f) in (None, ""):
            issues.append(_issue("MISSING_FIELD", "warning", f, f"Field '{f}' is missing"))

    # ---- unparseable raw values reported by the extractor
    for pe in inv.get("parse_errors") or []:
        fld = pe.get("field", "?")
        code = "INVALID_DATE" if fld == "invoice_date" else "INVALID_NUMBER"
        issues.append(_issue(code, "error", fld, f"Value '{pe.get('raw')}' is not a valid "
                             f"{'date' if code == 'INVALID_DATE' else 'number'}"))

    # ---- date sanity
    d = inv.get("invoice_date")
    if d:
        iso, _ = normalize_date(d)
        if iso is None:
            issues.append(_issue("INVALID_DATE", "error", "invoice_date", f"'{d}' is not a valid date"))
        else:
            dt = date.fromisoformat(iso)
            if dt > today + timedelta(days=1):
                issues.append(_issue("FUTURE_DATE", "warning", "invoice_date",
                                     f"Invoice date {iso} is in the future"))
            elif dt.year < 1990:
                issues.append(_issue("INVALID_DATE", "error", "invoice_date",
                                     f"Invoice date {iso} is implausibly old"))

    # ---- currency
    cur = inv.get("currency")
    if cur and not re.fullmatch(r"[A-Z]{3}", str(cur)):
        issues.append(_issue("INVALID_CURRENCY", "error", "currency",
                             f"'{cur}' is not a 3-letter ISO currency code"))

    # ---- numeric sanity
    sub, tax, total = D(inv.get("subtotal")), D(inv.get("tax")), D(inv.get("total"))
    for name, v in (("subtotal", sub), ("tax", tax), ("total", total)):
        if v is not None and v < 0:
            issues.append(_issue("INVALID_NUMBER", "error", name, f"'{name}' is negative ({v})"))
    if total is not None and total == 0:
        issues.append(_issue("INVALID_NUMBER", "warning", "total", "Invoice total is zero"))
    if tax is not None and sub is not None and sub > 0 and tax > sub:
        issues.append(_issue("INVALID_NUMBER", "warning", "tax",
                             f"Tax ({tax}) is larger than the subtotal ({sub})"))

    # ---- line items
    items = inv.get("line_items") or []
    line_sum, line_sum_ok = Decimal(0), bool(items)
    for n, it in enumerate(items, 1):
        q, p, a = D(it.get("quantity")), D(it.get("unit_price")), D(it.get("amount"))
        for k, v in (("quantity", q), ("unit_price", p), ("amount", a)):
            if v is not None and v < 0:
                issues.append(_issue("INVALID_NUMBER", "warning", f"line_items[{n}].{k}",
                                     f"Line {n}: {k} is negative ({v})"))
        if a is None:
            line_sum_ok = False
        else:
            line_sum += a
        if q is not None and p is not None and a is not None:
            expected = q * p
            if abs(expected - a) > max(tol, Decimal("0.005") * abs(expected)):
                issues.append(_issue("LINE_ITEM_MISMATCH", "error", f"line_items[{n}]",
                                     f"Line {n}: {q} x {p} = {expected:.2f}, but amount is {a}"))

    # ---- arithmetic consistency
    if sub is not None and tax is not None and total is not None:
        if abs(sub + tax - total) > tol:
            issues.append(_issue("TOTAL_MISMATCH", "error", "total",
                                 f"Subtotal {sub} + tax {tax} = {sub + tax}, but total is {total}"))
    if line_sum_ok:
        line_tol = tol * max(1, len(items))
        if sub is not None:
            if abs(line_sum - sub) > line_tol:
                issues.append(_issue("SUBTOTAL_MISMATCH", "error", "subtotal",
                                     f"Line items add up to {line_sum}, but subtotal is {sub}"))
        elif total is not None and tax is None:
            if abs(line_sum - total) > line_tol:
                issues.append(_issue("SUBTOTAL_MISMATCH", "warning", "total",
                                     f"Line items add up to {line_sum}, but total is {total}"))
        elif total is not None and tax is not None and abs(line_sum + tax - total) > line_tol:
            issues.append(_issue("SUBTOTAL_MISMATCH", "error", "total",
                                 f"Line items ({line_sum}) + tax ({tax}) differ from total ({total})"))

    # ---- duplicates
    for dup in duplicates:
        same_supplier = (not norm_name(inv.get("supplier_name")) or not norm_name(dup.get("supplier_name"))
                         or norm_name(inv.get("supplier_name")) == norm_name(dup.get("supplier_name")))
        dtotal = D(dup.get("total"))
        same_total = total is None or dtotal is None or abs(total - dtotal) <= tol
        where = dup.get("filename") or dup.get("document_id") or "another document"
        if same_supplier and same_total:
            issues.append(_issue("DUPLICATE_INVOICE", "error", "invoice_number",
                                 f"Same invoice number, supplier and total already exist in {where}"))
        else:
            issues.append(_issue("POSSIBLE_DUPLICATE", "warning", "invoice_number",
                                 f"Invoice number already used in {where} with different supplier/total"))
        break  # one is enough

    # ---- low confidence
    for f, c in conf.items():
        if inv.get(f) not in (None, "") and c is not None and c < min_conf:
            issues.append(_issue("LOW_CONFIDENCE", "warning", f,
                                 f"'{f}' extracted with low confidence ({c:.0%}); please verify"))
    return issues


def needs_review(issues):
    return any(i["severity"] == "error" or i["code"] in REVIEW_CODES for i in issues)


# ============================================================================ cross verification
def _similar(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return a == b
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.85


def cross_verify(list_a, list_b, tol=0.02):
    """Compare invoices from two sources (PDF vs PDF, PDF vs Excel...). Matching key is the
    normalised invoice number. Each input item is an invoice dict (Invoice.to_dict())."""
    idx_b = {}
    for inv in list_b:
        idx_b.setdefault(norm_number(inv.get("invoice_number")), []).append(inv)
    matched, only_a, used = [], [], set()
    for a in list_a:
        key = norm_number(a.get("invoice_number"))
        cands = [c for c in idx_b.get(key, []) if id(c) not in used] if key else []
        if not cands:
            only_a.append(_brief(a))
            continue
        b = cands[0]
        used.add(id(b))
        diffs = []
        for f in ("invoice_date", "currency"):
            if a.get(f) != b.get(f) and (a.get(f) or b.get(f)):
                diffs.append({"field": f, "a": a.get(f), "b": b.get(f)})
        if not _similar(a.get("supplier_name"), b.get("supplier_name")):
            diffs.append({"field": "supplier_name", "a": a.get("supplier_name"), "b": b.get("supplier_name")})
        if not _similar(a.get("customer_name"), b.get("customer_name")):
            diffs.append({"field": "customer_name", "a": a.get("customer_name"), "b": b.get("customer_name")})
        for f in ("subtotal", "tax", "total"):
            va, vb = a.get(f), b.get(f)
            if (va is None) != (vb is None) or (va is not None and abs(va - vb) > tol):
                diffs.append({"field": f, "a": va, "b": vb})
        la, lb = a.get("line_items") or [], b.get("line_items") or []
        if la and lb and len(la) != len(lb):
            diffs.append({"field": "line_item_count", "a": len(la), "b": len(lb)})
        matched.append({"invoice_number": a.get("invoice_number"), "invoice_a": a.get("id"),
                        "invoice_b": b.get("id"), "match": not diffs, "differences": diffs})
    only_b = [_brief(b) for items in idx_b.values() for b in items if id(b) not in used]
    return {
        "summary": {"compared": len(matched), "identical": sum(m["match"] for m in matched),
                    "with_differences": sum(not m["match"] for m in matched),
                    "only_in_a": len(only_a), "only_in_b": len(only_b)},
        "matched": matched, "only_in_a": only_a, "only_in_b": only_b,
    }


def _brief(inv):
    return {"id": inv.get("id"), "invoice_number": inv.get("invoice_number"),
            "supplier_name": inv.get("supplier_name"), "total": inv.get("total")}
