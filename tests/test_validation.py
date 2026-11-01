from decimal import Decimal

from validation import cross_verify, needs_review, validate_invoice


def good(**over):
    inv = {"invoice_number": "A-1", "invoice_date": "2024-03-15", "supplier_name": "Acme",
           "customer_name": "Globex", "subtotal": 190.0, "tax": 19.0, "total": 209.0, "currency": "USD",
           "line_items": [{"description": "x", "quantity": 100, "unit_price": 0.45, "amount": 45.0},
                          {"description": "y", "quantity": 10, "unit_price": 14.5, "amount": 145.0}],
           "confidence": {}, "parse_errors": []}
    inv.update(over)
    return inv


def codes(issues, sev=None):
    return {(i["code"], i["field"]) for i in issues if sev is None or i["severity"] == sev}


def test_clean_invoice_has_no_issues():
    assert validate_invoice(good()) == []


def test_missing_required_fields_are_errors():
    issues = validate_invoice(good(invoice_number=None, supplier_name="", total=None))
    assert {("MISSING_FIELD", "invoice_number"), ("MISSING_FIELD", "supplier_name"),
            ("MISSING_FIELD", "total")} <= codes(issues, "error")
    assert needs_review(issues)


def test_missing_optional_fields_are_warnings_only():
    issues = validate_invoice(good(customer_name=None))
    assert codes(issues) == {("MISSING_FIELD", "customer_name")} and not needs_review(issues)


def test_total_mismatch():
    assert ("TOTAL_MISMATCH", "total") in codes(validate_invoice(good(total=215.0)), "error")


def test_tolerance_allows_rounding():
    assert validate_invoice(good(total=209.01)) == []


def test_subtotal_vs_line_items():
    assert ("SUBTOTAL_MISMATCH", "subtotal") in codes(validate_invoice(good(subtotal=200.0, total=219.0)), "error")


def test_line_item_arithmetic():
    bad = good()
    bad["line_items"][0]["amount"] = 50.0
    assert ("LINE_ITEM_MISMATCH", "line_items[1]") in codes(validate_invoice(bad), "error")


def test_negative_and_zero_amounts():
    assert ("INVALID_NUMBER", "total") in codes(validate_invoice(good(total=-5.0)), "error")
    assert ("INVALID_NUMBER", "total") in codes(validate_invoice(good(subtotal=None, tax=None, total=0.0,
                                                                      line_items=[])), "warning")


def test_parse_errors_from_extractor():
    issues = validate_invoice(good(total=None, parse_errors=[{"field": "total", "raw": "abc"},
                                                             {"field": "invoice_date", "raw": "32/13/2024"}]))
    assert {("INVALID_NUMBER", "total"), ("INVALID_DATE", "invoice_date")} <= codes(issues, "error")


def test_future_and_invalid_dates():
    assert ("FUTURE_DATE", "invoice_date") in codes(validate_invoice(good(invoice_date="2999-01-01")))
    assert ("INVALID_DATE", "invoice_date") in codes(validate_invoice(good(invoice_date="garbage")), "error")


def test_invalid_currency():
    assert ("INVALID_CURRENCY", "currency") in codes(validate_invoice(good(currency="dollars")), "error")


def test_exact_duplicate_is_error_partial_is_warning():
    dup = {"supplier_name": "ACME Ltd.", "total": 209.0, "filename": "old.pdf"}
    assert ("DUPLICATE_INVOICE", "invoice_number") in codes(validate_invoice(good(), [dup]), "error")
    other = {"supplier_name": "Totally Different", "total": 5.0, "filename": "old.pdf"}
    issues = validate_invoice(good(), [other])
    assert ("POSSIBLE_DUPLICATE", "invoice_number") in codes(issues, "warning") and needs_review(issues)


def test_low_confidence_triggers_review():
    issues = validate_invoice(good(confidence={"total": 0.3, "supplier_name": 0.9}))
    assert codes(issues) == {("LOW_CONFIDENCE", "total")} and needs_review(issues)


def test_decimal_precision_no_float_noise():
    inv = good(subtotal=10.1, tax=0.2, total=10.3, line_items=[])
    assert validate_invoice(inv, tol=Decimal("0.001")) == []


def test_cross_verify_detects_differences():
    a = [{"id": 1, "invoice_number": "INV-1", "supplier_name": "Acme Ltd", "total": 100.0, "tax": 10.0,
          "subtotal": 90.0, "invoice_date": "2024-01-01", "currency": "USD"},
         {"id": 2, "invoice_number": "INV-2", "total": 5.0}]
    b = [{"id": 9, "invoice_number": "inv 1", "supplier_name": "ACME", "total": 110.0, "tax": 10.0,
          "subtotal": 90.0, "invoice_date": "2024-01-01", "currency": "USD"},
         {"id": 10, "invoice_number": "INV-3", "total": 7.0}]
    r = cross_verify(a, b)
    assert r["summary"] == {"compared": 1, "identical": 0, "with_differences": 1, "only_in_a": 1, "only_in_b": 1}
    assert [d["field"] for d in r["matched"][0]["differences"]] == ["total"]
