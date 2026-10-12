"""The functions LangSmith calls for each dataset example (the system under test)."""
from __future__ import annotations

import pandas as pd

from books.categorizer import Categorizer
from books.chart_of_accounts import ChartOfAccounts
from books.config import CompanyConfig
from books.ingest import from_records, validate
from books.ledger import build_journal
from books.pipeline import make_categorizer, run_pipeline
from books.statements import build_statements, flatten_statements

COA = ChartOfAccounts.load()


class OracleCategorizer:
    """Assigns the known-correct category. Used to build reference statements."""

    def __init__(self, truth: dict[str, str]):
        self.truth = truth
        self.metadata = {"model": "oracle"}

    def categorize(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["category"] = [self.truth[t] for t in out["txn_id"]]
        out["confidence"], out["source"], out["reasoning"], out["needs_review"] = 1.0, "truth", "", False
        return out


def make_categorization_target(categorizer: Categorizer):
    def categorize_transaction(inputs: dict) -> dict:
        df = from_records([{
            "txn_id": inputs.get("txn_id", "T0001"), "date": inputs["date"],
            "description": inputs["description"], "amount": float(inputs["amount"]),
        }])
        row = categorizer.categorize(df).iloc[0]
        return {"category": row["category"], "confidence": float(row["confidence"]), "source": row["source"],
                "reasoning": row["reasoning"], "needs_review": bool(row["needs_review"])}
    return categorize_transaction


def run_scenario(inputs: dict, categorizer) -> dict:
    config = CompanyConfig.from_dict(inputs["config"])
    df = from_records(inputs["transactions"])
    report = validate(df, config.period_start, config.period_end, config.opening_cash)
    result = run_pipeline(df, config, COA, categorizer, report=report)
    flat = flatten_statements(result.statements)
    flat["categories"] = dict(zip(result.categorized["txn_id"], result.categorized["category"]))
    flat["review_count"] = int(result.categorized["needs_review"].sum())
    flat["validation_ok"] = report.ok
    return flat


def make_statements_target(categorizer: Categorizer):
    def build_financial_statements(inputs: dict) -> dict:
        return run_scenario(inputs, categorizer)
    return build_financial_statements


def reference_statements(scenario: dict) -> dict:
    """Statements built from the answer key: the stand-in for your accountant's numbers."""
    config = CompanyConfig.from_dict(scenario["config"])
    df = from_records(scenario["transactions"])
    report = validate(df, config.period_start, config.period_end, config.opening_cash)
    categorized = OracleCategorizer(scenario["truth"]).categorize(df)
    journal, _ = build_journal(categorized, COA, config)
    result = build_statements(journal, COA, config, txn_ids=list(df["txn_id"]),
                              bank_ending_balance=report.stats.get("ending_balance"))
    flat = flatten_statements(result)
    flat.pop("checks")
    flat["truth"] = scenario["truth"]
    return flat


def build_categorizer(offline: bool, use_rules: bool, model: str | None) -> Categorizer:
    return make_categorizer(COA, CompanyConfig.load(), offline=offline, use_rules=use_rules, model=model)
