"""Run the eval suite, in LangSmith or locally.

    # LangSmith experiments (needs LANGSMITH_API_KEY + ANTHROPIC_API_KEY)
    python -m evals.run_evals categorization --experiment-prefix sonnet-prompt-v1
    python -m evals.run_evals categorization --llm-only --judge      # test the LLM without vendor rules
    python -m evals.run_evals statements

    # Same evaluators, no LangSmith upload (good for CI and quick checks)
    python -m evals.run_evals categorization --local
    python -m evals.run_evals statements --local --offline           # no API keys at all

Exit code is 1 when a threshold is missed, so this can gate a CI job.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from evals import evaluators as ev
from evals.datasets import (CATEGORIZATION_DATASET, STATEMENTS_DATASET, categorization_examples,
                            statement_examples)
from evals.targets import build_categorizer, make_categorization_target, make_statements_target

THRESHOLDS = {
    "categorization": {"accuracy": 0.90, "critical_category_accuracy": 0.95, "high_confidence_error": 0.03},
    "statements": {"balance_sheet_balances": 1.0, "cash_ties_to_bank": 1.0, "all_transactions_posted": 1.0,
                   "net_income_accuracy": 0.98},
}
# For these keys lower is better (threshold is a maximum).
LOWER_IS_BETTER = {"high_confidence_error"}


def _score(result) -> tuple[str, float]:
    """Normalize an evaluator return value (bool | dict | EvaluationResult) to (key, score)."""
    if isinstance(result, dict):
        return result["key"], float(result["score"])
    if hasattr(result, "key"):
        return result.key, float(result.score)
    raise TypeError(result)


def run_local(examples, target, evaluators, summary_evaluators=(), batch_target=None):
    import inspect

    if batch_target:
        outputs = batch_target(examples)
    else:
        outputs = [target(ex["inputs"]) for ex in examples]
    refs = [ex["outputs"] for ex in examples]
    per_key: dict[str, list[float]] = {}
    for ex, out, ref in zip(examples, outputs, refs):
        for fn in evaluators:
            params = inspect.signature(fn).parameters
            kwargs = {k: v for k, v in (("inputs", ex["inputs"]), ("outputs", out), ("reference_outputs", ref))
                      if k in params}
            res = fn(**kwargs)
            key, score = (fn.__name__, float(res)) if isinstance(res, bool) else _score(res)
            per_key.setdefault(key, []).append(score)
            if isinstance(res, dict) and res.get("comment") and score < 1 and key != "high_confidence_error":
                print(f"  {ex['inputs'].get('scenario', '')} {key}: {res['comment']}")
    metrics = {k: sum(v) / len(v) for k, v in per_key.items()}
    for fn in summary_evaluators:
        key, score = _score(fn(outputs, refs))
        metrics[key] = score
    return metrics, outputs


def run_langsmith(dataset, target, evaluators, summary_evaluators, prefix, metadata, concurrency):
    from langsmith import Client

    client = Client()
    results = client.evaluate(
        target, data=dataset, evaluators=evaluators, summary_evaluators=list(summary_evaluators),
        experiment_prefix=prefix, metadata=metadata, max_concurrency=concurrency,
    )
    per_key: dict[str, list[float]] = {}
    outputs, refs = [], []
    for row in results:
        outputs.append(row["run"].outputs or {})
        refs.append(row["example"].outputs)
        for r in row["evaluation_results"]["results"]:
            if r.score is not None:
                per_key.setdefault(r.key, []).append(float(r.score))
    metrics = {k: sum(v) / len(v) for k, v in per_key.items()}
    for fn in summary_evaluators:  # recompute locally so thresholds don't depend on SDK internals
        key, score = _score(fn(outputs, refs))
        metrics[key] = score
    print(f"\nExperiment: {results.experiment_name}  (open it in LangSmith to see every trace)")
    return metrics, outputs


def check_thresholds(metrics: dict, thresholds: dict) -> bool:
    ok = True
    print("\nMetric                          Score    Threshold")
    for key in sorted(metrics):
        score = metrics[key]
        line = f"{key:<30} {score:7.3f}"
        if key in thresholds:
            limit = thresholds[key]
            passed = score <= limit if key in LOWER_IS_BETTER else score >= limit
            ok &= passed
            line += f"   {'<=' if key in LOWER_IS_BETTER else '>='} {limit:<6} {'PASS' if passed else 'FAIL'}"
        print(line)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return ok


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("suite", choices=["categorization", "statements"])
    p.add_argument("--local", action="store_true", help="Run evaluators locally; don't upload to LangSmith")
    p.add_argument("--offline", action="store_true", help="No LLM: vendor rules only (implies --local)")
    p.add_argument("--llm-only", action="store_true", help="Disable vendor rules to measure the LLM alone")
    p.add_argument("--judge", action="store_true", help="Add the LLM-as-judge evaluator (categorization)")
    p.add_argument("--model", default=None)
    p.add_argument("--experiment-prefix", default=None)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--no-gate", action="store_true", help="Always exit 0 (report only)")
    args = p.parse_args(argv)

    if not args.offline and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set -> running --offline (rules only).")
        args.offline = True
    if args.offline:
        args.local = True
    if not args.local and not os.getenv("LANGSMITH_API_KEY"):
        print("LANGSMITH_API_KEY not set -> running --local.")
        args.local = True

    categorizer = build_categorizer(offline=args.offline, use_rules=not args.llm_only, model=args.model)
    metadata = {**categorizer.metadata, "suite": args.suite}
    prefix = args.experiment_prefix or f"{args.suite}-{categorizer.metadata['model']}-{metadata['prompt_version']}"
    print(f"Suite: {args.suite} | categorizer: {categorizer.metadata} | {'local' if args.local else 'LangSmith'}")

    if args.suite == "categorization":
        evaluators = list(ev.CATEGORIZATION_EVALUATORS)
        if args.judge and not args.offline:
            from evals.judge import category_defensible
            evaluators.append(category_defensible)
        target = make_categorization_target(categorizer)
        if args.local:
            examples = categorization_examples()

            def batch_target(exs):  # one batched call instead of one call per example
                from books.ingest import from_records
                df = from_records([ex["inputs"] for ex in exs])
                cat = categorizer.categorize(df)
                return [{"category": r.category, "confidence": float(r.confidence), "source": r.source,
                         "reasoning": r.reasoning, "needs_review": bool(r.needs_review)}
                        for r in cat.itertuples()]

            metrics, outputs = run_local(examples, target, evaluators, ev.CATEGORIZATION_SUMMARY_EVALUATORS,
                                         batch_target=batch_target)
            misses = Counter((ex["outputs"]["category"], o["category"]) for ex, o in zip(examples, outputs)
                             if o["category"] != ex["outputs"]["category"])
            if misses:
                print("Most common confusions (expected -> got):")
                for (want, got), n in misses.most_common(8):
                    print(f"  {n:3d}  {want} -> {got}")
        else:
            metrics, _ = run_langsmith(CATEGORIZATION_DATASET, target, evaluators,
                                       ev.CATEGORIZATION_SUMMARY_EVALUATORS, prefix, metadata, args.concurrency)
    else:
        target = make_statements_target(categorizer)
        if args.local:
            metrics, _ = run_local(statement_examples(), target, ev.STATEMENT_EVALUATORS)
        else:
            metrics, _ = run_langsmith(STATEMENTS_DATASET, target, ev.STATEMENT_EVALUATORS, [], prefix,
                                       metadata, 1)

    ok = check_thresholds(metrics, THRESHOLDS[args.suite])
    return 0 if ok or args.no_gate else 1


if __name__ == "__main__":
    sys.exit(main())
