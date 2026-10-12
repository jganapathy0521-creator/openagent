"""Categorizer plumbing, tested with a fake LLM (no API calls)."""
import pandas as pd
import pytest
from pydantic import ValidationError

from books.categorizer import Categorizer, apply_corrections, build_llm, make_output_schema
from books.chart_of_accounts import ChartOfAccounts
from books.ingest import from_records
from books.rules import load_rules

COA = ChartOfAccounts.load()
SCHEMA = make_output_schema(COA.categorizable_names())


class FakeLLM:
    """Mimics llm.with_structured_output(...).invoke(messages)."""

    def __init__(self, answers: dict[str, tuple[str, float]], skip: set[str] = frozenset()):
        self.answers, self.skip, self.calls = answers, set(skip), 0

    def invoke(self, messages):
        self.calls += 1
        human = messages[-1][1]
        results = []
        for tid, (cat, conf) in self.answers.items():
            if f'"txn_id": "{tid}"' in human and tid not in self.skip:
                results.append({"txn_id": tid, "category": cat, "confidence": conf, "reasoning": "fake"})
        self.skip.clear()  # answer everything on the retry
        return SCHEMA(results=results)


def frame():
    return from_records([
        {"txn_id": "T1", "date": "2026-01-15", "description": "GUSTO NET PAY AB12", "amount": -8000.0},
        {"txn_id": "T2", "date": "2026-01-16", "description": "UBER *EATS 8005928996", "amount": -31.2},
        {"txn_id": "T3", "date": "2026-01-17", "description": "CHECK #1043", "amount": -1200.0},
        {"txn_id": "T4", "date": "2026-01-18", "description": "STRIPE TRANSFER ST-XYZ", "amount": 2100.0},
    ])


def test_schema_rejects_invented_category():
    with pytest.raises(ValidationError):
        SCHEMA(results=[{"txn_id": "x", "category": "Snacks", "confidence": 0.9, "reasoning": ""}])


def test_rules_then_llm_then_review_flags():
    llm = FakeLLM({"T2": ("Meals", 0.95), "T3": ("Uncategorized", 0.2), "T4": ("Service Revenue", 0.7)},
                  skip={"T4"})
    out = Categorizer(COA, rules=load_rules(COA), llm=llm, review_threshold=0.8).categorize(frame())
    row = out.set_index("txn_id")
    assert row.loc["T1", "source"] == "rule" and row.loc["T1", "category"] == "Wages"
    assert row.loc["T2", "category"] == "Meals" and not row.loc["T2", "needs_review"]
    assert row.loc["T3", "needs_review"]          # Uncategorized always goes to review
    assert row.loc["T4", "needs_review"]          # confidence 0.7 < 0.8
    assert llm.calls == 2                         # T4 was skipped once, then retried


def test_offline_marks_unmatched_for_review():
    out = Categorizer(COA, rules=load_rules(COA), llm=None).categorize(frame())
    assert (out.loc[out["source"] == "unmatched", "category"] == "Uncategorized").all()
    assert out.loc[out["source"] == "unmatched", "needs_review"].all()


def test_llm_only_mode_skips_rules():
    llm = FakeLLM({t: ("Wages", 0.9) for t in ("T1", "T2", "T3", "T4")})
    out = Categorizer(COA, rules=load_rules(COA), llm=llm, use_rules=False).categorize(frame())
    assert set(out["source"]) == {"llm"}


def test_corrections_override():
    out = Categorizer(COA, rules=load_rules(COA), llm=None).categorize(frame())
    fixed = apply_corrections(out, pd.DataFrame([{"txn_id": "T3", "corrected_category": "Contractors"}]), COA)
    r = fixed.set_index("txn_id").loc["T3"]
    assert r["category"] == "Contractors" and r["source"] == "human" and not r["needs_review"]
    with pytest.raises(ValueError):
        apply_corrections(out, pd.DataFrame([{"txn_id": "T3", "corrected_category": "Snacks"}]), COA)


def test_build_llm_constructs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    assert build_llm(COA) is not None  # builds the structured-output runnable without calling the API
