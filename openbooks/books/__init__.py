"""openbooks: AI-assisted bookkeeping from a bank export.

Pipeline: ingest -> validate -> categorize (rules, then LLM) -> double-entry
journal -> Profit & Loss + Balance Sheet (+ reconciliation checks) -> Excel.

The LLM only chooses categories. Every number on the statements is computed
by plain Python so the math is deterministic and auditable.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
