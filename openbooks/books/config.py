"""Company configuration: period, opening balances, loan and depreciation inputs.

A bank export only shows cash moving. These inputs are what turn it into a
real balance sheet, so they are validated up front.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from books import CONFIG_DIR
from books.chart_of_accounts import ChartOfAccounts


def to_cents(x: float | int | str) -> int:
    return int(round(float(x) * 100))


@dataclass
class CompanyConfig:
    business_name: str
    period_start: date
    period_end: date
    opening_balances: dict[str, float]  # account code -> positive natural amount
    loan_account: str | None = None
    loan_annual_rate: float = 0.0
    opening_equipment_annual_depreciation: float = 0.0
    new_equipment_useful_life_months: int = 36
    batch_size: int = 40
    review_confidence_threshold: float = 0.8
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "CompanyConfig":
        loan = d.get("loan") or {}
        dep = d.get("depreciation") or {}
        cat = d.get("categorizer") or {}
        return cls(
            business_name=d.get("business_name", "My Business"),
            period_start=date.fromisoformat(d["period_start"]),
            period_end=date.fromisoformat(d["period_end"]),
            opening_balances={str(k): float(v) for k, v in (d.get("opening_balances") or {}).items()},
            loan_account=loan.get("account"),
            loan_annual_rate=float(loan.get("annual_interest_rate", 0.0)),
            opening_equipment_annual_depreciation=float(dep.get("opening_equipment_annual_depreciation", 0.0)),
            new_equipment_useful_life_months=int(dep.get("new_equipment_useful_life_months", 36)),
            batch_size=int(cat.get("batch_size", 40)),
            review_confidence_threshold=float(cat.get("review_confidence_threshold", 0.8)),
            raw=d,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CompanyConfig":
        path = Path(path) if path else CONFIG_DIR / "company.json"
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def opening_cash(self) -> float:
        return self.opening_balances.get("1000", 0.0)

    def validate(self, coa: ChartOfAccounts) -> list[str]:
        """Return a list of problems (empty list = OK)."""
        problems = []
        if self.period_end < self.period_start:
            problems.append("period_end is before period_start")
        total = 0
        for code, amount in self.opening_balances.items():
            if code not in coa.by_code:
                problems.append(f"Opening balance for unknown account {code}")
                continue
            if coa.by_code[code].statement != "BS":
                problems.append(f"Opening balance for P&L account {code}; P&L accounts start the year at zero")
            total += coa.by_code[code].sign * to_cents(amount)
        if total != 0:
            problems.append(
                f"Opening balances do not balance: assets - liabilities - equity = {total / 100:,.2f}"
            )
        if self.loan_account and self.loan_account not in coa.by_code:
            problems.append(f"Loan account {self.loan_account} not in chart of accounts")
        return problems
