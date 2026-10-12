"""Optional LLM-as-judge evaluator (enable with --judge).

Exact-match accuracy treats every disagreement as an error, but some labels
are genuinely debatable (is a $1,100 monitor Equipment or Office Supplies?).
The judge looks only at disagreements and says whether the model's answer is
a defensible alternative or a real mistake. Use it to triage, not to replace
exact match: a high "defensible" rate means your labels or chart of accounts
need tightening, not that the model is right.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field

from books.chart_of_accounts import ChartOfAccounts

JUDGE_MODEL = os.getenv("OPENBOOKS_JUDGE_MODEL", "claude-haiku-5-5")

JUDGE_PROMPT = """You are reviewing a bookkeeping categorization.

Chart of accounts:
{chart}

Transaction: description="{description}", amount={amount:.2f} (positive = money in)
Reference (accountant) category: {reference}
Model's category: {predicted}
Model's reasoning: {reasoning}

Is the model's category a DEFENSIBLE alternative that a competent bookkeeper could reasonably \
choose, or a MISTAKE? Moving a transaction between the Balance Sheet and the Profit & Loss \
(e.g. an owner draw booked as an expense, or a loan payment booked as an expense) is always a mistake."""


class Verdict(BaseModel):
    defensible: bool
    reasoning: str = Field(description="One short sentence")


@lru_cache(maxsize=1)
def _judge():
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model=JUDGE_MODEL, max_tokens=500).with_structured_output(Verdict, method="json_schema")


@lru_cache(maxsize=1)
def _chart() -> str:
    return ChartOfAccounts.load().prompt_listing()


def category_defensible(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    if outputs.get("category") == reference_outputs["category"]:
        return {"key": "category_defensible", "score": 1, "comment": "exact match"}
    verdict: Verdict = _judge().invoke(JUDGE_PROMPT.format(
        chart=_chart(), description=inputs["description"], amount=float(inputs["amount"]),
        reference=reference_outputs["category"], predicted=outputs.get("category"),
        reasoning=outputs.get("reasoning", ""),
    ))
    return {"key": "category_defensible", "score": int(verdict.defensible), "comment": verdict.reasoning}
