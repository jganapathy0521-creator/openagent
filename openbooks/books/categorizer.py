"""Categorize transactions: vendor rules first, then an LLM with structured output.

The LLM is constrained to the chart of accounts with a Literal type, so it
cannot invent a category. Every result carries a confidence score; anything
below the review threshold (or Uncategorized) goes to the human review queue.

Pass llm=None (or use --offline on the CLI) to run with rules only: unmatched
transactions become Uncategorized and are flagged for review.
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal

import pandas as pd
from langsmith import traceable
from pydantic import BaseModel, Field, create_model

from books.chart_of_accounts import ChartOfAccounts
from books.prompts import PROMPT_VERSION, SYSTEM_PROMPT, format_examples
from books.rules import Rule, match_rule

DEFAULT_MODEL = os.getenv("OPENBOOKS_MODEL", "claude-sonnet-5-5")

RESULT_COLUMNS = ["category", "confidence", "source", "reasoning", "needs_review"]


def make_output_schema(category_names: list[str]) -> type[BaseModel]:
    """Pydantic schema whose `category` field only accepts names from the chart."""
    CategoryLiteral = Literal[tuple(category_names)]  # type: ignore[valid-type]
    Item = create_model(
        "TransactionCategory",
        txn_id=(str, Field(description="The txn_id exactly as given")),
        category=(CategoryLiteral, Field(description="One category name from the chart of accounts")),
        confidence=(float, Field(description="Probability from 0 to 1 that the category is correct")),
        reasoning=(str, Field(description="One short sentence")),
    )
    return create_model("CategorizedBatch", results=(list[Item], ...))


def build_llm(coa: ChartOfAccounts, model: str | None = None):
    """LangChain chat model with structured output. Traced to LangSmith automatically."""
    from langchain_anthropic import ChatAnthropic

    chat = ChatAnthropic(model=model or DEFAULT_MODEL, max_tokens=8000, max_retries=3)
    # json_schema = Anthropic native structured outputs (forced tool calling is not
    # supported on newer models). Numeric bounds are enforced in code, not the schema.
    return chat.with_structured_output(make_output_schema(coa.categorizable_names()), method="json_schema")


def build_messages(coa: ChartOfAccounts, txns: list[dict]) -> list[tuple[str, str]]:
    system = SYSTEM_PROMPT.format(
        chart=coa.prompt_listing(),
        uncategorized=coa.uncategorized_name,
        examples=format_examples(),
    )
    lines = [
        json.dumps({"txn_id": t["txn_id"], "date": str(t["date"]),
                    "description": t["description"], "amount": round(float(t["amount"]), 2)})
        for t in txns
    ]
    human = "Categorize these transactions:\n" + "\n".join(lines)
    return [("system", system), ("human", human)]


@traceable(name="llm_categorize_batch", run_type="chain")
def llm_categorize_batch(llm, coa: ChartOfAccounts, txns: list[dict]) -> dict[str, dict]:
    """One LLM call for a batch. Returns {txn_id: {category, confidence, reasoning}}."""
    wanted = {t["txn_id"] for t in txns}
    response: Any = llm.invoke(build_messages(coa, txns))
    results = response.results if hasattr(response, "results") else response["results"]
    out = {}
    for r in results:
        r = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        if r["txn_id"] in wanted:
            out[r["txn_id"]] = {
                "category": r["category"],
                "confidence": min(1.0, max(0.0, float(r["confidence"]))),
                "reasoning": r["reasoning"],
            }
    return out


class Categorizer:
    def __init__(self, coa: ChartOfAccounts, rules: list[Rule] | None = None, llm=None,
                 batch_size: int = 40, review_threshold: float = 0.8, use_rules: bool = True,
                 model_name: str | None = None):
        self.coa = coa
        self.rules = rules or []
        self.llm = llm
        self.batch_size = batch_size
        self.review_threshold = review_threshold
        self.use_rules = use_rules
        self.model_name = model_name or (DEFAULT_MODEL if llm is not None else "offline")

    @property
    def metadata(self) -> dict:
        return {"model": self.model_name, "prompt_version": PROMPT_VERSION,
                "use_rules": self.use_rules, "batch_size": self.batch_size,
                "review_threshold": self.review_threshold}

    @traceable(name="categorize_transactions", run_type="chain")
    def categorize(self, df: pd.DataFrame) -> pd.DataFrame:
        results: dict[str, dict] = {}
        pending: list[dict] = []
        for t in df.to_dict("records"):
            rule = match_rule(t["description"], t["amount"], self.rules) if self.use_rules else None
            if rule:
                results[t["txn_id"]] = {"category": rule.category, "confidence": 1.0, "source": "rule",
                                        "reasoning": f"Vendor rule /{rule.pattern.pattern}/"}
            else:
                pending.append(t)

        if self.llm is not None:
            for start in range(0, len(pending), self.batch_size):
                batch = pending[start:start + self.batch_size]
                got = self._call_llm(batch)
                missing = [t for t in batch if t["txn_id"] not in got]
                if missing:  # one retry for anything the model skipped
                    got.update(self._call_llm(missing))
                for t in batch:
                    if t["txn_id"] in got:
                        results[t["txn_id"]] = {**got[t["txn_id"]], "source": "llm"}

        for t in pending:
            if t["txn_id"] not in results:
                results[t["txn_id"]] = {
                    "category": self.coa.uncategorized_name, "confidence": 0.0,
                    "source": "unmatched" if self.llm is None else "llm_failed",
                    "reasoning": "No rule matched" + ("" if self.llm is None else " and the LLM returned no result"),
                }

        out = df.copy()
        for col in ("category", "confidence", "source", "reasoning"):
            out[col] = [results[i][col] for i in out["txn_id"]]
        out["needs_review"] = (out["confidence"] < self.review_threshold) | (
            out["category"] == self.coa.uncategorized_name)
        return out

    def _call_llm(self, batch: list[dict]) -> dict[str, dict]:
        try:
            return llm_categorize_batch(self.llm, self.coa, batch)
        except Exception as e:  # noqa: BLE001 - surface but keep going; rows fall to review
            print(f"  ! LLM batch of {len(batch)} failed: {type(e).__name__}: {e}")
            return {}


def apply_corrections(df: pd.DataFrame, corrections: pd.DataFrame, coa: ChartOfAccounts) -> pd.DataFrame:
    """Apply human corrections from a reviewed review_queue.csv (txn_id, corrected_category)."""
    fixes = corrections.dropna(subset=["corrected_category"])
    fixes = fixes[fixes["corrected_category"].astype(str).str.strip() != ""]
    valid = set(coa.categorizable_names())
    out = df.copy()
    for _, row in fixes.iterrows():
        cat = str(row["corrected_category"]).strip()
        if cat not in valid:
            raise ValueError(f"Correction for {row['txn_id']}: {cat!r} is not a category in the chart")
        mask = out["txn_id"] == row["txn_id"]
        out.loc[mask, ["category", "confidence", "source", "reasoning", "needs_review"]] = [
            cat, 1.0, "human", "Corrected by reviewer", False]
    return out
