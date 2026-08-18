# BUILD_NOTES.md

## 2026-08-18 — Nanny salary + Loan EMI rules (regex match type), broader boot backfill

- Rule engine: single `rule_matches()` used by `apply_rules()` and `rule_conflicts()`; new match type **`regex`** (case-insensitive `re.search`; invalid pattern never matches). Rules page offers it.
- New built-in rules (0.9, outrank "Family transfers" 0.86): `Nanny salary` (`SENTIMPS.*NANNY NAME` → Home & Utilities / Household Help & Services, fixed, spend, note "Nanny salary" via `DEFAULT_RULE_NOTES`) and `Home loan EMI (Jananiya)` (`NEFT.*JANANIYA R/BANK OF` → Home & Utilities / Loan EMI, fixed, spend). Other Jananiya/Sujatha transfers still hit Family transfers.
- Startup backfill now also re-runs rows still bound to a rule (`category IS NULL OR rule_id != ''`), so rules that were edited or outranked re-file their rows on the next boot; manual overrides and import-provided categories are never touched. Pattern-sync skips regex/exact rules.

## 2026-08-18 — Bank internal transfers · additive default-rule patterns · "Groceries" subcategory

- New category **Bank internal transfers** (`HIDDEN_CATEGORIES`): the `FD auto-sweep` rule (`SWEEP TRANSFER|SWEEP TRF|FD PREMAT PROCEEDS`, flow `transfer`, excluded) now assigns it; `RULE_UPDATES` sets it on existing DBs. Rows in a hidden category are dropped from `dashboard_data()` before any sum and excluded from `breakdown()` **regardless of flow** (so a manual move there also disappears from rollups). They stay in the Transactions ledger and are filterable/assignable (listed in the Category dropdown and drawer).
- **Default-rule patterns are now additive on boot:** `seed_defaults()` merges each `DEFAULT_RULES` pattern into the DB row's pattern (case-insensitive union, DB alternatives first) so new built-in alternatives reach existing DBs while user-added ones survive. This is what was keeping `BLINK COMMERCE PVT …` rows uncategorised (the DB still had `BLINKIT|INSTAMART|FIRSTCLUB|ZEPTO`); the startup backfill then categorises them.
- Subcategory **"Groceries / Quick Commerce" → "Groceries"** via `TAXONOMY_MIGRATIONS` (rules, transactions, manual_overrides, baselines) + taxonomy/rules/seed sample updated.
- Verified on a copy of the real DB: 82 rows → Bank internal transfers, 0 SWEEP/FD left uncategorised, Blink Commerce rows → Groceries & Household / Groceries, no old label anywhere, rollups exclude the transfers.

## 2026-08-18 — Transactions: count, multi-select Flow, sticky filters · Income categories · Dashboard inflow sources + click-through

- **Transactions heading** shows the filtered count: `Transactions (130)` (all pages).
- **Flow filter is an Excel-style multi-select** (`render_multi_select` + `MULTI_SELECT_SCRIPT`): button + panel with an "Any flow" master checkbox (all/none/indeterminate), one checkbox per flow, Apply/Clear. URL `flow=spend,fee` (comma-joined; `flow_values()` validates; all flows selected == no filter). Chip "Flow: Money out, Fee".
- **Sticky filter card** (≥768px): `.tx-filter-card{position:sticky; top:var(--header-h)+8px}` with a stuck-shadow via an IntersectionObserver sentinel. On mobile the form collapses in a `<details>` ("Filters · N active", open when filters are active) and a floating "Filters" button appears once the card scrolls away. **Global fix:** removed `overflow-x:hidden` from `body` — it made `body` the scroll container so *no* `position:sticky` (including the header) actually stuck; `html{overflow-x:hidden}` still clips.
- **Income categories** `INCOME_CATEGORIES = ["Salary", "Dividend income"]` (chips/filters/drawer list them; the money-out breakdown, Baselines and creep never do). Rules: `Salary` (contains `EMPLOYER PAYROLL`) → Salary; `Dividend income` (`NACH-ECS-CR|NACH-10-CR`) → Dividend income; `Interest received` (`INT.PD:`) stays uncategorised income. `RULE_UPDATES` in `seed_defaults()` migrates existing DBs (sets rule_salary's category; deletes the old uncustomised `rule_dividends___interest`); the startup backfill now re-runs every uncategorised, non-overridden row (rule or not), so old salary/dividend rows pick up their categories on the next boot.
- **Dashboard:** `dashboard_data()` returns `income_flows` (money-in by category, uncategorised → "Other inflow") and a real `summary.dividends` (4th KPI tile). `/api/sankey` puts one node per income source on the left (nodes carry `kind` + `category`; links unrounded so totals stay exact). Waterfall starts with one bar per income source; treemap/bars get an "Inflow" legend row. All charts are clickable: category → `/transactions?month=…&category=…`, income source → `…&flow=income`; Surplus/Shortfall aren't links.

## 2026-08-18 — "Pocket change": small uncategorised money-out is auto-filed

- New parent category **Pocket change** (`POCKET_CHANGE`, chip slug `pocket`, icon `coins`). `apply_rules()` fallback: only when **no rule matched**, `flow_type ∈ {spend, fee}`, `amount < 0` and `|amount| < threshold` → category Pocket change, classification `controllable`, `rule_id=rule_pocket_change`, confidence 0.8 (so no review item). Money-in, transfers, card payments never qualify; real rules, "Remember" rules and manual overrides always win. A category supplied by the import itself is never replaced by the fallback (guard in `reapply_rules`).
- Threshold: setting `pocket_change_threshold` (default ₹200, 0 = off) via `pocket_change_threshold()` / `set_pocket_change_threshold()`. Admin page card "Pocket change threshold" → `POST /admin/pocket-change` validates, saves, runs `reapply_rules()` and reports how many rows were re-filed. Lowering/disabling the threshold puts engine-filed Pocket change rows back to uncategorised (review item rebuilt); manually overridden rows are untouched.
- **Startup backfill:** `init_db()` now runs `reapply_rules(conn, only_uncategorised=True)` on every boot (audit `startup_backfill` when anything changed), so rows imported before a rule or the Pocket change fallback existed get filed without pressing Save. Only uncategorised, rule-less, non-overridden rows are touched; a row you sent back for review (override with `category=''`) stays uncategorised because overrides win.
- Breakdown panel hides a lone "(no subcategory)" child row.
- Tests: `tests/test_rules_engine.py` (fallback boundaries, money-in/transfer exclusion, rule precedence, reapply on threshold change, import-provided category guard), `tests/test_transactions_page.py` (filter/breakdown/admin endpoint round-trip). Existing tests that used −100 unknown rows now use amounts above the threshold.

## 2026-08-18 — Edit drawer: "Uncategorised — send back for review"

- Category combobox (Transactions + Review drawers) has a pinned dashed option **Uncategorised — send back for review**. Picking it clears category+subcategory (subcategory input disabled), keeps classification/flow, unchecks+disables "Remember" (note: "Can't remember an unknown"), and submits `action=uncategorise`.
- `/review` POST `action=uncategorise`: override stores `category=''`/`subcategory=''` (an override NULL means "no change"; `NULLIF(…,'')` in `EFFECTIVE_TX_COLUMNS` turns it back into None everywhere), does **not** resolve review items, and inserts one with reason `manual_uncategorised` ("Sent back for review") via `create_review_item`. No rule is created. Toast: "Sent back for review" (Transactions) / "Kept in review — marked as not sure" (Review, row stays in the queue).
- Review GET now builds items from the effective CTE (`effective_tx_sql`) instead of raw `t.*`, so a row sent back doesn't show its old category as the "best guess"; visibility/seed filtering unchanged.

## 2026-08-18 — Edit drawer: combobox shows all options; "Remember" conflict preview + supersede

- Category/Subcategory comboboxes (Review + Transactions drawers): when the text is empty or exactly equals a known option the **full list** is shown with the current one highlighted (was: filtered down to itself, so a pre-filled field looked like it had no other options). Focus selects the text; a chevron caret marks it as a dropdown.
- `rule_conflicts()` (next to `apply_rules`) lists every enabled rule that would match a merchant, in engine order, with `same_outcome` / `remembered` flags. `GET /api/rule-conflicts?transaction_id&category&subcategory` exposes it.
- Both drawers: turning on "Remember for future matches" (or changing category/subcategory while it's on) fetches the preview and shows an amber note — *Overrides '<rule>' (…)* / *Replaces your earlier rule (…)* / *Already covered by '<rule>'*. The remember copy now links to Rules → Re-apply (re-apply stays a manual action).
- `/review` POST remember branch: an older remembered rule for the same merchant + account is disabled (`supersede_rule_from_remember` audit) before the new one is written, so two 0.94 rules never tie. JSON gains `superseded` and `conflicts`; existing keys unchanged. Toasts mention replaced rules.

## 2026-08-17 — Transactions page revamp (drill-down breakdown + editable, paginated table)

`/transactions` is now the drill-down for the Dashboard's category split.

Changes:
- New **Breakdown** card between the filters and the table with a `Category | Account | Person` switch. Category rows expand into subcategory rows (incl. a "(no subcategory)" bucket); every row is a link that applies/removes that facet as a filter. Money-out basis (spend+fee net of refund/reversal, clamped ≥0) is identical to `dashboard_data`, so the totals match the Dashboard. The displayed facet's own filter is ignored by the panel so the list never collapses to one row.
- Page defaults to the Dashboard's session month (`self.active_month()`); `?month=all` shows everything. Transactions never writes the session month.
- New filters: Subcategory (cascades from Category) and Payer. Active-filter chips remove one param and keep the rest.
- Table moved to the bottom, paginated 50/page with a pager, sortable by Date / Amount (|amount|).
- Click any row → edit drawer (category / subcategory / classification / note, "remember" rule toggle, transfer/exclude). Posts to `/review` with `origin=transactions`; the endpoint now edits against the *effective* row (earlier override fields survive), accepts `classification`, and treats an empty subcategory as "clear" (stored as `''`, normalised to NULL by the overlay). Review-page behaviour is unchanged. Drawer CSS is shared via `DRAWER_CSS` / `DRAWER_MOBILE_CSS`; a `.exclude-confirm[hidden]` rule fixes the confirm strip that was always visible.
- `effective_transactions()` no longer does an N+1 override query: `effective_tx_sql()` LEFT JOINs the latest override in SQL (`created_at DESC, rowid DESC`), and `query_transactions()` / `breakdown()` / `tx_present_values()` / `taxonomy_options()` all build on it — so deleted/excluded batches and the seed batch stay hidden in every new query. Index `ix_manual_overrides_txn` added in `ensure_schema`.

Tests: `tests/test_effective_transactions.py`, `tests/test_transactions_query.py`, and `tests/test_transactions_page.py` (existing filter tests now pass `month=all` because of the default-month change).

## 2026-08-11 — Import history UI redesign

Implemented the attached Import Statements UI/UX redesign on `/import`, focused on the Import history table.

Changes:
- Replaced the old history table structure with `File | Account | Status | Rows | Imported | Included | Actions`.
- Removed the old mixed Controls column and old Start/End/Type/Duplicates history columns from the visible table.
- File cells now show a file icon plus one-line filename with ellipsis/tooltip behavior.
- Status now has one primary badge with smaller muted supporting notes below it.
- Rows now show a count only for successfully processed imports; parser-needed/failed/processing states show `—`, so `0` only means a real processed zero.
- Imported timestamps now render on one compact line like `11 Aug 2026, 13:52`.
- Inclusion state is its own column with explicit `Included` / `Excluded` text plus a switch. Failed/parser-needed imports render as `Excluded` with a disabled switch and explanatory tooltip.
- Replaced visible Import-screen terminology from Hidden/Hide to Included/Excluded, including the explanatory copy.
- Actions now have stable positions: an always-visible Retry button and a subtle icon-only Delete control with confirmation. Retry is disabled with tooltip unless the import is failed/parser-needed.
- Added a small optimistic retrying state on Retry submit and a reversible exclusion notice with Undo.
- Tightened Import statement card spacing and renamed the primary action to `Import statement`.

Verification:
- `python3 -m py_compile app.py` passed; the two pre-existing embedded-JavaScript invalid-escape warnings remain harmless.
- Restarted the app detached on `0.0.0.0:5010`.
- Authenticated `/`, `/import`, `/transactions`, `/review`, `/admin`, and `/api/sankey?month=2026-07` returned 200.
- Rendered `/import` contains the requested headers and does not contain the old history headers `Controls`, `Duplicates`, `Type`, `Start`, or `End`.
- Rendered `/import` uses Included/Excluded language and no longer shows Hidden/Hide from dashboard terminology.
- The real Kotak `needs_parser` row renders Rows as `—`, Included as `Excluded`, the switch disabled with the required tooltip, Retry enabled, and Delete icon-only with confirmation.
- Disposable committed import verification confirmed Included → Excluded hides rows from Transactions, Undo appears, Include restores rows, and Delete/archive hides rows again. Disposable records were cleaned from the database afterward.
- Rendered inline JavaScript for Import passed `node --check`.

## 2026-08-11 — Active month and account coverage overlay bug fixes

Fixed exactly the two requested defects in `app.py`.

Changes:
- Added one server-side active-month value per authenticated session. A new login defaults it to the previous completed calendar month (July 2026 on 11 August 2026). Dashboard requests without `month` use that value; an explicit dashboard `?month=YYYY-MM` updates it. Every page now renders its beacon count, state, coverage title/rows, and import links from the same session month.
- Split the beacon button from its coverage overlay markup. The scrim and panel are now top-level body children after the header and before `main`, rather than descendants of the header/beacon wrapper.
- Made the desktop panel viewport-fixed and positioned it from the beacon's `getBoundingClientRect()` when opened. It repositions on viewport resize/scroll, has a viewport-derived maximum height, internal vertical scrolling, and an overlay z-index above page content. The mobile fixed bottom sheet retains its animation/dismissal behavior and now also has a viewport cap/internal scrolling.
- Preserved scrim click, outside click, Escape, close-button dismissal, spring/row animation, mobile bottom-sheet behavior, and existing Import now links.

Verification:
- `python3 -m py_compile app.py` passed; the two pre-existing embedded-JavaScript invalid-escape warnings remain harmless.
- Restarted the live app on `0.0.0.0:5010`; it remains running there.
- On a fresh authenticated session, `/` rendered Dashboard month `2026-07` and `Account coverage — July 2026`. `/transactions`, `/review`, `/rules`, `/baselines`, `/import`, and `/admin` each returned 200 and rendered the same July 2026 coverage title.
- In that session, requesting `/?month=2026-08` rendered August 2026 and a subsequent `/transactions` request also rendered `Account coverage — August 2026`, confirming navigation persistence.
- Generated-markup checks confirmed the scrim/panel occur after `</header>` and before `<main>`, and confirmed `position:fixed`, `max-height`, `overflow-y:auto`, overlay z-index ordering, and beacon `getBoundingClientRect()` anchoring.

Limitation:
- No browser automation binary was available for a visual 600px-height/mobile interaction pass. Overlay behavior was verified through compiled code, generated HTML/CSS/JS markers, session-level HTTP checks, and route status checks.

## 2026-08-11 — Seed data setting

Implemented a persistent Admin setting for demo visibility without deleting transactions.

Changes:
- Added an iPhone-style `Use seed data` switch beside the existing Bank logos manager. A new database defaults the setting to enabled; upgraded databases with a real statement default it to disabled.
- Seed visibility now consistently filters the dashboard calculations and previews, Cash flow API, Transactions, Review queue, account coverage, month bounds/empty states, category suggestions, and Recent imports.
- Uploading a CSV, JSON, PDF, or other statement automatically disables seed data while retaining every real/uploaded row. Web uploads show the unified bottom toast `Seed data has been removed from the dashboard.` for 1.8 seconds.
- The switch can explicitly restore or hide demo rows later. Demo transactions remain stored under the seed import batch; no destructive deletion is used.
- Preserved the existing auth, calculations, review workbench/confetti, skeletons/transitions, unified toast, rules/baselines, and bank-logo upload/remove behavior.

Verification:
- `python3 -m py_compile app.py` passed (the two pre-existing embedded-JavaScript invalid-escape warnings remain harmless).
- A temporary fresh database started with 16 visible seed transactions and the switch enabled. Importing one real CSV row automatically disabled seed data; the visible result contained only that real row while all 16 demo rows remained stored. Manually re-enabling showed 17 rows and disabling again returned to the single real row.
- Restarted the app on `0.0.0.0:5010`; authenticated checks returned 200 for `/`, `/transactions`, `/review`, `/rules`, `/baselines`, `/import`, and `/admin`.
- Follow-up route verification returned 200 for `/`, `/?month=2026-05`, `/transactions`, `/review`, `/rules`, `/baselines`, `/import`, `/admin`, and `/api/sankey?month=2026-05`.
- `/admin/seed-data` returned 200 and persisted both states. Admin HTML contains the human label and designed switch.
- Rendered inline JavaScript for Dashboard, Admin, Review, and Import passed `node --check`.
- Verification left the live seed setting enabled because the live database has no real statement batches yet, preserving 5 open review transactions.

## 2026-08-11 Spending Control Brief 3.3 — Pre-fix audit (captured before code changes)

| Page | Element | Rule broken (1–8) | Proposed fix |
|---|---|---:|---|
| Dashboard | Review queue empty state | 4 | Add a friendly action button linking to statement import. |
| Dashboard | Month popover and amount presentation | ✓ clean | Preserve the existing human month picker and numeric styling. |
| Transactions | Table ID/date/flow/classification columns; empty path | 1, 2, 3, 4, 6, 8 | Remove ID, human-format dates and coded values, wrap amounts in numeric styling, and add a designed empty state with Import action. |
| Review | Queue empty state; remembered-match switch | 4, 5 | Add an Import action and give the custom switch the standard focus ring. |
| Review drawer | Detail evidence/reason chips/actions | ✓ clean | Preserve human dates/reasons and inline Exclude confirmation. |
| Rules | Visible rule ID, raw match/classification/status values, native-looking selects, headings, empty path | 2, 3, 4, 5, 8 | Remove ID, humanize all codes/status, style selects, use sentence case, and add a designed empty state. |
| Baselines | Visible baseline ID, raw effective month/source, native-looking select, headings, empty path | 1, 2, 3, 4, 5, 8 | Remove ID, humanize month/source, replace visible raw month with human month picker backed by a hidden canonical value, style controls, use sentence case, and add empty state. |
| Import | Visible batch ID, raw month/dates/status/type, raw date/month fields and placeholders, native-looking source select/file control, headings, empty path, result notices expose batch IDs | 1, 2, 3, 4, 5, 6, 8 | Remove IDs, format month/dates/status/type, add human month/date controls backed by canonical hidden values, style controls, use sentence case, add empty state, and remove IDs from success notices. |
| Admin | Logo manager list and upload/remove controls | ✓ clean | Preserve inline Remove confirmation, styled upload controls, and existing feedback. |
| Login | Login form controls and copy | ✓ clean | Preserve shared styled fields/buttons and focus ring. |
| Account garden overlay | Account list and actions | ✓ clean | Preserve human month, status phrases, and Import actions. |
| Toasts | App-wide toast | ✓ clean | Preserve the single designed toast; no browser dialogs. |
| Skeletons | Page/table/form/review drawer skeletons | ✓ clean | Preserve Brief 3.2 skeleton and reduced-motion behavior. |

### Brief 3.3 fixes

- Added shared `human_month`, `human_date`, code-label, empty-state, human month/date control, and Indian-number presentation helpers.
- Removed visible transaction/rule/baseline/import identifiers and humanized every audited date, month, classification, flow, match, source, file-type, and status value.
- Added designed empty paths for Transactions, Rules, Baselines, Import history, and Review, with actions where useful.
- Restyled selects and file controls, extended the standard focus ring to the custom Review switch, and retained hidden canonical fields for persistence.
- Preserved all hidden DB keys, finance calculations, auth, review behavior, logo persistence, skeletons, transitions, toasts, and inline destructive confirmations.

### Resolved audit

| Page | Element | Rule broken (1–8) | Proposed fix | Resolved |
|---|---|---:|---|---|
| Dashboard | Review queue empty state | 4 | Add a friendly action button linking to statement import. | Yes |
| Dashboard | Month popover and amount presentation | ✓ clean | Preserve the existing human month picker and numeric styling. | Yes — unchanged |
| Transactions | Table ID/date/flow/classification columns; empty path | 1, 2, 3, 4, 6, 8 | Remove ID, human-format dates and coded values, wrap amounts in numeric styling, and add a designed empty state with Import action. | Yes |
| Review | Queue empty state; remembered-match switch | 4, 5 | Add an Import action and give the custom switch the standard focus ring. | Yes |
| Review drawer | Detail evidence/reason chips/actions | ✓ clean | Preserve human dates/reasons and inline Exclude confirmation. | Yes — unchanged |
| Rules | Visible rule ID, raw match/classification/status values, native-looking selects, headings, empty path | 2, 3, 4, 5, 8 | Remove ID, humanize all codes/status, style selects, use sentence case, and add a designed empty state. | Yes |
| Baselines | Visible baseline ID, raw effective month/source, native-looking select, headings, empty path | 1, 2, 3, 4, 5, 8 | Remove ID, humanize month/source, replace visible raw month with human month picker backed by a hidden canonical value, style controls, use sentence case, and add empty state. | Yes |
| Import | Visible batch ID, raw month/dates/status/type, raw date/month fields and placeholders, native-looking source select/file control, headings, empty path, result notices expose batch IDs | 1, 2, 3, 4, 5, 6, 8 | Remove IDs, format month/dates/status/type, add human month/date controls backed by canonical hidden values, style controls, use sentence case, add empty state, and remove IDs from success notices. | Yes |
| Admin | Logo manager list and upload/remove controls | ✓ clean | Preserve inline Remove confirmation, styled upload controls, and existing feedback. | Yes — unchanged |
| Login | Login form controls and copy | ✓ clean | Preserve shared styled fields/buttons and focus ring. | Yes — unchanged |
| Account garden overlay | Account list and actions | ✓ clean | Preserve human month, status phrases, and Import actions. | Yes — unchanged |
| Toasts | App-wide toast | ✓ clean | Preserve the single designed toast; no browser dialogs. | Yes — unchanged |
| Skeletons | Page/table/form/review drawer skeletons | ✓ clean | Preserve Brief 3.2 skeleton and reduced-motion behavior. | Yes — unchanged |

Verification:
- `python3 -m py_compile app.py` passed (two harmless Python invalid-escape warnings originate from the embedded JavaScript date regex; generated JavaScript parses successfully).
- Authenticated HTTP returned 200 for `/`, `/transactions`, `/review`, `/rules`, `/baselines`, `/import`, and `/admin` after restart on `0.0.0.0:5010`.
- Generated inline JavaScript for all seven authenticated pages passed `node --check`.
- Visible-text scans of all authenticated pages found zero raw ISO months/dates, internal IDs, snake-case values, or audited raw codes.
- Source marker checks found zero `window.confirm`, visible date/month native input types, raw date placeholders, or ID/Batch table headers.
- Review queue remained unchanged at 5 open transactions.

Deliberately left unfixed: none. Hidden canonical values and hidden DB IDs remain by design and are not visible content.


## 2026-08-11 Spending Control Brief 3.2 — Skeletons & Transitions

Implemented Brief 3.2 app-wide in `app.py`, preserving the previous-completed-month default, v2.5 charts/Admin/logo behavior, and Brief 3.1 review drawer/confetti flow.

Changes:
- Added shared skeleton primitives using the existing tokens: 60%-border blocks, matching radii, a 1.4s left-to-right shimmer, KPI, chart, form, five-row table, coverage-panel, and review-drawer silhouettes.
- Added the 150ms anti-flash threshold and 300ms minimum-visible timing for slow initial navigations, route changes, form submissions, and dashboard month navigation. Content swaps use a 150ms fade.
- Added app-wide 250ms height/fade list transition classes for table rows, review rows, logo/account rows, rules, baselines, transactions, and imports; retained the existing purpose-built review collapse choreography.
- Added a single bottom-center `app-toast` API with icon, surface/shadow styling, and 2.5s dismissal. It is used by Admin logo upload, baseline save, rule save/toggle, and successful review resolution.
- Added a review-drawer amount plus five key/value-pair skeleton. Embedded detail data normally resolves below the anti-flash threshold, so the skeleton does not flash on fast opens.
- Added reduced-motion behavior: shimmer is static, page/list swaps are instant through the existing global motion override, and toast motion is opacity-only.

Verification:
- `python3 -m py_compile app.py` passed.
- Restarted the app on `0.0.0.0:5010`.
- Authenticated HTTP checks returned 200 for `/`, `/transactions`, `/review`, `/rules`, `/baselines`, `/import`, `/admin`, and `/api/sankey?month=2026-07`.
- Generated HTML marker checks passed for shared/page/drawer skeletons, 150/300 timing constants, 150ms page fade, 250ms list transitions, unified toast, logo/rule/baseline/review toast paths, and reduced-motion static skeleton behavior.
- Generated inline JavaScript for Dashboard, Review, and Admin parsed successfully with Node.
- Review queue was not mutated during verification and remains at 5 open transactions.

Limitation:
- No browser automation binary was available for a visual throttled-network or screenshot pass. Verification was compile-, generated-JS/HTML-, authenticated-HTTP-, and SQLite-level.

## 2026-08-11 Spending Control Brief 3.1 — Review Workbench + Queue Zero

Implemented Brief 3.1 end-to-end in `app.py` while retaining the previous-completed-month dashboard default, v2.5 charts, account beacon, and persistent Admin logo manager.

Changes:
- Restyled both the dashboard Review Queue preview and `/review` list to Date, Source, Description, Reason, Amount; removed IDs, added human dates, uploaded/bundled/monogram account marks, human reason chips, true-minus currency, and full-row keyboard/click affordances.
- Rebuilt `/review` as a 420px desktop detail drawer and 85vh mobile bottom sheet with scrim/Escape/close dismissal, statement evidence, optional time, generic raw import fields, verbatim description, reasons, and best-guess category.
- Added a guarded searchable category combobox with create-on-no-match behavior, notes, remembered-match toggle and live copy, plus Approve, Transfer, and two-step inline Exclude actions.
- Review actions now return JSON for the animated workbench while retaining a normal POST redirect fallback. Overrides remain the source of truth; transfers and exclusions are retained but use non-spend flow types so calculations omit them.
- Added nullable `rules.notes` migration. Remembered exact-merchant rules assign category/classification/flow and attach the saved note to future matching imports; they appear in the existing Rules manager.
- Added close → row collapse → count tick choreography, list focus restoration, queue-zero victory copy cycling, canvas-confetti top-edge drop, beacon bounce, persistent normal empty state, and reduced-motion instant/static behavior.
- Deferred split controls and the dashboard attention digest as required; the verdict layout includes modest reserved split space.

Verification:
- `python3 -m py_compile app.py` passed; generated workbench inline JavaScript passed `node --check`.
- Restarted on `0.0.0.0:5010`; authenticated HTTP checks returned 200 for `/`, `/review`, `/rules`, `/api/sankey?month=2026-07`, and `/admin`.
- `/` still renders July 2026 by default in August 2026.
- Generated HTML marker checks passed for the five list columns, drawer/scrim, all specified human reason phrases, category combobox/create path, remember toggle, all three actions, party-popper/confetti markers, and reduced-motion CSS.
- Inserted one disposable low-confidence review transaction, approved it as Travel with a note and Remember enabled, and verified: resolved review rows, manual override category/note, visible rule category/note, and future rule application of both category and note. Removed the disposable transaction, review rows, override, rule, and related audit records afterward.

Limitation:
- Browser screenshot automation was not available in this run; interaction verification was compile-, generated-JS-, authenticated-HTTP-, and SQLite-level.

## 2026-08-11 Default Completed Month

- Dashboard requests without an explicit `month` query now default to the previous completed calendar month.
- Explicit month navigation is unchanged: the current month remains available through Today, the next chevron, the month picker, and `?month=YYYY-MM`.

## 2026-08-10

Built the v1 scaffold directly after Bob could not start.

Files:
- `app.py`: dependency-light Python stdlib web app with SQLite.
- `README.md`: run instructions and current limitations.
- `spending_control.sqlite3`: initialized local database with seed rules, seed sample transactions, and Food baseline/cap of ₹12,000.

Implementation shape:
- Uses `http.server` + `sqlite3`, no Flask dependency.
- One shared login via `KANAKKU_USER` / `KANAKKU_PASSWORD`.
- Dashboard includes top cards, Sankey chart, creep watch, and review queue preview.
- Review page supports manual overrides and optional save-as-rule.
- Rules page supports add/toggle.
- Baselines page supports adding caps.
- Import page supports CSV/JSON import and PDF file intake.
- PDF passwords are not stored.
- CLI hook exists for future chat commands: `python3 app.py chat ...`.

Known gaps:
- True Telegram attachment processing still needs OpenClaw file handoff integration.
- Real PDF parsing needs a parser dependency/tool such as `pypdf`, `pdfplumber`, `pdftotext`, or bank-specific extraction scripts.
- UI is functional v1, not final polish.
- Import preview is basic; CSV/JSON commit happens directly after upload.
- Auth is process-memory session based. Fine for v1 local use, but sessions reset on restart.

## 2026-08-10 Mobile/Sankey Fix

Triggered by Vignesh testing on laptop/mobile.

Changes:
- Fixed Sankey rendering by setting `d3.sankey().nodeId(d => d.name)` for named links.
- Added a visible Sankey fallback message instead of blank empty space if the chart library fails.
- Added responsive chart height and resize redraw.
- Added table overflow wrappers so dashboard/review/rules/baselines tables scroll horizontally on mobile instead of spilling out.
- Changed summary card layout to 2 columns on tablet and 1 column on narrow phones.
- Removed seeded review noise for income, card payments, and transfers.
- Changed review count to distinct transactions, not raw reason rows.

## 2026-08-10 Statement Intake Flow

Triggered by Vignesh asking to upload statements through Telegram and flagging that bank and credit card statement periods differ.

Changes:
- Added `statement_start_date` and `statement_end_date` to `import_batches`.
- Import batches now preserve statement cycles separately from dashboard month.
- Dashboard month calculations continue to use actual `transaction_date`, so a 15th-to-15th credit card statement can naturally contribute to two calendar months.
- Import form now asks for statement start and end dates.
- Added chat CLI handoff:
  - `python3 app.py chat ingest-statement /path/to/file.csv "Jananiya HDFC Bank" 2026-08 2026-08-01 2026-08-31`
  - `python3 app.py chat ingest-statement /path/to/file.pdf "Vignesh HDFC Diners" 2026-08 2026-08-15 2026-09-15`

Remaining gap:
- Telegram attachment files still need to be fetched by the agent/runtime and passed into the CLI command.
- PDF extraction is still parser-dependent; PDF files are queued with correct source and period, not parsed yet.

## 2026-08-11 Light-mode Design System

Applied the supplied Spending Control v1 visual brief across the existing server-rendered app without changing routes, persistence, finance logic, or calculations.

Changes:
- Added semantic CSS custom properties for typography, light-mode colors, status/viz colors, spacing, radii, and elevation.
- Loaded Plus Jakarta Sans for UI text, Inter with tabular numerals for financial data, and Lucide for a consistent icon set.
- Restyled global navigation, cards, forms, buttons, notices, chips, tables, focus states, and responsive layouts.
- Kept the content container at 1240px, set the desktop KPI grid to four columns, and set the Sankey/Creep Watch row to a 2:1 split with tablet stacking.
- Added responsive two-column KPI cards on mobile, horizontally snapping coverage tiles, a scrollable nav pill row, and compact reflowing tables to prevent page overflow.
- Added normalized bank-logo containers with automatic image-error fallback monograms; real logo files can later be placed under `/assets/banks/` without markup changes.
- Rebuilt Creep Watch as label/status/progress rows using the same planned and actual values.
- Updated the Sankey to use the specified semantic palette; surplus/unallocated is now green. Labels include tabular currency values.
- Added reduced-motion handling.

Verification:
- `python3 -m py_compile app.py` passes.
- Restarted the app on `0.0.0.0:5010` and verified the listener.
- Authenticated HTTP smoke checks returned 200 for Dashboard, Transactions, Review, Rules, Baselines, Import, and the Sankey API.
- A browser automation package was not available as an importable runtime, so desktop/mobile screenshot inspection was not completed in this environment.

## 2026-08-10 Account Coverage

Changes:
- Added a compact, mobile-friendly account coverage panel at the top of the dashboard for all five expected household accounts/cards.
- Each source shows `Represented`, `Import pending`, or `Missing`; represented sources use a visible tick.
- Coverage counts transaction data in the selected calendar month or a successfully imported batch whose statement period overlaps that month.
- Added alias-aware matching for the former `Vignesh Kotak Bank` label and renamed the configured source to `Vignesh Kotak Mahindra Bank`.
- Added a dashboard month selector. Summary cards and the Sankey now consistently use transaction dates from the selected month; statement dates remain coverage/import-audit metadata.

## 2026-08-11 Design Brief #2 — Motion, Mood & the Beacon

Applied the visual/interaction brief without changing finance calculations, import/rule behavior, persistence, auth, or existing application routes.

Changes:
- Replaced the native month field and View button with full-name month navigation, bounded chevrons, an off-current Today link, a month/year popover, keyboard arrows, and 250ms debounced navigation. Missing months now render a friendly import empty state.
- Added the time-of-day/day-of-year greeting hero with factual month totals, health-driven healthy/tight/negative gradients, and the 16-second ambient breathing treatment.
- Added 400ms Indian-formatted count-ups for exactly the four KPI values and Creep Watch amounts, a 60ms KPI stagger, and animated Creep Watch progress fills.
- Renamed the visualization card to Cash flow and added non-persistent Sankey, Waterfall, Treemap, and Bars views with view-specific entrance motion.
- Removed the dashboard coverage card. Coverage now lives in the top-bar sprout beacon and responsive Account garden popover/bottom sheet, with missing counts, represented/missing states, dismissal controls, and account-prefilled Import now deep links.
- Centralized bank-name matching in `BANK_ASSETS`, with HDFC Diners matched before generic HDFC. Added an authenticated-independent static handler for `/assets/banks/<name>` and mandatory monogram fallback.
- Added comprehensive `prefers-reduced-motion: reduce` behavior for count-ups, fills, hero, charts, beacon, and panels.

Bank asset owner step:
- Put the official symbol-mark SVGs at `assets/banks/axis.svg`, `kotak.svg`, `hdfc.svg`, `yes.svg`, and `hdfc-diners.svg`. No copyrighted logo files were downloaded or bundled. Missing files fall back cleanly to monograms.

Verification:
- `python3 -m py_compile app.py` passes.
- Restarted on `0.0.0.0:5010`.
- Authenticated HTTP checks return 200 for Login redirect, Dashboard, Transactions, Review, Rules, Baselines, Import, and `/api/sankey?month=2026-05`.
- Dashboard HTML marker checks passed for the hero, human month nav, four-view segmented control, beacon/panel, reduced-motion CSS, count-up attributes, and progress attributes. Legacy native month/View controls and body coverage card are absent.
- Extracted inline JavaScript passes `node --check`; account/month Import deep-link prefilling was confirmed in generated HTML.
- Playwright is installed, but its configured Chrome binary is absent (`/opt/google/chrome/chrome`), so automated desktop/mobile screenshots could not be completed in this environment.

## 2026-08-11 Previous Completed Month Default

Triggered by Vignesh noting that bank/card statements are only complete after month-end.

Changes:
- Opening the dashboard without a `month` query now defaults to the previous completed calendar month.
- Explicit month links still work, so `/` defaults to July 2026 during August 2026, while `/?month=2026-08` still opens August 2026.
- Current month remains reachable via Today, chevrons, and direct query links.
- Fixed the static bank asset handler to use the app `ROOT` path for `/assets/banks/` fallback/logo requests.

## 2026-08-11 Design Brief v2.5 — Chart Fixes + Admin Logo Manager

Implemented the supplied v2.5 brief without changing finance calculations, transaction/import/rule behavior, authentication, or the previous-completed-month default.

Changes:
- Rebuilt Bars as descending label/track/amount rows with fixed 24px tracks and fills, 6px corners, per-row `--brand-050` tracks, inflow-relative widths, a 4px positive-value minimum, solid viz colors, and the requested 400ms/60ms staggered entrance animation.
- Rebuilt Treemap with D3 squarified layout, 4px internal gaps, solid 8px-radius tiles, white fit-aware labels, native hover tooltips, and a legend chip row for values below 3% of inflow.
- Added the top-right 36px Admin settings button between the Account garden beacon and version chip. Admin remains outside the primary navigation and receives active styling on `/admin`.
- Added a hero-less `/admin` page with a Bank logos manager sourced from the same stable `SOURCES` account list as coverage.
- Added 40px and 28px shared logo-container previews, upload buttons, per-row drag/drop, client-side validation, instant previews, inline errors, success toast, and inline Remove confirmation.
- Added SQLite `account_logos` persistence keyed by stable `source_id`, durable files under `assets/uploads/bank-logos/`, authenticated upload/remove handling at `/admin/logo`, and stable serving via `/assets/uploads/bank-logos/<file>`.
- Logo resolution is now uploaded logo, then bundled bank asset, then monogram. The Account garden and Admin previews share this resolver; existing grayscale missing-data classes continue to wrap uploaded images.
- Hardened multipart filename parsing so file parts with a `Content-Type` header retain the actual filename (also benefits statement imports).

Verification:
- `python3 -m py_compile app.py` passes without warnings.
- Restarted/reused the app on `0.0.0.0:5010`.
- Authenticated HTTP checks returned 200 for `/`, `/?month=2026-08`, `/admin`, `/transactions`, `/review`, `/rules`, `/baselines`, `/import`, and `/api/sankey?month=2026-07`.
- `/` still renders July 2026 and `/?month=2026-08` renders August 2026.
- HTML marker checks confirmed Admin is absent from the primary nav, the settings entry is in the top-right cluster, all five logo rows/inputs/remove controls exist, and the new Bars/Treemap implementations are present.
- Uploaded a tiny valid SVG, confirmed 200 response and serving as `image/svg+xml`, confirmed its stable URL appeared in both Admin and dashboard Account garden HTML, restarted the server, and confirmed the mapping remained. Removed the test logo afterward, leaving no test mapping/file.
- Invalid `.txt` upload returns 400 with the required validation message. Missing bundled and uploaded asset requests return 404 without crashing the server.

Limitation:
- Automated browser screenshots were not completed; verification was compile-, HTTP-, persistence-, and generated-HTML-based.

## 2026-08-11 Admin Logo Upload Trigger Fix

Triggered by Vignesh reporting that clicking "Upload logo" in Admin did nothing.

Changes:
- Replaced the script-only `input.click()` upload trigger with a native `<label for="...">` bound to each hidden file input.
- Added stable ids for each account logo file input.
- Kept drag/drop, validation, upload persistence, remove, and toast behavior unchanged.

Verification:
- `python3 -m py_compile app.py` passes.
- Restarted on `0.0.0.0:5010`.
- Admin HTML now renders `label.upload-logo` controls linked to the corresponding file input ids.
- `/`, `/admin`, `/import`, and `/api/sankey?month=2026-07` return 200.
- Test SVG upload and remove still work; test mapping count returned to 0.

## 2026-08-11 Admin Logo Upload Feedback Fix

Triggered by Vignesh reporting that file selection worked, but the upload step appeared to do nothing.

Changes:
- Added explicit per-row upload feedback in the Admin logo manager.
- After a file is selected, the row now immediately previews the selected image in the existing 40px and 28px logo containers.
- While the request is in flight, the row shows `Uploading logo...`, sets `aria-busy`, and displays a small spinner on the Upload logo control.
- On success, the row shows `Logo uploaded.`, shows the existing bottom toast, enables Remove, and swaps the preview to the persisted server URL.
- Upload failures now stay on the page with a visible inline error instead of silently reloading.
- The file input is reset after each attempt so the same file can be retried.

Verification:
- `python3 -m py_compile app.py` passes.
- Restarted on `0.0.0.0:5010`.
- `/login` returns 200 and `/admin` returns 200 after authentication.
- Rendered Admin upload JavaScript passes `node --check`.
- Test SVG upload returned 200, served as `image/svg+xml`, and was removed afterward.
## 2026-08-11 Spending Control — automatic extraction and import visibility

- Added a daemon background worker that is triggered immediately after PDF upload, uses its own SQLite connections, claims each batch with `extracting`, and records start/outcome audit entries. The same worker runs at server startup and is available through `python3 app.py chat process-pending-imports` and the Import page’s **Process pending imports** button.
- Added optional `pypdf` extraction without making it a startup dependency. Encrypted files require the password during the upload request; it is passed only to the worker thread and never stored. Missing extractor, encrypted-without-password, image-only, and unsupported-bank-parser cases now end in a clear `needs_parser` state instead of waiting indefinitely.
- Added a conservative bank-parser interface. It deliberately imports no guessed PDF transactions: a verified source-specific mapper must return structured rows before anything is committed.
- Added `excluded_at` and `deleted_at` migrations on `import_batches`. Recent imports now show Included/Excluded state, Hide from dashboard, Restore, Retry extraction, and confirmed Delete/archive controls. Delete is a soft archive and keeps the uploaded file and transaction rows.
- Applied active-import filtering to dashboard totals/charts, dashboard and full review queues, Transactions, Sankey data, account coverage, month bounds, and category suggestions. Seed visibility behavior remains independent and real uploads still disable seed data automatically.
- Processed the real Kotak May 2026 batch. It now shows **Needs parser** with the concrete note that the server has no optional `pypdf` extractor; its PDF remains untouched in `imports/inbox`.

Verification:
- `python3 -m py_compile app.py` passed (the two pre-existing embedded-JavaScript invalid-escape warnings remain).
- Authenticated GETs returned 200 for `/`, `/import`, `/transactions`, `/review`, and `/api/sankey?month=2026-05` after restart on `0.0.0.0:5010`.
- A disposable batch/transaction/review item was visible initially, absent from Transactions and Review after Hide, visible after Restore, and absent from Transactions and active Import history after Delete/archive. The disposable verification records were then removed.
- Final limitation: PDF text/transaction import will require installing optional `pypdf` and adding a verified Kotak statement mapper (or OCR for image-only PDFs). Until then the worker reports an actionable Needs parser status and does not risk incorrect financial rows.
