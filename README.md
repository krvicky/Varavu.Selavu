# Varavu.Selavu

Local household income-and-expense dashboard for Kanakku.

It is a single-file Python web app backed by SQLite. The app can run on a VPS, laptop, or any machine with Python 3.11+.

## What Is In This Repo

- `app.py` - web app, routes, UI templates, SQLite schema, import logic, and CLI hooks.
- `requirements.txt` - optional Python package list for PDF text extraction.
- `assets/banks/` - tracked static asset folder placeholder.
- `imports/inbox/` and `imports/archive/` - runtime import folders, kept empty in git.

Not committed by design:

- `spending_control.sqlite3` - live local database.
- `*.log` - server/runtime logs.
- `imports/inbox/*` and `imports/archive/*` - uploaded statements.
- `assets/uploads/*` - uploaded bank logos.
- `__pycache__/`, virtualenvs, and other generated files.

Real financial data should stay off GitHub. Boring rule, but important.

## Fresh Install

```bash
git clone <repo-url>
cd Varavu.Selavu
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

If you only want the current CSV/JSON app features, the app can run without third-party packages. Installing `requirements.txt` adds the optional `pypdf` package used for PDF text extraction.

## Run Locally

```bash
export KANAKKU_USER='vignesh'
export KANAKKU_PASSWORD='change-this-before-real-use'
export KANAKKU_PORT='5010'
python3 app.py
```

Open:

```text
http://localhost:5010
```

Defaults if env vars are not set:

- Username: `vignesh`
- Password: `change-me`
- Port: `5010`

Set a real password before using real financial data.

## First Run Behavior

On startup, `app.py` automatically:

- Creates `spending_control.sqlite3` if it does not exist.
- Creates the required SQLite tables.
- Creates `imports/inbox/`, `imports/archive/`, and `assets/uploads/bank-logos/`.
- Seeds demo data and baseline rules so the dashboard is usable immediately.

The Admin page has a `Use seed data` toggle. Keep it on for demo mode; turn it off when real imports should drive the dashboard.

## Current Statement Import Support

Supported now:

- CSV import.
- JSON import.
- PDF upload intake.
- PDF background extraction attempt.
- Import inclusion/exclusion and safe delete/archive controls.

PDF parsing has two layers:

1. Text extraction: handled by optional `pypdf` when installed.
2. Bank-specific transaction mapping: still needs verified parsers for Kotak, Axis, HDFC Bank, Yes Bank, and HDFC Diners.

If `pypdf` is missing, PDF imports will show extraction unavailable. If `pypdf` is installed but no bank parser exists, the app may extract text but will keep the import in `Needs parser` instead of guessing transaction rows.

That is deliberate. Guessing financial rows is how dashboards get quietly corrupted.

## CLI Hooks

Set a baseline:

```bash
python3 app.py chat set-baseline Food 12000 2026-05
```

Queue or import a statement file from chat/OpenClaw handoff:

```bash
python3 app.py chat ingest-statement /path/to/statement.pdf "Vignesh HDFC Diners" 2026-08 2026-08-15 2026-09-15
python3 app.py chat ingest-statement /path/to/statement.csv "Jananiya HDFC Bank" 2026-08 2026-08-01 2026-08-31
```

CSV/JSON files commit immediately. PDFs are copied into `imports/inbox/` and marked for extraction. PDF passwords are used only during extraction and are not stored.

## VPS Notes

For a VPS, run the app behind a process manager such as `systemd`, `supervisor`, Docker, or another deployment wrapper. Minimum environment variables:

```bash
KANAKKU_USER=vignesh
KANAKKU_PASSWORD=<strong-password>
KANAKKU_PORT=5010
```

If exposed directly to the internet, put it behind a reverse proxy with HTTPS and authentication rules. The simpler private option is to keep access restricted through Tailscale or another private network.

## Backup Notes

To move the live app to another machine, copy these runtime items separately from git:

- `spending_control.sqlite3`
- `imports/inbox/` and `imports/archive/`, if you need original uploaded statements
- `assets/uploads/bank-logos/`, if you want uploaded logos preserved

Do not commit those files to GitHub.
