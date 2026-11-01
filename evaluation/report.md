# Evaluation report

Dataset: 112 synthetic files (seed 42): digital PDF, scanned PDF, PNG, noisy PNG, XLSX, CSV, 6 injected arithmetic errors, 6 corrupted files. Extraction mode: `hybrid (rules only)`. All numbers below were measured by `evaluation/evaluate.py`.

## Headline

| Metric | Value |
|---|---|
| Files evaluated (valid / total) | 106 / 112 |
| Document success rate | 100.0% |
| Document failure rate | 0.0% |
| Corrupted files failed cleanly | 100.0% |
| Overall field accuracy | 98.6% |
| Invoices with every field correct | 91.5% |
| Line-item amount accuracy | 93.5% |
| Hallucination rate (value given where truth is null) | 0.0% |
| Injected arithmetic errors flagged for review | 100.0% |
| Processing time mean / median / p95 / max | 355 / 26 / 1062 / 1178 ms |

## Accuracy per field

| Field | Accuracy |
|---|---|
| invoice_number | 100.0% |
| invoice_date | 100.0% |
| supplier_name | 98.1% |
| customer_name | 92.5% |
| subtotal | 100.0% |
| tax | 99.1% |
| total | 100.0% |
| currency | 99.1% |

## Accuracy and mean time by source

| Source | Field accuracy | Mean time |
|---|---|---|
| badmath | 100.0% | 27 ms |
| csv | 100.0% | 22 ms |
| img | 100.0% | 930 ms |
| noisy | 85.0% | 974 ms |
| pdf | 100.0% | 20 ms |
| scan | 100.0% | 827 ms |
| xlsx | 100.0% | 28 ms |
