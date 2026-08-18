# Varavu Selavu / Spending Control — Feature Brief: PDF Statement Parser (docling)

Scope: parse uploaded bank-statement PDFs (including password-protected ones) into normalized transactions that flow into the existing import → transactions → review pipeline. Stack: Python with **docling** for document parsing and **pikepdf** for decryption. UI work reuses the existing design system, toast, and Admin panel patterns.

---

## 1. Pipeline architecture

```
Upload PDF
  → detect encryption
  → [if encrypted] decrypt via pikepdf (stored password / pattern / prompt)
  → docling conversion (table structure = ACCURATE; OCR auto-fallback)
  → extract transaction table(s) per page, merge multi-page tables
  → per-bank normalizer adapter → normalized transaction records
  → balance reconciliation + validation
  → dedupe against existing transactions
  → import summary screen → user confirms → records created
  → uncertain rows land in the Review Queue with reasons
```

Each stage below.

## 2. Decryption stage (pikepdf)

- On upload, attempt to open the PDF; if it raises a password error, it's encrypted.
- Decrypt with pikepdf: `pikepdf.open(path, password=pw)` then `save()` an unencrypted temporary copy. Docling never sees the encrypted file — it always receives the decrypted temp file.
- Password resolution order: (1) the stored password for the selected account; (2) the stored **password pattern** for that bank, evaluated for the current context; (3) inline prompt in the Import UI ("This statement is password-protected — enter the password"), masked input, with "save for this account" checkbox.
- The decrypted temp file is deleted immediately after parsing completes (success or failure). Passwords are NEVER written to logs, error messages, or the parsed output.

## 3. Password management (Admin panel)

Add an "Accounts & statement passwords" section to the existing Admin page (same card pattern as the Logo Manager):

- One row per account: account name + logo, a masked password field ("Statement password"), and an optional "Password pattern" text field with helper text: "e.g. First 4 letters of name + DDMM — used to derive the password automatically."
- Store in the app's existing persistence layer, encrypted at rest if the stack offers a straightforward way (at minimum, never in plaintext config files or client-visible code).
- Masked display (dots) with a reveal-on-hold eye icon; standard toast on save.

## 4. Parsing stage (docling configuration)

- Use docling's PDF pipeline with:
  - **Table structure recognition ON**, mode **ACCURATE** (TableFormer accurate mode) — bank tables are dense; fast mode drops or merges cells.
  - **OCR OFF by default** (bank statements are digitally generated; OCR adds cost for nothing).
  - **OCR auto-fallback:** if a page yields no text layer (scanned statement), re-run that document with OCR enabled and note "OCR used" in the import summary.
- Export detected tables in a structured form (docling's table export → rows/columns; a DataFrame is fine server-side).
- **Multi-page handling:** bank tables continue across pages, usually repeating the header row on each page. Merge continuation tables into one logical table; drop repeated header rows; drop footer junk rows (page numbers, "carried forward" markers — but capture carried-forward amounts if present for reconciliation).
- Also extract from the document text (not the table): statement period (from/to dates), opening balance, closing balance, and account number (for account auto-matching). These usually live in the header block above the table.

## 5. Per-bank normalizer adapters

One adapter per bank/product, registered against the account. Each adapter maps that bank's table columns into the normalized schema and encodes its quirks. The adapters are the only bank-specific code; everything else is generic.

Normalized transaction schema:

- `account_id`
- `date` (ISO; parsed from the bank's own format — the format string is part of the adapter)
- `time` (nullable — only when the statement provides it)
- `description` (the verbatim statement string, untrimmed — this is what rules match against and what the review drawer displays)
- `amount` (signed decimal; **convention: money out = negative, money in = positive**)
- `balance_after` (nullable — running balance where the statement provides it)
- `raw_row` (the original row, retained for debugging/audit)
- `source_file`, `imported_at`
- `dedupe_hash` = hash(account_id, date, amount, normalized description)

Adapter responsibilities:

- Column mapping (which columns are date / description / debit / credit / balance).
- **Debit-credit convention:** banks with separate Dr/Cr columns → merge into the signed amount; banks with a single amount + Dr/Cr flag → apply the flag; **credit-card statements (HDFC Diners): spends are money OUT (negative), payments/refunds are money IN (positive)** — the reverse of how the card statement visually presents them.
- Multi-line descriptions: rows where the description wraps into a following table row with empty date/amount cells → merge into the previous row's description.
- Skip rules: opening-balance rows, section headers, summary/total rows.

**CRITICAL ARCHITECTURE DECISION — adapters are DATA, not code.** Every adapter is a declarative JSON configuration validated against one schema, interpreted by a single generic parsing engine. No adapter is ever a Python function per bank. This is what makes the two creation tracks below produce identical artifacts:

```json
{
  "bank_id": "axis-savings",
  "header_signature": ["Tran Date", "Particulars", "Debit", "Credit", "Balance"],
  "column_map": { "date": 0, "description": 1, "debit": 2, "credit": 3, "balance": 4 },
  "date_format": "DD-MM-YYYY",
  "amount_convention": "separate_debit_credit",
  "decimal_style": "indian",
  "wrap_merge": "empty_date_row_appends_to_previous_description",
  "skip_row_patterns": ["OPENING BALANCE", "TOTAL", "Page \\d+"],
  "statement_meta": { "period_regex": "...", "opening_balance_regex": "...", "closing_balance_regex": "..." }
}
```

(Schema above is illustrative — design the real one to cover all responsibilities listed in this section, including single-signed-amount and Dr/Cr-flag conventions and the credit-card sign flip.)

**Track 1 — Hand-built adapters (launch banks).** The owner will supply 2–3 sample statements for a couple of banks to start, plus field notes and quirks. Build those adapters directly (as JSON configs), test against the samples, ship them.

**Track 2 — every other bank is taught, not programmed** (next section).

A bank with no adapter gets the Adapter Trainer flow — never a garbage import, never a dead end.

## 5b. The Adapter Trainer — teach a new bank in plain language

When a statement is uploaded for a bank with no adapter, the Import page offers: "This bank isn't set up yet — **teach me this statement**." That opens the Trainer, a split-screen workspace:

**Left: the evidence.** The raw table docling extracted, shown as a grid (first 10–15 rows, real data), with column indices visible. Below it, a live **preview of normalized transactions** rendered from the current adapter draft — date, description, signed amount, balance — updating after every change, with a live reconciliation status chip ("Balance check: ✗ off by ₹4,230" → "✓").

**Right: the conversation.** A chat panel where the owner describes the statement in natural language:

> "The second column is the date, format DD/MM/YYYY. Withdrawals are in column 4 and deposits in column 5. Ignore any row that says 'B/F' or 'Carried Forward'. When a row has no date, its text belongs to the transaction above."

**How it works technically:**

1. Each user message is sent server-side to an LLM API (provider-agnostic — a configurable endpoint + key set via environment/Admin; do not hardcode a provider) together with: the adapter JSON schema, the current adapter draft, a sample of the raw extracted rows, and the user's instruction.
2. System prompt contract: *"You convert instructions about a bank-statement table into an adapter configuration. Respond ONLY with the complete updated adapter JSON, valid against the schema. Change only what the instruction requires."* Strip code fences; validate the response against the schema; on invalid output, retry once, then show a friendly "I didn't get that — try rephrasing" in the chat.
3. Apply the returned config → re-render the normalized preview and reconciliation chip instantly. The user iterates conversationally until the preview looks right.
4. The assistant side of the chat can also *ask*: after the first parse, it should proactively state what it inferred ("I think column 1 is the date in DD-MM-YY — correct?") so the owner confirms rather than authors from scratch.
5. **Save gate:** "Save adapter" is enabled only when the reconciliation check passes on the training statement (opening + Σ = closing). Override is possible with an explicit warning and marks the adapter "unverified". Saved adapters are stored with the account/bank mapping and used automatically for all future uploads.
6. **Retraining:** an existing adapter can be reopened in the Trainer at any time (bank changed their layout) — same flow, seeded with the current config. Keep prior versions so a bad edit can be reverted.
7. The Trainer never mutates real data: nothing is imported until the adapter is saved and the normal import flow (summary screen → confirm) runs.

**Why this works:** the LLM never parses statements in production (costly, non-deterministic) — it only writes configuration during training. Production parsing stays deterministic, fast, and free: docling + the saved JSON. The natural-language interface is a config editor wearing a friendly face.

## 6. Validation & reconciliation (the trust layer)

After normalization, before anything is written:

1. **Balance reconciliation:** opening balance + Σ(all parsed amounts) must equal closing balance (both extracted from the statement itself). Match → show a green "Balance check ✓" in the summary. Mismatch → the import is flagged: show the discrepancy amount, do not silently import; the user may still force-import, and the whole batch is marked "reconciliation failed" for the Review Queue. (Credit-card adapter reconciles against previous balance → total dues equivalently.)
2. Every date must fall within the statement period ± 3 days; outliers → that row goes to review with reason "date outside statement period".
3. Any row that fails to parse (unparseable date, no amount) is NOT dropped: it's imported as a review item with reason "couldn't parse this row" and the raw row text attached, so nothing is ever silently lost.
4. **Dedupe:** rows whose `dedupe_hash` already exists for that account are skipped and counted ("12 duplicates skipped") — re-uploading the same statement must be a no-op.

## 7. Import UX (extends the existing Import page)

1. Drop zone accepts PDF (in addition to whatever exists today). On drop: account auto-match via account number found in the PDF or filename hint; if ambiguous, an account picker appears.
2. If encrypted and no stored password works → inline password prompt (masked, with save-for-account checkbox).
3. Parse progress: skeleton/progress state with stage labels ("Reading statement… Extracting transactions… Checking balances…").
4. **Import summary screen** before committing: "Vignesh Axis Bank · 1–31 July 2026 · 42 transactions found · ₹4,03,143 in / ₹2,01,926 out · Balance check ✓ · 12 duplicates skipped · 2 rows need review". Buttons: primary "Import 42 transactions", quiet "Cancel". Numbers in `--font-num`, statuses as the standard chips.
5. On confirm: standard toast; the coverage beacon recomputes for the active month (the imported month's account flips to Represented if it's the viewed month); flagged rows appear in the Review Queue.
6. Failure states are kind and specific: wrong password ("That password didn't open it — try again"), unsupported layout ("Couldn't find a transaction table in this file"), reconciliation mismatch (shows the ₹ discrepancy). Never a raw stack trace or library error string in the UI.

## 8. Inputs the owner will supply (build against these)

1. **For the launch banks (a couple of banks to start):** 2–3 recent sample statements each — including at least one multi-page statement — plus field notes (date format, Dr/Cr convention, description wrapping, balance column, where period and opening/closing balances appear) and known quirks. These get hand-built Track-1 adapters.
2. **For all remaining banks:** nothing up front — they'll be taught through the Adapter Trainer when their first statement is uploaded.
3. Passwords per account and/or the bank's password pattern (via the Admin section, any time).
4. Filename/account-number hints for auto-matching.
5. An LLM API endpoint + key for the Trainer (configured via environment or Admin, never hardcoded).

## 9. Acceptance tests

- Encrypted sample statement + stored password → parses with no prompt; wrong stored password → inline prompt appears; password never appears in any log or error.
- Decrypted temp files are gone after import (success and failure paths).
- Each supplied sample statement imports with Balance check ✓ and the correct transaction count.
- Re-importing the same statement imports 0 and reports all rows as duplicates.
- A deliberately corrupted row (edit a sample) arrives in the Review Queue with "couldn't parse this row" and its raw text — not silently dropped.
- Credit-card sample: spends land negative, payments positive.
- A scanned (image-only) PDF triggers the OCR fallback and still parses.
- Import summary shows before commit; Cancel imports nothing.
- **Trainer:** uploading a statement from an unconfigured bank offers "teach me this statement"; a plain-language instruction ("column 2 is the date, DD/MM/YYYY; debits in column 4") visibly updates the normalized preview and reconciliation chip; invalid LLM output produces a friendly retry, never a crash.
- **Trainer save gate:** Save is disabled until reconciliation passes on the training statement; override marks the adapter "unverified"; a saved adapter parses the NEXT month's statement from that bank with no retraining.
- Reopening an adapter in the Trainer seeds it with the current config; reverting restores the previous version.
- Both adapter tracks produce configs valid against the same schema; the production parser contains zero bank-specific code branches.
