"""Load eval examples from data/ and (optionally) upload them to LangSmith.

    python -m evals.datasets              # create the two LangSmith datasets if missing
    python -m evals.datasets --recreate   # delete and re-upload (after editing the CSV/JSON)
"""
from __future__ import annotations

import argparse
import csv
import json

from books import DATA_DIR
from evals.targets import reference_statements

CATEGORIZATION_DATASET = "openbooks-categorization-golden"
STATEMENTS_DATASET = "openbooks-statements-scenarios"


def categorization_examples() -> list[dict]:
    with open(DATA_DIR / "golden_transactions.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    return [{
        "inputs": {"txn_id": r["txn_id"], "date": r["date"], "description": r["description"],
                   "amount": float(r["amount"])},
        "outputs": {"category": r["category"]},
        "metadata": {"difficulty": r["difficulty"], "note": r["note"]},
        "split": r["difficulty"],
    } for r in rows]


def statement_examples() -> list[dict]:
    examples = []
    for path in sorted((DATA_DIR / "scenarios").glob("*.json")):
        s = json.loads(path.read_text())
        examples.append({
            "inputs": {"scenario": s["name"], "config": s["config"], "transactions": s["transactions"]},
            "outputs": reference_statements(s),
            "metadata": {"description": s["description"], "transactions": len(s["transactions"])},
        })
    return examples


def upload(name: str, description: str, examples: list[dict], recreate: bool) -> None:
    from langsmith import Client

    client = Client()
    if client.has_dataset(dataset_name=name):
        if not recreate:
            print(f"Dataset {name!r} already exists (use --recreate to replace it)")
            return
        client.delete_dataset(dataset_name=name)
    ds = client.create_dataset(dataset_name=name, description=description)
    for start in range(0, len(examples), 50):
        client.create_examples(dataset_id=ds.id, examples=examples[start:start + 50])
    print(f"Uploaded {len(examples)} examples to {name!r}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--recreate", action="store_true")
    args = p.parse_args()
    upload(CATEGORIZATION_DATASET, "Labeled bank transactions -> chart-of-accounts category",
           categorization_examples(), args.recreate)
    upload(STATEMENTS_DATASET, "Full-year bank exports -> reference P&L and Balance Sheet",
           statement_examples(), args.recreate)


if __name__ == "__main__":
    main()  # importing evals.targets -> books.pipeline already loaded .env
