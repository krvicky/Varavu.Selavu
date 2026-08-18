"""Coverage of the built-in rules against the real parsed statements.

Skipped unless experiments/transactions*.csv exist locally (they hold real financial data and are
gitignored; produce them with scripts/parse_statements.py). Guards against regressions in
DEFAULT_RULES: coverage must stay at least where the categorization experiment left it, and no
rule may assign a (category, subcategory) pair that isn't in the taxonomy.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

import app as app_module

ROOT = Path(__file__).resolve().parents[1]
DATASETS = [
    ("kotak", ROOT / "experiments" / "transactions.csv", 0.88),
    ("diners", ROOT / "experiments" / "transactions_diners.csv", 0.98),
]


@pytest.mark.parametrize("name,path,minimum", DATASETS, ids=[d[0] for d in DATASETS])
def test_default_rules_cover_real_statements(seeded_server, name, path, minimum):
    if not path.exists():
        pytest.skip(f"{path.name} not present locally")
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    matched = 0
    bad_pairs = set()
    with app_module.db() as conn:
        for r in rows:
            tx = app_module.apply_rules(conn, {"description": r["description"], "amount": float(r["amount"]), "source_name": r["source_name"]})
            if tx.get("rule_id"):
                matched += 1
            cat, sub = tx.get("category"), tx.get("subcategory")
            if cat and (cat not in app_module.SUBCATEGORIES or (sub and sub not in app_module.SUBCATEGORIES[cat])):
                bad_pairs.add((cat, sub))
    coverage = matched / len(rows)
    print(f"\n{name}: {matched}/{len(rows)} matched ({coverage:.0%})")
    assert not bad_pairs, f"rules assign pairs outside the taxonomy: {sorted(bad_pairs)}"
    assert coverage >= minimum, f"{name} coverage {coverage:.0%} < {minimum:.0%}"
