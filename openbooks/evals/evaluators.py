"""Evaluators for the LangSmith eval suite.

They are plain functions using LangSmith's argument names (inputs, outputs,
reference_outputs), so the same code runs in `client.evaluate(...)` and in the
local runner used by tests/CI.

Two levels:
  1. Categorization (per transaction): is the category right, and when it is
     wrong, was it at least flagged for human review?
  2. Statements (per full-year scenario): do the books balance, does cash tie
     to the bank, and how close are the P&L / Balance Sheet lines to the
     accountant's version?
"""
from __future__ import annotations

from collections import Counter

# Balance-sheet categories plus "send to review": mistakes here distort the
# statements the most (e.g. an owner draw booked as an expense understates profit).
CRITICAL_CATEGORIES = {
    "Savings Account", "Owner Draws", "Owner Contributions", "Loan Payable", "Equipment", "Uncategorized",
}

LINE_TOLERANCE_ABS = 50.00   # dollars
LINE_TOLERANCE_PCT = 0.02    # 2% of the reference line


# ---------------------------------------------------------------- categorization (per row)

def category_correct(outputs: dict, reference_outputs: dict) -> bool:
    return outputs.get("category") == reference_outputs["category"]


def high_confidence_error(outputs: dict, reference_outputs: dict) -> dict:
    """1 = BAD: wrong but not flagged for review, so it would flow straight into the statements."""
    wrong = outputs.get("category") != reference_outputs["category"]
    return {"key": "high_confidence_error", "score": int(wrong and not outputs.get("needs_review", False))}


def error_caught(outputs: dict, reference_outputs: dict) -> dict:
    """1 = safe: either correct, or wrong but flagged for a human."""
    ok = outputs.get("category") == reference_outputs["category"] or bool(outputs.get("needs_review"))
    return {"key": "error_caught", "score": int(ok)}


# ---------------------------------------------------------------- categorization (whole experiment)

def accuracy(outputs: list[dict], reference_outputs: list[dict]) -> dict:
    n = len(reference_outputs)
    hits = sum(o.get("category") == r["category"] for o, r in zip(outputs, reference_outputs))
    return {"key": "accuracy", "score": hits / n if n else 0.0}


def critical_category_accuracy(outputs: list[dict], reference_outputs: list[dict]) -> dict:
    pairs = [(o, r) for o, r in zip(outputs, reference_outputs) if r["category"] in CRITICAL_CATEGORIES]
    hits = sum(o.get("category") == r["category"] for o, r in pairs)
    return {"key": "critical_category_accuracy", "score": hits / len(pairs) if pairs else 1.0}


def macro_f1(outputs: list[dict], reference_outputs: list[dict]) -> dict:
    """Average F1 across categories, so rare categories count as much as common ones."""
    tp, fp, fn = Counter(), Counter(), Counter()
    for o, r in zip(outputs, reference_outputs):
        pred, gold = o.get("category"), r["category"]
        if pred == gold:
            tp[gold] += 1
        else:
            fp[pred] += 1
            fn[gold] += 1
    labels = set(tp) | set(fn)
    f1s = []
    for c in labels:
        p = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        rcl = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else 0.0
        f1s.append(2 * p * rcl / (p + rcl) if p + rcl else 0.0)
    return {"key": "macro_f1", "score": sum(f1s) / len(f1s) if f1s else 0.0}


def review_rate(outputs: list[dict], reference_outputs: list[dict]) -> dict:
    """Share of transactions sent to a human. Lower is less work; zero is suspicious."""
    n = len(outputs)
    return {"key": "review_rate", "score": sum(bool(o.get("needs_review")) for o in outputs) / n if n else 0.0}


CATEGORIZATION_EVALUATORS = [category_correct, high_confidence_error, error_caught]
CATEGORIZATION_SUMMARY_EVALUATORS = [accuracy, critical_category_accuracy, macro_f1, review_rate]


# ---------------------------------------------------------------- statements (per scenario)

def balance_sheet_balances(outputs: dict) -> bool:
    return bool(outputs["checks"].get("balance_sheet_balances"))


def cash_ties_to_bank(outputs: dict) -> bool:
    return bool(outputs["checks"].get("cash_ties_to_bank"))


def all_transactions_posted(outputs: dict) -> bool:
    return bool(outputs["checks"].get("all_transactions_posted"))


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(LINE_TOLERANCE_ABS, LINE_TOLERANCE_PCT * abs(b))


def net_income_accuracy(outputs: dict, reference_outputs: dict) -> dict:
    """1.0 = exact; decreases with relative error. Comment carries the dollar difference."""
    got, want = outputs["net_income"], reference_outputs["net_income"]
    rel = abs(got - want) / max(abs(want), 1.0)
    return {"key": "net_income_accuracy", "score": max(0.0, 1.0 - rel),
            "comment": f"net income {got:,.2f} vs reference {want:,.2f} (diff {got - want:,.2f})"}


def _line_accuracy(key: str, got: dict, want: dict) -> dict:
    accounts = sorted(set(got) | set(want))
    off = [a for a in accounts if not _close(got.get(a, 0.0), want.get(a, 0.0))]
    score = 1 - len(off) / len(accounts) if accounts else 1.0
    detail = "; ".join(f"{a}: {got.get(a, 0.0):,.2f} vs {want.get(a, 0.0):,.2f}" for a in off[:6])
    return {"key": key, "score": score, "comment": f"{len(off)} of {len(accounts)} lines off. {detail}"}


def pnl_line_accuracy(outputs: dict, reference_outputs: dict) -> dict:
    return _line_accuracy("pnl_line_accuracy", outputs["pnl"], reference_outputs["pnl"])


def bs_line_accuracy(outputs: dict, reference_outputs: dict) -> dict:
    return _line_accuracy("bs_line_accuracy", outputs["bs"], reference_outputs["bs"])


def scenario_categorization_accuracy(outputs: dict, reference_outputs: dict) -> dict:
    truth = reference_outputs["truth"]
    got = outputs.get("categories", {})
    hits = sum(got.get(t) == c for t, c in truth.items())
    return {"key": "scenario_categorization_accuracy", "score": hits / len(truth) if truth else 1.0}


STATEMENT_EVALUATORS = [
    balance_sheet_balances, cash_ties_to_bank, all_transactions_posted,
    net_income_accuracy, pnl_line_accuracy, bs_line_accuracy, scenario_categorization_accuracy,
]
