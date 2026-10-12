"""Turn categorized bank transactions into a double-entry journal.

All amounts are integer cents, debit-positive / credit-negative, so every
entry sums to exactly zero and the books always balance arithmetically.

Entries produced:
  * opening balances (from company config)
  * one entry per bank transaction: Cash vs. the chosen category
  * loan payments split into principal (Loan Payable) and interest (Interest Expense)
  * year-end depreciation on opening equipment and on equipment bought during the period
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from books.chart_of_accounts import (ACCUMULATED_DEPRECIATION, CASH, DEPRECIATION_EXPENSE, EQUIPMENT,
                                     INTEREST_EXPENSE, ChartOfAccounts)
from books.config import CompanyConfig, to_cents


@dataclass
class JournalLine:
    entry_id: str
    date: date
    account: str  # account code
    amount_cents: int  # debit +, credit -
    memo: str
    txn_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = str(self.date)
        return d


def opening_entries(config: CompanyConfig, coa: ChartOfAccounts) -> list[JournalLine]:
    return [
        JournalLine("OPEN", config.period_start, code, coa.by_code[code].sign * to_cents(amount),
                    "Opening balance")
        for code, amount in config.opening_balances.items()
        if to_cents(amount) != 0
    ]


def _months_in_service(purchase: date, period_end: date) -> int:
    """Full months of depreciation from the month after purchase through period_end."""
    months = (period_end.year - purchase.year) * 12 + (period_end.month - purchase.month)
    return max(0, months)


def transaction_entries(df: pd.DataFrame, coa: ChartOfAccounts,
                        config: CompanyConfig) -> tuple[list[JournalLine], list[dict]]:
    """Journal lines for each categorized bank transaction, plus the loan schedule."""
    lines: list[JournalLine] = []
    loan_schedule: list[dict] = []
    loan_code = config.loan_account
    loan_balance = to_cents(config.opening_balances.get(loan_code, 0)) if loan_code else 0
    monthly_rate = config.loan_annual_rate / 12

    for t in df.sort_values(["date", "txn_id"], kind="stable").to_dict("records"):
        amt = to_cents(t["amount"])
        if amt == 0:
            continue
        account = coa.get(t["category"])
        eid = f"TXN-{t['txn_id']}"
        lines.append(JournalLine(eid, t["date"], CASH, amt, t["description"], t["txn_id"]))

        if loan_code and account.code == loan_code and amt < 0 and monthly_rate > 0:
            payment = -amt
            interest = min(payment, int(round(loan_balance * monthly_rate)))
            principal = payment - interest
            loan_balance -= principal
            lines.append(JournalLine(eid, t["date"], loan_code, principal, "Loan principal", t["txn_id"]))
            if interest:
                lines.append(JournalLine(eid, t["date"], INTEREST_EXPENSE, interest, "Loan interest",
                                         t["txn_id"]))
            loan_schedule.append({"date": str(t["date"]), "txn_id": t["txn_id"], "payment": payment / 100,
                                  "interest": interest / 100, "principal": principal / 100,
                                  "balance_after": loan_balance / 100})
        else:
            lines.append(JournalLine(eid, t["date"], account.code, -amt, t["description"], t["txn_id"]))
    return lines, loan_schedule


def depreciation_entries(df: pd.DataFrame, coa: ChartOfAccounts, config: CompanyConfig) -> list[JournalLine]:
    period_days = (config.period_end - config.period_start).days + 1
    year_fraction = min(1.0, period_days / 365)
    total = to_cents(config.opening_equipment_annual_depreciation * year_fraction)

    equipment_name = coa.by_code[EQUIPMENT].name
    life = max(1, config.new_equipment_useful_life_months)
    for t in df[df["category"] == equipment_name].to_dict("records"):
        cost = -to_cents(t["amount"])  # purchases are outflows
        if cost <= 0:
            continue
        months = min(life, _months_in_service(t["date"], config.period_end))
        total += int(round(cost * months / life))

    if total == 0:
        return []
    return [
        JournalLine("DEPR", config.period_end, DEPRECIATION_EXPENSE, total, "Depreciation for the period"),
        JournalLine("DEPR", config.period_end, ACCUMULATED_DEPRECIATION, -total, "Depreciation for the period"),
    ]


def build_journal(df: pd.DataFrame, coa: ChartOfAccounts,
                  config: CompanyConfig) -> tuple[list[JournalLine], list[dict]]:
    txn_lines, loan_schedule = transaction_entries(df, coa, config)
    journal = opening_entries(config, coa) + txn_lines + depreciation_entries(df, coa, config)
    return journal, loan_schedule


def journal_frame(journal: list[JournalLine], coa: ChartOfAccounts) -> pd.DataFrame:
    rows = []
    for line in journal:
        rows.append({
            "entry_id": line.entry_id, "date": line.date, "txn_id": line.txn_id,
            "account_code": line.account, "account": coa.by_code[line.account].name,
            "debit": line.amount_cents / 100 if line.amount_cents > 0 else None,
            "credit": -line.amount_cents / 100 if line.amount_cents < 0 else None,
            "memo": line.memo,
        })
    return pd.DataFrame(rows)
