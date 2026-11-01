# AI prompts and disclosure of AI-assisted development

## Disclosure

This project was built with AI assistance. The AI assistant used was **Claude (Anthropic)**, through a chat
interface. The assistant generated the architecture, the source code (Flask backend, extraction/validation logic,
React frontend), the automated tests, the evaluation script, the diagrams and the documentation. The candidate
directed the work with the prompts below, chose the stack (Flask, OpenAI/Groq), and is responsible for the submission.

What was verified by actually running things (not just read):

- the automated test suite (`pytest`, results in `tests/results.txt`);
- real OCR with Tesseract on generated scans and noisy images;
- a live server with background jobs, driven through a real browser (Playwright), which also produced the walkthrough video;
- the evaluation script, whose measured output is in `evaluation/report.md`.

What was **not** verified: calls to the live OpenAI / Groq API (no key was available during development). The LLM code path is
covered by unit tests that use a stand-in client, and the app falls back to rules-only extraction when no key is configured.
Run the evaluation with your key set before quoting LLM-assisted accuracy.

> Candidate: add here, in your own words, what you reviewed, changed or tested yourself.

## Prompts given to the AI assistant (development)

1. *(Attached the assignment PDF "Senior AI Engineer — AI-Powered Document Intelligence System")*
   "I want to build the project using flask where app.py, requirements.txt, templates(index.html), .env etc will be in the project."
2. "can you change the api key from ANTHROPIC_API_KEY to openai api? I have openai api, not anthropic. but if you think the quality
   of the work won't effect at all, then we can use free one like GROQ api. also do i need to download tesseract.exe for TESSERACT_CMD?"
3. "i need to submit [the nine deliverables]? if yes, then provide me those. also it is vanilla JS in `templates/index.html`, not React or Next.js.
   but it instruct 'Build a simple React or Next.js'. please follow the instruction of the pdf."

## Prompts used by the running application (verbatim from `extraction.py`)

These are sent to the configured LLM (OpenAI by default, Groq or Anthropic optional) only when the rule-based extractor is not
confident (`EXTRACTION_MODE=hybrid`) or always (`EXTRACTION_MODE=llm`). The provider is called in JSON mode.

### System prompt

```
You are a precise invoice data-extraction engine. Extract ONLY values that are explicitly written in the document text. Never guess, infer, calculate or invent a value. If a field is missing, unreadable or ambiguous, return null for it. Respond with a single JSON object and nothing else (no markdown, no commentary).
```

### User prompt template (`{text}` is replaced by the document text)

```
Extract the invoice fields from the document text below.

Return JSON with exactly this shape:
{
  "invoice_number": string|null,
  "invoice_date": "YYYY-MM-DD"|null,
  "supplier_name": string|null,      // the company issuing the invoice
  "customer_name": string|null,      // the company/person billed
  "subtotal": number|null,           // before tax
  "tax": number|null,                // total tax amount (not the percentage)
  "total": number|null,              // final amount payable
  "currency": "ISO 4217 code"|null,  // only if a code or an unambiguous symbol is printed
  "line_items": [{"description": string|null, "quantity": number|null, "unit_price": number|null, "amount": number|null}],
  "confidence": {"<field>": number between 0 and 1},
  "notes": string|null
}

Rules:
- Numbers: plain numbers, no currency symbols or thousands separators.
- Copy text exactly as printed. Do not translate or correct spelling.
- If two dates look like day/month, use the document's own conventions; if you cannot tell, return null.
- Do not compute totals yourself. If a value is not printed, return null.

DOCUMENT TEXT:
"""
{text}
"""
```

### Safeguards around the model output

The model is never trusted directly. Every returned string, number, date and currency must literally appear in the source text
(`extraction.ground`); anything that does not is discarded and noted, so missing information stays `null`. When the model and the rules
disagree on a field, confidence drops to 55 % and the invoice is flagged for manual review.
