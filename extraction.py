"""Task 2 - Information extraction.

Three layers, cheapest first:
  1. heuristics  - regexes over row-aligned text (free, instant)
  2. LLM         - Claude, only when heuristics are weak (hybrid) or always (llm mode)
  3. grounding   - every value returned by the LLM must literally appear in the source text,
                   otherwise it is discarded. This is what prevents invented values.
Missing/uncertain data is returned as null with a low or absent confidence, never guessed.
"""
import json
import logging
import math
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as dtparser

log = logging.getLogger(__name__)

SETTINGS = {"dayfirst": True}

CURRENCY_CODES = {
    "USD", "EUR", "GBP", "BDT", "INR", "JPY", "CNY", "CAD", "AUD", "CHF", "SEK", "NOK", "DKK",
    "SGD", "HKD", "AED", "SAR", "NZD", "ZAR", "BRL", "MXN", "PKR", "LKR", "MYR", "THB", "KRW",
    "TRY", "PLN", "RUB", "IDR", "PHP", "VND", "EGP", "NGN", "KES",
}
SYMBOLS = {"€": ("EUR", 0.9), "£": ("GBP", 0.9), "৳": ("BDT", 0.9), "₹": ("INR", 0.9),
           "$": ("USD", 0.6), "¥": ("JPY", 0.5)}
CENT = Decimal("0.01")
REQUIRED_FOR_NO_LLM = ("invoice_number", "invoice_date", "supplier_name", "total")


class ExtractionError(Exception):
    pass


def configure(dayfirst=True):
    SETTINGS["dayfirst"] = dayfirst


# ============================================================================ parsing helpers
def parse_amount(value):
    """'1,234.56' '1.234,56' '$ 12' '(45.00)' '12,5' -> Decimal, or None if not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-") or s.endswith("-")
    t = re.sub(r"(?i)\b(?:usd|eur|gbp|bdt|inr|jpy|cad|aud|tk)\b\.?", "", s)
    t = re.sub(r"[$€£₹৳¥\s()+\-]", "", t)
    if not t or not re.fullmatch(r"[\d.,]+", t) or not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        head, _, tail = t.rpartition(",")
        t = head.replace(",", "") + "." + tail if (len(tail) in (1, 2) and "," not in head) \
            else t.replace(",", "")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    return -d if neg else d


def to_float(d, places=2):
    return None if d is None else float(round(d, places))


def normalize_date(value):
    """-> (iso_string | None, ambiguous: bool)."""
    if value is None:
        return None, False
    if isinstance(value, datetime):
        return value.date().isoformat(), False
    if isinstance(value, date):
        return value.isoformat(), False
    s = str(value).strip()
    if not s or s.lower() in ("nan", "nat", "none", "null"):
        return None, False
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}(?:[T ].*)?", s):
            y, m, d = (int(x) for x in re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s).groups())
            return date(y, m, d).isoformat(), False
        m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
        if m:
            a, b, y = (int(x) for x in m.groups())
            y += 2000 if y < 100 else 0
            if a > 12 >= b:
                return date(y, b, a).isoformat(), False
            if b > 12 >= a:
                return date(y, a, b).isoformat(), False
            day, month = (a, b) if SETTINGS["dayfirst"] else (b, a)
            return date(y, month, day).isoformat(), a != b
        return dtparser.parse(s, dayfirst=SETTINGS["dayfirst"]).date().isoformat(), False
    except (ValueError, OverflowError, TypeError):
        return None, False


def norm_name(s):
    if not s:
        return None
    s = re.sub(r"[^a-z0-9 ]+", " ", str(s).lower())
    s = re.sub(r"\b(inc|llc|ltd|limited|co|corp|corporation|gmbh|plc|pvt|pty|company)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def norm_number(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower()) or None if s else None


def _blank(v):
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip().lower() in ("", "nan", "nat", "none", "null")


# ============================================================================ heuristics
_MONEY = re.compile(r"\(?-?\s*(?:[$€£₹৳¥]\s*)?\d[\d,]*(?:\.\d+)?\s*\)?")
_DATE_PATTERNS = [
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}",
    r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}",
    r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
]
_DATE_RE = re.compile("|".join(f"(?:{p})" for p in _DATE_PATTERNS))
_INV_NUM_RE = re.compile(
    r"(?i)\b(?:invoice|receipt|bill|inv)\s*(?:no\.?|number|num\.?|#|id)\s*[:#.\-]?\s*"
    r"([A-Z0-9][A-Z0-9\-/_.]*)")
_INV_FALLBACK_RE = re.compile(r"\b(INV[-/_]?[A-Z0-9][A-Z0-9\-/_]*\d[A-Z0-9\-/_]*)\b")
_LINE_RE = re.compile(
    r"^\s*(?:\d{1,3}[.)]\s+)?(?P<desc>[A-Za-z][^\n]*?)\s{1,}(?P<qty>\d+(?:\.\d+)?)\s*"
    r"(?:x|pcs|ea|units?|hrs?)?\s+[$€£₹৳¥]?\s*(?P<price>\d[\d,]*\.\d{1,2})\s+"
    r"[$€£₹৳¥]?\s*(?P<amount>\d[\d,]*\.\d{1,2})\s*$", re.I)
_TOTAL_LINE_WORDS = re.compile(r"(?i)\b(sub\s*-?\s*total|total|tax|vat|gst|balance|amount due|"
                               r"discount|shipping|freight)\b")


def find_invoice_number(text):
    for m in _INV_NUM_RE.finditer(text or ""):
        cand = m.group(1).rstrip(".-/_")
        if re.search(r"\d", cand) and cand.lower() not in ("date",):
            return cand
    m = _INV_FALLBACK_RE.search(text or "")
    return m.group(1) if m else None


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _last_money(line):
    vals = [m.group(0) for m in _MONEY.finditer(line)]
    for raw in reversed(vals):
        d = parse_amount(raw)
        if d is not None:
            return d
    return None


def _amount_for(lines, label_res, skip_re=None, reverse=True):
    seq = list(enumerate(lines))
    if reverse:
        seq.reverse()
    for label in label_res:
        rx = re.compile(label, re.I)
        for i, ln in seq:
            if not rx.search(ln) or (skip_re and skip_re.search(ln)):
                continue
            tail = ln[rx.search(ln).end():]
            d = _last_money(tail)
            if d is None and i + 1 < len(lines):
                nxt = lines[i + 1]
                if not re.search(r"[A-Za-z]{3,}", nxt):
                    d = _last_money(nxt)
            if d is not None:
                return d
    return None


def _value_after_label(lines, labels, max_len=80):
    stop = re.compile(r"(?i)^(invoice|date|due|tax|total|bill|ship|from|to|po\b|qty|item|description)")
    for label in labels:
        rx = re.compile(label, re.I)
        for i, ln in enumerate(lines):
            m = rx.search(ln)
            if not m:
                continue
            rest = ln[m.end():].strip(" :-\t")
            rest = re.split(r"\s{3,}", rest)[0].strip()
            if not rest and i + 1 < len(lines):
                rest = re.split(r"\s{3,}", lines[i + 1])[0].strip()
                if stop.match(rest):
                    rest = ""
            if 2 <= len(rest) <= max_len and re.search(r"[A-Za-z]", rest):
                return rest
    return None


def _find_date(lines):
    labels = [r"invoice\s*date", r"date\s+of\s+issue", r"issue\s*date", r"date\s+issued",
              r"\bdated?\b"]
    for label in labels:
        rx = re.compile(label, re.I)
        for i, ln in enumerate(lines):
            m = rx.search(ln)
            if not m or re.search(r"(?i)\bdue\s*date|date\s+due|payment", ln[:m.end() + 1]):
                continue
            hay = ln[m.end():] or ""
            dm = _DATE_RE.search(hay) or (i + 1 < len(lines) and _DATE_RE.fullmatch(lines[i + 1]))
            if dm:
                return dm.group(0) if hasattr(dm, "group") else lines[i + 1]
    return None


def _find_currency(text):
    counts = {}
    for m in re.finditer(r"\b([A-Z]{3})\b", text or ""):
        if m.group(1) in CURRENCY_CODES:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if counts:
        return max(counts, key=counts.get), 0.9
    for sym, (code, conf) in SYMBOLS.items():
        if sym in (text or ""):
            return code, conf
    return None, 0.0


def _find_line_items(lines):
    items = []
    for ln in lines:
        if _TOTAL_LINE_WORDS.search(ln) and not re.match(r"^\s*\d", ln):
            continue
        m = _LINE_RE.match(ln)
        if not m:
            continue
        items.append({"description": m["desc"].strip(),
                      "quantity": to_float(parse_amount(m["qty"]), 4),
                      "unit_price": to_float(parse_amount(m["price"]), 4),
                      "amount": to_float(parse_amount(m["amount"]))})
    return items


def heuristic_extract(text):
    lines = _lines(text)
    fields, conf, notes = {}, {}, []

    num = find_invoice_number(text)
    fields["invoice_number"], conf["invoice_number"] = num, (0.85 if num else 0.0)

    raw_date = _find_date(lines)
    iso, amb = normalize_date(raw_date) if raw_date else (None, False)
    fields["invoice_date"] = iso
    conf["invoice_date"] = 0.0 if not iso else (0.55 if amb else 0.85)
    if amb:
        notes.append(f"Date '{raw_date}' is ambiguous (day/month order assumed)")

    supplier = _value_after_label(lines, [r"\b(?:bill(?:ed)?\s+from|sold\s+by|issued\s+by|"
                                          r"supplier|vendor|seller|from)\b\s*:?"])
    sconf = 0.75
    if not supplier:
        for ln in lines[:8]:
            first = re.split(r"\s{3,}", ln)[0].strip()
            if (3 <= len(first) <= 80 and re.search(r"[A-Za-z]{3}", first)
                    and not re.search(r"(?i)\b(invoice|receipt|tax|bill|page|date|statement|"
                                      r"total|qty|description)\b", first)
                    and sum(c.isdigit() for c in first) < len(first) * 0.3):
                suffix = re.search(r"(?i)\b(ltd|limited|inc|llc|corp|corporation|gmbh|plc|co|company|"
                                   r"pvt|pty|traders|supplies|industries|services)\b\.?", first)
                supplier, sconf = first, (0.75 if suffix else 0.5)
                break
    fields["supplier_name"], conf["supplier_name"] = supplier, (sconf if supplier else 0.0)

    customer = _value_after_label(lines, [r"\b(?:bill(?:ed)?\s+to|sold\s+to|customer|client|"
                                          r"buyer)\b\s*(?:name)?\s*:?"])
    fields["customer_name"], conf["customer_name"] = customer, (0.75 if customer else 0.0)

    tax_skip = re.compile(r"(?i)(tax\s*(id|no|number|reg|invoice)|\bvat\s*(id|no|number|reg)|"
                          r"gstin|registration|tax invoice)")
    sub = _amount_for(lines, [r"sub\s*-?\s*total", r"net\s+amount", r"total\s+before\s+tax",
                              r"amount\s+before\s+tax"])
    tax = _amount_for(lines, [r"\b(?:sales\s+)?(?:tax|vat|gst)\b"], skip_re=tax_skip)
    total = _amount_for(lines, [r"grand\s+total", r"total\s+due", r"amount\s+due",
                                r"balance\s+due", r"invoice\s+total", r"total\s+amount",
                                r"\btotal\b"],
                        skip_re=re.compile(r"(?i)sub\s*-?\s*total|total\s+(tax|vat|gst)|"
                                           r"total\s+before"))
    for k, v in (("subtotal", sub), ("tax", tax), ("total", total)):
        fields[k] = to_float(v)
        conf[k] = 0.8 if v is not None else 0.0

    cur, cconf = _find_currency(text)
    fields["currency"], conf["currency"] = cur, cconf

    items = _find_line_items(lines)
    return {"fields": fields, "line_items": items, "confidence": conf,
            "method": "heuristic", "notes": notes, "parse_errors": []}


# ============================================================================ LLM + grounding
LLM_SYSTEM = (
    "You are a precise invoice data-extraction engine. Extract ONLY values that are explicitly "
    "written in the document text. Never guess, infer, calculate or invent a value. If a field is "
    "missing, unreadable or ambiguous, return null for it. Respond with a single JSON object and "
    "nothing else (no markdown, no commentary)."
)
LLM_PROMPT = """Extract the invoice fields from the document text below.

Return JSON with exactly this shape:
{{
  "invoice_number": string|null,
  "invoice_date": "YYYY-MM-DD"|null,
  "supplier_name": string|null,      // the company issuing the invoice
  "customer_name": string|null,      // the company/person billed
  "subtotal": number|null,           // before tax
  "tax": number|null,                // total tax amount (not the percentage)
  "total": number|null,              // final amount payable
  "currency": "ISO 4217 code"|null,  // only if a code or an unambiguous symbol is printed
  "line_items": [{{"description": string|null, "quantity": number|null, "unit_price": number|null, "amount": number|null}}],
  "confidence": {{"<field>": number between 0 and 1}},
  "notes": string|null
}}

Rules:
- Numbers: plain numbers, no currency symbols or thousands separators.
- Copy text exactly as printed. Do not translate or correct spelling.
- If two dates look like day/month, use the document's own conventions; if you cannot tell, return null.
- Do not compute totals yourself. If a value is not printed, return null.

DOCUMENT TEXT:
\"\"\"
{text}
\"\"\""""


class LLMExtractor:
    """OpenAI, Groq (OpenAI-compatible endpoint) or Anthropic behind one interface."""

    def __init__(self, provider, api_key, model, base_url=None, timeout=60, max_chars=24000, retries=2):
        self.provider, self.model = provider, model
        self.max_chars, self.retries = max_chars, retries
        if provider == "anthropic":
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
            self._errors = (anthropic.APIError,)
        else:                                   # openai | groq | any OpenAI-compatible server
            import openai
            self.client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
            self._errors = (openai.OpenAIError,)

    def _clip(self, text):
        if len(text) <= self.max_chars:
            return text
        half = self.max_chars // 2          # totals live at the end, header at the start
        return text[:half] + "\n...[middle truncated]...\n" + text[-half:]

    def _complete(self, prompt):
        if self.provider == "anthropic":
            msg = self.client.messages.create(
                model=self.model, max_tokens=4000, system=LLM_SYSTEM,
                messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        resp = self.client.chat.completions.create(
            model=self.model, max_tokens=4000, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": prompt}])
        return resp.choices[0].message.content or ""

    def extract(self, text):
        prompt = LLM_PROMPT.format(text=self._clip(text))
        last = None
        for attempt in range(self.retries + 1):
            try:
                return parse_llm_json(self._complete(prompt))
            except (ValueError, *self._errors) as e:
                last = e
                rate_limited = "429" in str(e) or "rate" in type(e).__name__.lower()
                time.sleep(5 * (attempt + 1) if rate_limited else min(2 ** attempt, 8))
        raise ExtractionError(f"LLM extraction failed: {last}")


def parse_llm_json(raw):
    raw = (raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM returned no JSON object")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM JSON is not an object")
    return data


def _tokens(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).split()


class _Haystack:
    def __init__(self, text):
        toks = _tokens(text)
        self.tokens = set(toks)
        self.flat = " " + " ".join(toks) + " "
        nums = set()
        for raw in re.findall(r"\d[\d.,]*\d|\d", text or ""):
            d = parse_amount(raw)
            if d is not None:
                nums.add(abs(d).quantize(CENT))
        self.numbers = nums
        self.dates = set()
        for m in _DATE_RE.finditer(text or ""):
            iso, _ = normalize_date(m.group(0))
            if iso:
                self.dates.add(iso)
            old = SETTINGS["dayfirst"]
            SETTINGS["dayfirst"] = not old       # accept either reading of a/b/yyyy
            iso2, _ = normalize_date(m.group(0))
            SETTINGS["dayfirst"] = old
            if iso2:
                self.dates.add(iso2)
        self.text = text or ""

    def has_str(self, value):
        toks = _tokens(value)
        if not toks:
            return False
        if " " + " ".join(toks) + " " in self.flat:
            return True
        hit = sum(t in self.tokens for t in toks)
        return len(toks) > 1 and hit / len(toks) >= 0.8     # tolerate 1 OCR slip

    def has_num(self, d):
        return d is not None and abs(d).quantize(CENT) in self.numbers


def ground(llm, text):
    """Convert LLM output to our schema and drop everything not present in the source text."""
    hay = _Haystack(text)
    notes, fields, conf = [], {}, {}
    llm_conf = llm.get("confidence") if isinstance(llm.get("confidence"), dict) else {}

    def c(name, default=0.85):
        try:
            return max(0.0, min(0.95, float(llm_conf.get(name, default))))
        except (TypeError, ValueError):
            return default

    for f in ("invoice_number", "supplier_name", "customer_name"):
        v = llm.get(f)
        v = None if _blank(v) else str(v).strip()[:255]
        if v and not hay.has_str(v):
            notes.append(f"Discarded '{f}' returned by the model: not found in the document text")
            v = None
        fields[f], conf[f] = v, (c(f) if v else 0.0)

    iso, _ = normalize_date(llm.get("invoice_date"))
    if iso and iso not in hay.dates:
        notes.append("Discarded 'invoice_date': not found in the document text")
        iso = None
    fields["invoice_date"], conf["invoice_date"] = iso, (c("invoice_date") if iso else 0.0)

    for f in ("subtotal", "tax", "total"):
        d = parse_amount(llm.get(f))
        if d is not None and not hay.has_num(d):
            notes.append(f"Discarded '{f}': value {d} not found in the document text")
            d = None
        fields[f], conf[f] = to_float(d), (c(f) if d is not None else 0.0)

    cur = llm.get("currency")
    cur = None if _blank(cur) else str(cur).strip().upper()
    if cur and not (re.search(rf"\b{re.escape(cur)}\b", text or "", re.I)
                    or any(code == cur and sym in (text or "") for sym, (code, _) in SYMBOLS.items())):
        notes.append("Discarded 'currency': not printed in the document")
        cur = None
    fields["currency"], conf["currency"] = cur, (c("currency") if cur else 0.0)

    items = []
    for it in llm.get("line_items") or []:
        if not isinstance(it, dict):
            continue
        desc = None if _blank(it.get("description")) else str(it["description"]).strip()[:255]
        row = {"description": desc if desc and hay.has_str(desc) else None}
        for k in ("quantity", "unit_price", "amount"):
            d = parse_amount(it.get(k))
            row[k] = to_float(d, 4) if d is not None and hay.has_num(d) else None
        if row["description"] or row["amount"] is not None:
            items.append(row)
    if llm.get("line_items") and len(items) < len(llm["line_items"]):
        notes.append("Some model line items were dropped because they were not in the text")

    return {"fields": fields, "line_items": items, "confidence": conf,
            "method": "llm", "notes": notes, "parse_errors": []}


def merge(heur, llm):
    """LLM (grounded) wins; agreement raises confidence, disagreement lowers it for review."""
    out = {"fields": {}, "confidence": {}, "notes": list(heur["notes"]) + list(llm["notes"]),
           "method": "heuristic+llm", "parse_errors": []}
    for f, hv in heur["fields"].items():
        lv = llm["fields"].get(f)
        if lv is None:
            out["fields"][f], out["confidence"][f] = hv, heur["confidence"][f]
        elif hv is None:
            out["fields"][f], out["confidence"][f] = lv, llm["confidence"][f]
        elif str(hv).strip().lower() == str(lv).strip().lower() or (
                isinstance(hv, float) and isinstance(lv, float) and abs(hv - lv) < 0.005):
            out["fields"][f], out["confidence"][f] = lv, 0.95
        else:
            out["fields"][f], out["confidence"][f] = lv, 0.55
            out["notes"].append(f"Model and rules disagree on '{f}' ('{lv}' vs '{hv}')")
    out["line_items"] = llm["line_items"] or heur["line_items"]
    return out


def needs_llm(heur):
    f, c = heur["fields"], heur["confidence"]
    if any(f.get(k) is None for k in REQUIRED_FOR_NO_LLM):
        return True
    if not heur["line_items"]:
        return True
    return min((c[k] for k in REQUIRED_FOR_NO_LLM), default=0) < 0.7


class LLMBudget:
    def __init__(self, calls):
        self.left = calls

    def take(self):
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def extract_invoice(text, llm=None, mode="hybrid", conf_scale=1.0, budget=None):
    result = heuristic_extract(text)
    want_llm = (llm is not None and mode != "heuristic" and (mode == "llm" or needs_llm(result)))
    if want_llm and (budget is None or budget.take()):
        try:
            result = merge(result, ground(llm.extract(text), text))
        except Exception as e:  # network/API/JSON problems must not fail the document
            log.warning("LLM extraction skipped: %s", e)
            result["notes"].append(f"LLM unavailable, used rules only ({type(e).__name__})")
    elif want_llm:
        result["notes"].append("LLM call budget for this document exhausted; rules only")
    if conf_scale < 1.0:
        result["confidence"] = {k: round(v * conf_scale, 3) for k, v in result["confidence"].items()}
    return result


# ============================================================================ page segmentation
def segment_pages(pages):
    """Split a multi-invoice PDF into invoice segments: a new segment starts on the first page
    that carries an invoice number different from the current one."""
    segments, current, current_num = [], None, None
    for p in pages:
        if not (p.text or "").strip():
            if current:
                current["pages"].append(p)
            continue
        num = find_invoice_number(p.text)
        if current is None or (num and norm_number(num) != current_num):
            current = {"pages": [p]}
            segments.append(current)
            current_num = norm_number(num) if num else None
        else:
            current["pages"].append(p)
            current_num = current_num or (norm_number(num) if num else None)
    return segments


# ============================================================================ spreadsheets
_HEADER_ALIASES = {
    "invoice_number": ["invoice number", "invoice no", "invoice #", "invoice id", "inv no",
                       "inv number", "inv #", "invoice", "bill no", "document no"],
    "invoice_date": ["invoice date", "date", "issue date", "date issued", "inv date"],
    "supplier_name": ["supplier", "supplier name", "vendor", "vendor name", "seller", "from"],
    "customer_name": ["customer", "customer name", "client", "client name", "bill to", "buyer"],
    "subtotal": ["subtotal", "sub total", "net", "net amount", "amount before tax"],
    "tax": ["tax", "vat", "gst", "tax amount", "vat amount", "sales tax"],
    "total": ["total", "grand total", "total amount", "invoice total", "amount due",
              "total due", "gross", "gross amount"],
    "currency": ["currency", "curr", "ccy"],
    "description": ["description", "item", "item description", "product", "particulars"],
    "quantity": ["quantity", "qty", "units"],
    "unit_price": ["unit price", "price", "rate", "unit cost"],
    "line_amount": ["line total", "line amount", "item total", "amount", "line value"],
}
_ALIAS_LOOKUP = {a: canon for canon, names in _HEADER_ALIASES.items() for a in names}


def _norm_header(v):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#]+", " ", str(v).lower())).strip()


def _find_header(rows):
    best, best_map = None, {}
    for idx, row in enumerate(rows[:25]):
        cmap = {}
        for ci, cell in enumerate(row):
            if _blank(cell):
                continue
            canon = _ALIAS_LOOKUP.get(_norm_header(cell))
            if canon and canon not in cmap.values():
                cmap[ci] = canon
        if len(cmap) >= 2 and (best is None or len(cmap) > len(best_map)):
            best, best_map = idx, cmap
    return best, best_map


def _sheet_invoices(name, df):
    rows = df.values.tolist()
    hidx, cmap = _find_header(rows)
    if hidx is None:
        return [], [f"Sheet '{name}': no recognisable invoice header row"]
    canon = set(cmap.values())
    if "line_amount" in canon and "description" not in canon and "total" not in canon:
        cmap = {i: ("total" if c == "line_amount" else c) for i, c in cmap.items()}
    has_lines = "description" in set(cmap.values())

    groups, order, last, uniq = {}, [], None, 0
    for ridx in range(hidx + 1, len(rows)):
        r = rows[ridx]
        if all(_blank(c) for c in r):
            continue
        rec = {c: r[i] for i, c in cmap.items() if i < len(r)}
        inv_no = rec.get("invoice_number")
        desc = str(rec.get("description") or "").strip().lower()
        if _blank(inv_no) and re.match(r"(sub\s*-?\s*)?total|grand total", desc):
            continue
        if not _blank(inv_no):
            key = str(inv_no).strip()
            group = groups.get(key)
            if group is None:
                group = groups[key] = {"rows": []}
                order.append(key)
            last = group
        elif "invoice_number" in cmap.values() and last is not None:
            group = last
        else:
            uniq += 1
            key = f"__row{uniq}"
            group = groups[key] = {"rows": []}
            order.append(key)
            last = group
        group["rows"].append((ridx + 1, rec))

    out = []
    for key in order:
        grows = groups[key]["rows"]
        parse_errors, fields, conf, notes = [], {}, {}, []

        def first(f):
            for _, rec in grows:
                if not _blank(rec.get(f)):
                    return rec[f]
            return None

        for f in ("invoice_number", "supplier_name", "customer_name"):
            v = first(f)
            fields[f] = None if _blank(v) else str(v).strip()[:255]
        if fields["invoice_number"] is not None and key.startswith("__row"):
            fields["invoice_number"] = None

        raw = first("invoice_date")
        iso, amb = normalize_date(raw)
        if raw is not None and iso is None:
            parse_errors.append({"field": "invoice_date", "raw": str(raw)})
        fields["invoice_date"] = iso
        if amb:
            notes.append(f"Date '{raw}' is ambiguous (day/month order assumed)")

        for f in ("subtotal", "tax", "total"):
            raw = first(f)
            d = parse_amount(raw)
            if raw is not None and d is None:
                parse_errors.append({"field": f, "raw": str(raw)})
            fields[f] = to_float(d)

        raw = first("currency")
        cur = None if _blank(raw) else str(raw).strip()
        if cur and cur in SYMBOLS:
            cur = SYMBOLS[cur][0]
        fields["currency"] = cur.upper() if cur else None

        items = []
        if has_lines:
            for _, rec in grows:
                item = {"description": None if _blank(rec.get("description"))
                        else str(rec["description"]).strip()[:255]}
                for k, src, places in (("quantity", "quantity", 4), ("unit_price", "unit_price", 4),
                                       ("amount", "line_amount", 2)):
                    raw = rec.get(src)
                    d = parse_amount(raw)
                    if not _blank(raw) and d is None:
                        parse_errors.append({"field": f"line_items.{k}", "raw": str(raw)})
                    item[k] = to_float(d, places)
                if any(item[k] is not None for k in item):
                    items.append(item)

        for f, v in fields.items():
            conf[f] = 0.95 if v is not None else 0.0
        if amb:
            conf["invoice_date"] = 0.55
        r0, r1 = grows[0][0], grows[-1][0]
        out.append({"fields": fields, "line_items": items, "confidence": conf,
                    "method": "spreadsheet", "notes": notes, "parse_errors": parse_errors,
                    "source_pages": f"{name}!{r0}-{r1}" if r0 != r1 else f"{name}!{r0}"})
    return out, []


def extract_from_sheets(sheets):
    invoices, warnings = [], []
    for name, df in sheets.items():
        found, warns = _sheet_invoices(name, df)
        invoices.extend(found)
        warnings.extend(warns)
    return invoices, warnings
