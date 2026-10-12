"""Chart of accounts: the fixed list of categories the AI is allowed to choose from.

Each account says which statement it lands on (PL = Profit & Loss,
BS = Balance Sheet), its section, and its type, which decides whether the
account normally carries a debit or a credit balance.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from books import CONFIG_DIR

# Special accounts the ledger posts to directly.
CASH = "1000"
SAVINGS = "1010"
EQUIPMENT = "1500"
ACCUMULATED_DEPRECIATION = "1510"
LOAN_PAYABLE = "2100"
INTEREST_EXPENSE = "6200"
DEPRECIATION_EXPENSE = "6300"
UNCATEGORIZED = "6900"

DEBIT_NORMAL = {"asset", "expense", "other_expense"}
CREDIT_NORMAL = {"contra_asset", "liability", "equity", "revenue", "other_income"}
VALID_TYPES = DEBIT_NORMAL | CREDIT_NORMAL

PL_SECTIONS = ["Revenue", "Operating Expenses", "Other Income", "Other Expenses"]
BS_SECTIONS = ["Current Assets", "Fixed Assets", "Liabilities", "Equity"]


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    statement: str  # "PL" or "BS"
    section: str
    type: str
    categorizable: bool
    description: str

    @property
    def sign(self) -> int:
        """+1 if a positive natural balance is a debit, -1 if it is a credit."""
        return 1 if self.type in DEBIT_NORMAL else -1


class ChartOfAccounts:
    def __init__(self, accounts: list[Account]):
        self.accounts = list(accounts)
        self.by_code = {a.code: a for a in self.accounts}
        self.by_name = {a.name: a for a in self.accounts}
        self._validate()

    def _validate(self) -> None:
        if len(self.by_code) != len(self.accounts):
            raise ValueError("Duplicate account codes in chart of accounts")
        if len(self.by_name) != len(self.accounts):
            raise ValueError("Duplicate account names in chart of accounts")
        for a in self.accounts:
            if a.type not in VALID_TYPES:
                raise ValueError(f"Account {a.code} has unknown type {a.type!r}")
            if a.statement not in ("PL", "BS"):
                raise ValueError(f"Account {a.code} has unknown statement {a.statement!r}")
            sections = PL_SECTIONS if a.statement == "PL" else BS_SECTIONS
            if a.section not in sections:
                raise ValueError(f"Account {a.code} has unknown section {a.section!r}")
        for required in (CASH, EQUIPMENT, ACCUMULATED_DEPRECIATION, LOAN_PAYABLE,
                         INTEREST_EXPENSE, DEPRECIATION_EXPENSE, UNCATEGORIZED):
            if required not in self.by_code:
                raise ValueError(f"Chart of accounts is missing required account {required}")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ChartOfAccounts":
        path = Path(path) if path else CONFIG_DIR / "chart_of_accounts.csv"
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return cls([
            Account(
                code=r["code"].strip(),
                name=r["name"].strip(),
                statement=r["statement"].strip(),
                section=r["section"].strip(),
                type=r["type"].strip(),
                categorizable=r["categorizable"].strip().lower() in ("yes", "true", "1"),
                description=r["description"].strip(),
            )
            for r in rows
        ])

    def get(self, code_or_name: str) -> Account:
        if code_or_name in self.by_code:
            return self.by_code[code_or_name]
        if code_or_name in self.by_name:
            return self.by_name[code_or_name]
        raise KeyError(f"Unknown account {code_or_name!r}")

    def categorizable(self) -> list[Account]:
        return [a for a in self.accounts if a.categorizable]

    def categorizable_names(self) -> list[str]:
        return [a.name for a in self.categorizable()]

    @property
    def uncategorized_name(self) -> str:
        return self.by_code[UNCATEGORIZED].name

    def prompt_listing(self) -> str:
        """The chart of accounts as the LLM sees it."""
        lines = []
        for a in self.categorizable():
            where = "Profit & Loss" if a.statement == "PL" else "Balance Sheet"
            lines.append(f"- {a.name} [{where} / {a.section}]: {a.description}")
        return "\n".join(lines)
