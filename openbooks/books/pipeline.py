"""End-to-end pipeline and CLI.

    python -m books.pipeline data/sample_transactions_2026.xlsx
    python -m books.pipeline my_bank_export.csv --config config/company.json --out output/
    python -m books.pipeline my_bank_export.csv --offline          # rules only, no API calls
    python -m books.pipeline my_bank_export.csv --corrections output/review_queue.csv

Outputs (in --out):
    financials_<year>.xlsx   P&L, Balance Sheet, Checks, Transactions, Review Queue, Journal
    statements.json          the same statements as data
    review_queue.csv         fill in corrected_category, then re-run with --corrections
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langsmith import traceable

from books import PROJECT_ROOT
from books.categorizer import Categorizer, apply_corrections, build_llm
from books.chart_of_accounts import ChartOfAccounts
from books.config import CompanyConfig
from books.ingest import ValidationReport, ingest, validate
from books.ledger import build_journal, journal_frame
from books.rules import load_rules
from books.statements import build_statements, export_excel

load_dotenv(PROJECT_ROOT.parent / ".env")  # repo-root .env, shared with the other projects
load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class PipelineResult:
    categorized: pd.DataFrame
    statements: dict
    journal_df: pd.DataFrame
    loan_schedule: list[dict]
    report: ValidationReport


def make_categorizer(coa: ChartOfAccounts, config: CompanyConfig, offline: bool = False,
                     use_rules: bool = True, model: str | None = None) -> Categorizer:
    llm = None if offline else build_llm(coa, model)
    return Categorizer(coa, rules=load_rules(coa), llm=llm, batch_size=config.batch_size,
                       review_threshold=config.review_confidence_threshold, use_rules=use_rules,
                       model_name=model)


@traceable(name="openbooks_pipeline", run_type="chain")
def run_pipeline(df: pd.DataFrame, config: CompanyConfig, coa: ChartOfAccounts, categorizer: Categorizer,
                 report: ValidationReport | None = None, corrections: pd.DataFrame | None = None) -> PipelineResult:
    """Categorize -> journal -> statements. `df` must already be normalized (see books.ingest)."""
    if report is None:
        report = validate(df, config.period_start, config.period_end, config.opening_cash)
    categorized = categorizer.categorize(df)
    if corrections is not None:
        categorized = apply_corrections(categorized, corrections, coa)
    journal, loan_schedule = build_journal(categorized, coa, config)
    statements = build_statements(
        journal, coa, config,
        txn_ids=list(categorized["txn_id"]),
        bank_ending_balance=report.stats.get("ending_balance"),
        review_count=int(categorized["needs_review"].sum()),
    )
    return PipelineResult(categorized, statements, journal_frame(journal, coa), loan_schedule, report)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Categorize a bank export and build P&L + Balance Sheet")
    p.add_argument("transactions", help="Bank export (.csv or .xlsx)")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config" / "company.json"))
    p.add_argument("--chart", default=None, help="Chart of accounts CSV (default: config/chart_of_accounts.csv)")
    p.add_argument("--out", default=str(PROJECT_ROOT / "output"))
    p.add_argument("--offline", action="store_true", help="Rules only; no LLM calls")
    p.add_argument("--model", default=None, help="Anthropic model id (default: $OPENBOOKS_MODEL or built-in)")
    p.add_argument("--force", action="store_true", help="Continue even if validation finds errors")
    p.add_argument("--corrections", default=None, help="Reviewed review_queue.csv with corrected_category filled in")
    args = p.parse_args(argv)

    coa = ChartOfAccounts.load(args.chart)
    config = CompanyConfig.load(args.config)
    problems = config.validate(coa)
    if problems:
        print("Company config problems:\n  " + "\n  ".join(problems))
        return 2

    if not args.offline and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; running --offline (rules only).")
        args.offline = True

    df, report = ingest(args.transactions, config.period_start, config.period_end, config.opening_cash)
    print(report)
    if not report.ok and not args.force:
        print("\nFix the errors above (or pass --force to continue anyway).")
        return 1

    corrections = pd.read_csv(args.corrections) if args.corrections else None
    categorizer = make_categorizer(coa, config, offline=args.offline, model=args.model)
    result = run_pipeline(df, config, coa, categorizer, report=report, corrections=corrections)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    xlsx = out / f"financials_{config.period_end.year}.xlsx"
    export_excel(xlsx, result.statements, result.categorized, result.journal_df, report.to_dict(),
                 result.loan_schedule)
    (out / "statements.json").write_text(json.dumps(
        {**result.statements, "validation": report.to_dict(), "categorizer": categorizer.metadata},
        indent=2, default=str))
    review = result.categorized[result.categorized["needs_review"]][
        ["txn_id", "date", "description", "amount", "category", "confidence", "reasoning"]].copy()
    review.insert(5, "corrected_category", "")
    review.to_csv(out / "review_queue.csv", index=False)

    pnl, bs = result.statements["profit_and_loss"], result.statements["balance_sheet"]
    src = result.categorized["source"].value_counts().to_dict()
    print(f"\nCategorized {len(result.categorized)} transactions ({src}); "
          f"{len(review)} flagged for review.")
    print(f"Revenue {pnl['total_revenue']:,.2f} | Net income {pnl['net_income']:,.2f} | "
          f"Total assets {bs['total_assets']:,.2f}")
    for name, chk in result.statements["checks"].items():
        print(f"  [{'PASS' if chk['passed'] else 'FAIL'}] {name}: {chk['detail']}")
    print(f"\nWrote {xlsx}, {out / 'statements.json'}, {out / 'review_queue.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
