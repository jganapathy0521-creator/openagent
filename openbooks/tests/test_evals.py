"""The eval suite itself: perfect answers must score perfectly, and the reference must be self-consistent."""
import pytest

from evals import evaluators as ev
from evals.datasets import categorization_examples, statement_examples
from evals.run_evals import run_local
from evals.targets import OracleCategorizer, run_scenario


def test_golden_labels_are_valid_categories():
    from books.chart_of_accounts import ChartOfAccounts
    names = set(ChartOfAccounts.load().categorizable_names())
    for ex in categorization_examples():
        assert ex["outputs"]["category"] in names, ex


def test_categorization_evaluators_on_perfect_and_wrong():
    ref = {"category": "Owner Draws"}
    assert ev.category_correct({"category": "Owner Draws"}, ref)
    assert ev.high_confidence_error({"category": "Rent", "needs_review": False}, ref)["score"] == 1
    assert ev.high_confidence_error({"category": "Rent", "needs_review": True}, ref)["score"] == 0
    assert ev.error_caught({"category": "Rent", "needs_review": True}, ref)["score"] == 1
    outs = [{"category": "Owner Draws"}, {"category": "Rent"}]
    refs = [{"category": "Owner Draws"}, {"category": "Meals"}]
    assert ev.accuracy(outs, refs)["score"] == 0.5
    assert ev.critical_category_accuracy(outs, refs)["score"] == 1.0


@pytest.mark.parametrize("example", statement_examples(), ids=lambda e: e["inputs"]["scenario"])
def test_oracle_scores_perfectly_on_statement_scenarios(example):
    truth = example["outputs"]["truth"]
    metrics, _ = run_local([example], lambda inputs: run_scenario(inputs, OracleCategorizer(truth)),
                           ev.STATEMENT_EVALUATORS)
    for key, score in metrics.items():
        assert score == pytest.approx(1.0), key
