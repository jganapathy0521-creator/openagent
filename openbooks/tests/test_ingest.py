from datetime import date

import pandas as pd
import pytest

from books import DATA_DIR
from books.config import CompanyConfig
from books.ingest import ingest, load_transactions, parse_money, validate

START, END = date(2026, 1, 1), date(2026, 12, 31)


@pytest.mark.parametrize("raw,expected", [
    ("$1,234.56", 1234.56), ("(12.00)", -12.0), ("-7.5", -7.5), ("12.00-", -12.0),
    ("", None), ("-", None), (None, None), (42, 42.0), ("100.00 DR", -100.0), ("100.00CR", 100.0),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_csv_and_xlsx_load_identically():
    a = load_transactions(DATA_DIR / "sample_transactions_2026.csv")
    b = load_transactions(DATA_DIR / "sample_transactions_2026.xlsx")  # has a 3-line bank preamble
    assert len(a) == len(b) == len(pd.read_csv(DATA_DIR / "sample_truth_2026.csv"))
    assert list(a["amount"]) == pytest.approx(list(b["amount"]))
    assert list(a["description"]) == list(b["description"])


def test_sample_file_validates_clean():
    cfg = CompanyConfig.load()
    _, report = ingest(DATA_DIR / "sample_transactions_2026.csv", START, END, cfg.opening_cash)
    assert report.ok, report.errors
    assert report.stats["implied_opening_balance"] == cfg.opening_cash


def _write(tmp_path, rows, name="bank.csv"):
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_newest_first_is_reordered(tmp_path):
    path = _write(tmp_path, [
        {"Date": "2026-01-03", "Description": "C", "Amount": "-5", "Balance": "95"},
        {"Date": "2026-01-02", "Description": "B", "Amount": "50", "Balance": "100"},
        {"Date": "2026-01-01", "Description": "A", "Amount": "-50", "Balance": "50"},
    ])
    df = load_transactions(path)
    assert list(df["description"]) == ["A", "B", "C"]
    assert validate(df, START, END, opening_cash=100).ok


def test_missing_row_breaks_running_balance(tmp_path):
    path = _write(tmp_path, [
        {"Date": "2026-01-01", "Description": "A", "Amount": "-50", "Balance": "50"},
        # a -10 transaction is missing here
        {"Date": "2026-01-03", "Description": "C", "Amount": "-5", "Balance": "35"},
    ])
    _, report = ingest(path, START, END, opening_cash=100)
    assert not report.ok
    assert any("Running balance" in e for e in report.errors)


def test_out_of_period_and_duplicates(tmp_path):
    path = _write(tmp_path, [
        {"Date": "2025-12-31", "Description": "LAST YEAR", "Withdrawals": "10", "Deposits": "", "Balance": "90"},
        {"Date": "2026-01-05", "Description": "STARBUCKS", "Withdrawals": "5", "Deposits": "", "Balance": "85"},
        {"Date": "2026-01-05", "Description": "STARBUCKS", "Withdrawals": "5", "Deposits": "", "Balance": "80"},
    ])
    _, report = ingest(path, START, END, opening_cash=100)
    assert any("outside" in e for e in report.errors)
    assert any("duplicates" in w for w in report.warnings)
    assert any("partial period" in w for w in report.warnings)


def test_opening_cash_mismatch_is_an_error(tmp_path):
    path = _write(tmp_path, [{"Date": "2026-01-01", "Description": "A", "Amount": "-50", "Balance": "50"}])
    _, report = ingest(path, START, END, opening_cash=999)
    assert any("opening balance" in e.lower() for e in report.errors)
