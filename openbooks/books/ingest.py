"""Load a bank export (CSV or Excel), normalize it, and validate it.

Bank exports vary a lot, so this module:
  * finds the header row even when the file starts with a few preamble lines,
  * maps common column names (Date / Posted Date, Description / Memo, ...),
  * handles a single signed Amount column OR separate Withdrawals/Deposits,
  * parses "$1,234.56", "(12.00)" and "-12.00" style money values,
  * reorders newest-first exports to oldest-first.

Validation runs before any AI is called. Errors block the pipeline (unless
forced); warnings are reported alongside the statements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date", "transaction date", "posted date", "posting date", "trans date", "post date"],
    "description": ["description", "transaction description", "memo", "details", "payee", "name", "narrative"],
    "amount": ["amount", "transaction amount", "amt", "net amount"],
    "debit": ["withdrawals", "withdrawal", "debit", "debits", "money out", "withdrawal amount", "debit amount"],
    "credit": ["deposits", "deposit", "credit", "credits", "money in", "deposit amount", "credit amount"],
    "balance": ["balance", "running balance", "ledger balance", "available balance"],
}

NORMALIZED_COLUMNS = ["txn_id", "date", "description", "amount", "balance"]


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "stats": self.stats}

    def __str__(self) -> str:
        lines = [f"Validation: {'OK' if self.ok else 'FAILED'}"]
        lines += [f"  ERROR: {e}" for e in self.errors]
        lines += [f"  warning: {w}" for w in self.warnings]
        for k, v in self.stats.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


_MONEY_CLEAN = re.compile(r"[,$\s€£]")


def parse_money(value) -> float | None:
    """Parse a money cell. Returns None for blanks; raises ValueError for garbage."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return None
        return float(value)
    s = str(value).strip()
    if s in ("", "-", "—", "nan", "None"):
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("-"):  # some banks print "12.00-"
        negative, s = True, s[:-1]
    s = _MONEY_CLEAN.sub("", s)
    if s.upper().endswith("CR"):
        s = s[:-2]
    elif s.upper().endswith("DR"):
        negative, s = True, s[:-2]
    v = float(s)
    return -abs(v) if negative else v


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _match_columns(header: list) -> dict[str, int]:
    """Map logical column -> index for a candidate header row."""
    found: dict[str, int] = {}
    normalized = [_norm(h) for h in header]
    for logical, aliases in COLUMN_ALIASES.items():
        for i, h in enumerate(normalized):
            if h in aliases and i not in found.values():
                found[logical] = i
                break
    return found


def _read_raw(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, header=None, dtype=object)
    if suffix in (".csv", ".txt"):
        return pd.read_csv(path, header=None, dtype=str, keep_default_na=False,
                           skip_blank_lines=False, on_bad_lines="skip")
    raise ValueError(f"Unsupported file type {suffix!r}; use .csv or .xlsx")


def load_transactions(path: str | Path) -> pd.DataFrame:
    """Read a bank export and return the normalized transaction table."""
    raw = _read_raw(Path(path))
    for header_idx in range(min(20, len(raw))):
        cols = _match_columns(list(raw.iloc[header_idx]))
        has_amount = "amount" in cols or ("debit" in cols and "credit" in cols)
        if "date" in cols and "description" in cols and has_amount:
            body = raw.iloc[header_idx + 1:].reset_index(drop=True)
            return normalize(body, cols)
    raise ValueError(
        "Could not find a header row with date, description and amount "
        "(or withdrawals/deposits) columns in the first 20 rows. "
        f"Recognized names: {COLUMN_ALIASES}"
    )


def normalize(body: pd.DataFrame, cols: dict[str, int]) -> pd.DataFrame:
    records = []
    bad_rows = []
    for i, row in body.iterrows():
        raw_date = row.iloc[cols["date"]]
        desc = row.iloc[cols["description"]]
        try:
            if "amount" in cols:
                amount = parse_money(row.iloc[cols["amount"]])
            else:
                debit = parse_money(row.iloc[cols["debit"]])
                credit = parse_money(row.iloc[cols["credit"]])
                amount = None if debit is None and credit is None else (credit or 0.0) - abs(debit or 0.0)
            balance = parse_money(row.iloc[cols["balance"]]) if "balance" in cols else None
        except (ValueError, TypeError):
            bad_rows.append(i)
            amount, balance = None, None
        blank_date = raw_date is None or (isinstance(raw_date, float) and pd.isna(raw_date)) or str(raw_date).strip() == ""
        if blank_date and amount is None:
            continue  # blank/footer line
        records.append({
            "raw_date": raw_date,
            "description": "" if desc is None or (isinstance(desc, float) and pd.isna(desc)) else str(desc).strip(),
            "amount": amount,
            "balance": balance,
            "source_row": int(i),
            "unparsed_money": i in bad_rows,
        })
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)
    df["date"] = pd.to_datetime(df["raw_date"], errors="coerce", format="mixed").dt.date
    # Newest-first exports: flip to chronological order (stable within a day).
    dated = df.dropna(subset=["date"])
    if len(dated) > 1 and dated["date"].iloc[0] > dated["date"].iloc[-1]:
        df = df.iloc[::-1].reset_index(drop=True)
    df["txn_id"] = [f"T{i + 1:04d}" for i in range(len(df))]
    return df[NORMALIZED_COLUMNS + ["raw_date", "source_row", "unparsed_money"]]


def validate(df: pd.DataFrame, period_start: date, period_end: date,
             opening_cash: float | None = None) -> ValidationReport:
    r = ValidationReport()
    if df.empty:
        r.errors.append("No transactions found")
        return r

    bad_dates = df[df["date"].isna()]
    if len(bad_dates):
        r.errors.append(f"{len(bad_dates)} row(s) have unreadable dates, e.g. {list(bad_dates['raw_date'][:3])}")
    if "unparsed_money" in df:
        bad_money = df[df["unparsed_money"] | df["amount"].isna()]
    else:
        bad_money = df[df["amount"].isna()]
    if len(bad_money):
        r.errors.append(f"{len(bad_money)} row(s) have unreadable amounts: {list(bad_money['txn_id'][:5])}")

    ok = df.dropna(subset=["date", "amount"])
    outside = ok[(ok["date"] < period_start) | (ok["date"] > period_end)]
    if len(outside):
        r.errors.append(
            f"{len(outside)} transaction(s) fall outside {period_start}..{period_end}: "
            f"{list(outside['txn_id'][:5])}"
        )

    zero = ok[ok["amount"] == 0]
    if len(zero):
        r.warnings.append(f"{len(zero)} zero-amount transaction(s): {list(zero['txn_id'][:5])}")

    dupes = ok[ok.duplicated(subset=["date", "description", "amount"], keep=False)]
    if len(dupes):
        r.warnings.append(
            f"{len(dupes)} row(s) share the same date, description and amount (possible duplicates): "
            f"{list(dupes['txn_id'][:6])}"
        )

    if len(ok):
        first, last = ok["date"].min(), ok["date"].max()
        if (first - period_start).days > 7 or (period_end - last).days > 7:
            r.warnings.append(
                f"Data covers {first}..{last}, not the full period {period_start}..{period_end}. "
                "Statements will be for a partial period."
            )

    cents = (ok["amount"] * 100).round().astype("int64")
    implied_opening = None
    ending_balance = None
    if "balance" in ok and ok["balance"].notna().all() and len(ok):
        bal = (ok["balance"] * 100).round().astype("int64").tolist()
        amt = cents.tolist()
        mismatches = [ok["txn_id"].iloc[i] for i in range(1, len(bal)) if bal[i - 1] + amt[i] != bal[i]]
        if mismatches:
            r.errors.append(
                f"Running balance does not reconcile at {len(mismatches)} row(s) "
                f"(missing, duplicated or reordered transactions?): {mismatches[:5]}"
            )
        implied_opening = (bal[0] - amt[0]) / 100
        ending_balance = bal[-1] / 100
    if opening_cash is not None:
        if implied_opening is not None and round(implied_opening - opening_cash, 2) != 0:
            r.errors.append(
                f"Bank opening balance implied by the file ({implied_opening:,.2f}) does not match "
                f"opening cash in the company config ({opening_cash:,.2f})"
            )
        if ending_balance is None:
            ending_balance = round(opening_cash + cents.sum() / 100, 2)

    r.stats = {
        "transactions": int(len(df)),
        "first_date": str(ok["date"].min()) if len(ok) else None,
        "last_date": str(ok["date"].max()) if len(ok) else None,
        "money_in": round(float(ok.loc[ok["amount"] > 0, "amount"].sum()), 2),
        "money_out": round(float(-ok.loc[ok["amount"] < 0, "amount"].sum()), 2),
        "net_change": round(float(cents.sum()) / 100, 2),
        "implied_opening_balance": implied_opening,
        "ending_balance": ending_balance,
    }
    return r


def ingest(path: str | Path, period_start: date, period_end: date,
           opening_cash: float | None = None) -> tuple[pd.DataFrame, ValidationReport]:
    df = load_transactions(path)
    return df, validate(df, period_start, period_end, opening_cash)


def from_records(records: list[dict]) -> pd.DataFrame:
    """Build a normalized frame from already-clean records (used by the eval suite)."""
    df = pd.DataFrame.from_records(records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if "balance" not in df:
        df["balance"] = None
    if "txn_id" not in df:
        df["txn_id"] = [f"T{i + 1:04d}" for i in range(len(df))]
    df["raw_date"] = df["date"].astype(str)
    df["source_row"] = range(len(df))
    df["unparsed_money"] = False
    return df[NORMALIZED_COLUMNS + ["raw_date", "source_row", "unparsed_money"]]
