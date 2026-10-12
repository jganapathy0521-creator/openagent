"""Deterministic vendor rules, applied before the LLM.

Rules are free, instant and never change their mind, so put recurring,
unambiguous vendors here (payroll provider, landlord, loan servicer). Leave
ambiguous ones (Amazon, Uber, Google) to the LLM.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from books import CONFIG_DIR
from books.chart_of_accounts import ChartOfAccounts


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    category: str
    direction: str  # "in", "out" or "any"
    note: str = ""

    def matches(self, description: str, amount: float) -> bool:
        if self.direction == "in" and amount <= 0:
            return False
        if self.direction == "out" and amount >= 0:
            return False
        return bool(self.pattern.search(description.upper()))


def load_rules(coa: ChartOfAccounts, path: str | Path | None = None) -> list[Rule]:
    path = Path(path) if path else CONFIG_DIR / "vendor_rules.csv"
    if not path.exists():
        return []
    rules = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            category = row["category"].strip()
            if category not in coa.categorizable_names():
                raise ValueError(f"Rule {row['pattern']!r} points at non-categorizable account {category!r}")
            direction = (row.get("direction") or "any").strip().lower()
            if direction not in ("in", "out", "any"):
                raise ValueError(f"Rule {row['pattern']!r} has bad direction {direction!r}")
            rules.append(Rule(re.compile(row["pattern"].strip(), re.IGNORECASE), category, direction,
                              (row.get("note") or "").strip()))
    return rules


def match_rule(description: str, amount: float, rules: list[Rule]) -> Rule | None:
    for rule in rules:
        if rule.matches(description, amount):
            return rule
    return None
