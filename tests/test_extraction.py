from decimal import Decimal

import pytest

import extraction as ex
from tests import helpers as H


def text_of(rows):
    return "\n".join("   ".join(r) for r in rows)


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("$ 99", "99"), ("(45.00)", "-45.00"),
    ("12,5", "12.5"), ("€1 234,50", "1234.50"), ("1,234", "1234"), (1250, "1250"),
    ("USD 10.00", "10.00"), ("-3.2", "-3.2"),
])
def test_parse_amount(raw, expected):
    assert ex.parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["abc", "", None, "N/A", "12abc", float("nan"), True])
def test_parse_amount_rejects_garbage(raw):
    assert ex.parse_amount(raw) is None


def test_normalize_date_variants():
    ex.configure(dayfirst=True)
    assert ex.normalize_date("2024-03-15") == ("2024-03-15", False)
    assert ex.normalize_date("15/03/2024") == ("2024-03-15", False)
    assert ex.normalize_date("03/15/2024") == ("2024-03-15", False)
    assert ex.normalize_date("5 March 2024") == ("2024-03-05", False)
    assert ex.normalize_date("Mar 5, 2024") == ("2024-03-05", False)
    assert ex.normalize_date("04/03/2024") == ("2024-03-04", True)      # ambiguous -> flagged
    assert ex.normalize_date("32/13/2024")[0] is None
    assert ex.normalize_date("not a date")[0] is None


def test_heuristic_extracts_all_fields():
    res = ex.heuristic_extract(text_of(H.invoice_lines()))
    f = res["fields"]
    assert f["invoice_number"] == "INV-2024-0042"
    assert f["invoice_date"] == "2024-03-15"
    assert f["supplier_name"] == "Acme Industrial Supplies Ltd"
    assert f["customer_name"] == "Globex Corporation"
    assert (f["subtotal"], f["tax"], f["total"]) == (190.0, 19.0, 209.0)
    assert f["currency"] == "USD"
    assert len(res["line_items"]) == 3 and res["line_items"][0]["amount"] == 45.0


def test_alternative_labels():
    text = ("Northwind Traders\nTax Invoice\nInvoice #: NW-7781\nDate of Issue: 12 Jan 2024\n"
            "Billed To: Contoso Ltd\nNet Amount 500.00\nVAT 20% 100.00\nTotal Due £600.00\n")
    f = ex.heuristic_extract(text)["fields"]
    assert f["invoice_number"] == "NW-7781" and f["invoice_date"] == "2024-01-12"
    assert (f["subtotal"], f["tax"], f["total"]) == (500.0, 100.0, 600.0)
    assert f["currency"] == "GBP" and f["customer_name"] == "Contoso Ltd"


def test_missing_fields_stay_null_not_invented():
    res = ex.heuristic_extract("Some random memo about lunch plans\nsee you at noon")
    f = res["fields"]
    assert f["invoice_number"] is None and f["total"] is None and f["invoice_date"] is None
    assert f["tax"] is None and f["currency"] is None


def test_tax_id_line_is_not_tax_amount():
    text = "ACME Corp\nInvoice No: A-100\nTax ID: 12-3456789\nTotal 50.00\n"
    assert ex.heuristic_extract(text)["fields"]["tax"] is None


def test_ambiguous_date_lowers_confidence():
    ex.configure(dayfirst=True)
    res = ex.heuristic_extract("ACME\nInvoice No: A-1\nInvoice Date: 04/03/2024\nTotal 5.00")
    assert res["confidence"]["invoice_date"] < 0.6 and res["notes"]


# ---------------------------------------------------------------- grounding (anti-hallucination)
def test_grounding_discards_invented_values():
    text = text_of(H.invoice_lines())
    fake = {"invoice_number": "INV-9999-FAKE", "invoice_date": "2020-01-01",
            "supplier_name": "Acme Industrial Supplies Ltd", "customer_name": "Made Up Co",
            "subtotal": 190.0, "tax": 18.5, "total": 999.99, "currency": "EUR",
            "line_items": [{"description": "Steel bolts M8", "quantity": 100, "unit_price": 0.45, "amount": 45.0},
                           {"description": "Imaginary widget", "quantity": 1, "unit_price": 7.77, "amount": 7.77}],
            "confidence": {"invoice_number": 0.99}}
    g = ex.ground(fake, text)
    f = g["fields"]
    assert f["invoice_number"] is None and f["invoice_date"] is None
    assert f["customer_name"] is None and f["tax"] is None and f["total"] is None
    assert f["currency"] is None                      # EUR is not printed on the invoice
    assert f["supplier_name"] == "Acme Industrial Supplies Ltd" and f["subtotal"] == 190.0
    assert [i["description"] for i in g["line_items"]] == ["Steel bolts M8"]
    assert len(g["notes"]) >= 5


class FakeLLM:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, 0

    def extract(self, text):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


def test_hybrid_skips_llm_when_rules_are_enough():
    llm = FakeLLM({})
    ex.extract_invoice(text_of(H.invoice_lines()), llm, "hybrid")
    assert llm.calls == 0


def test_hybrid_calls_llm_for_weak_text_and_merges():
    text = "Zeta Corp\nInvoice No: Z-55\nDate 3 Feb 2024\nTotal 80.00\n"
    llm = FakeLLM({"invoice_number": "Z-55", "invoice_date": "2024-02-03", "supplier_name": "Zeta Corp",
                   "total": 80, "customer_name": None, "currency": None, "subtotal": None, "tax": None,
                   "line_items": []})
    res = ex.extract_invoice(text, llm, "hybrid")
    assert llm.calls == 1 and res["method"] == "heuristic+llm"
    assert res["fields"]["supplier_name"] == "Zeta Corp" and res["fields"]["total"] == 80.0


def test_llm_failure_falls_back_to_rules():
    res = ex.extract_invoice("Zeta Corp\nInvoice No: Z-55\nTotal 80.00", FakeLLM(error=RuntimeError("boom")), "llm")
    assert res["fields"]["invoice_number"] == "Z-55"
    assert any("LLM unavailable" in n for n in res["notes"])


def test_llm_budget_is_enforced():
    llm, budget = FakeLLM({}), ex.LLMBudget(1)
    for _ in range(3):
        ex.extract_invoice("memo", llm, "llm", budget=budget)
    assert llm.calls == 1


def test_parse_llm_json_tolerates_markdown():
    assert ex.parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        ex.parse_llm_json("no json here")


# ---------------------------------------------------------------- segmentation
class P:
    def __init__(self, n, text):
        self.page_no, self.text = n, text


def test_segment_pages_splits_on_new_invoice_number():
    pages = [P(1, "Invoice No: A-1\nitems"), P(2, "Page 2 of 2\nInvoice No: A-1\ntotal"),
             P(3, "Invoice No: B-2\nitems"), P(4, "")]
    segs = ex.segment_pages(pages)
    assert [[p.page_no for p in s["pages"]] for s in segs] == [[1, 2], [3, 4]]


# ---------------------------------------------------------------- spreadsheets
def test_spreadsheet_groups_rows_into_invoices(tmp_path):
    import processing
    rows = H.sheet_rows() + H.sheet_rows({**H.SAMPLE, "invoice_number": "INV-2024-0043", "total": 209.0})
    parsed = processing.parse_spreadsheet(H.make_xlsx(str(tmp_path / "s.xlsx"), rows), ".xlsx")
    invs, warns = ex.extract_from_sheets(parsed.sheets)
    assert [i["fields"]["invoice_number"] for i in invs] == ["INV-2024-0042", "INV-2024-0043"]
    first = invs[0]
    assert len(first["line_items"]) == 3 and first["fields"]["total"] == 209.0
    assert first["fields"]["invoice_date"] == "2024-03-15" and first["fields"]["currency"] == "USD"
    assert any("Notes" in w for w in warns)          # sheet without a header is reported


def test_spreadsheet_bad_numbers_become_parse_errors(tmp_path):
    import processing
    rows = H.sheet_rows()
    rows[0][10] = "twelve dollars"
    parsed = processing.parse_spreadsheet(H.make_xlsx(str(tmp_path / "b.xlsx"), rows), ".xlsx")
    inv = ex.extract_from_sheets(parsed.sheets)[0][0]
    assert inv["fields"]["total"] is None
    assert {"field": "total", "raw": "twelve dollars"} in inv["parse_errors"]


# ---------------------------------------------------------------- provider plumbing
class _Msg:
    def __init__(self, c): self.message = type("M", (), {"content": c})()


class _FakeOpenAI:
    """Mimics openai.OpenAI().chat.completions.create and records the request."""
    def __init__(self, reply):
        self.reply, self.kwargs = reply, None
        self.chat = type("C", (), {"completions": self})()

    def create(self, **kw):
        self.kwargs = kw
        return type("R", (), {"choices": [_Msg(self.reply)]})()


def test_openai_compatible_extractor_uses_json_mode_and_parses():
    llm = object.__new__(ex.LLMExtractor)
    llm.provider, llm.model, llm.max_chars, llm.retries, llm._errors = "groq", "m", 24000, 0, (OSError,)
    llm.client = _FakeOpenAI('{"invoice_number": "Z-55", "total": 80}')
    assert llm.extract("Invoice No: Z-55 Total 80")["invoice_number"] == "Z-55"
    assert llm.client.kwargs["response_format"] == {"type": "json_object"}
    assert llm.client.kwargs["messages"][0]["role"] == "system"


def test_extractor_raises_after_bad_json():
    llm = object.__new__(ex.LLMExtractor)
    llm.provider, llm.model, llm.max_chars, llm.retries, llm._errors = "openai", "m", 24000, 0, (OSError,)
    llm.client = _FakeOpenAI("sorry, I cannot do that")
    with pytest.raises(ex.ExtractionError):
        llm.extract("anything")
