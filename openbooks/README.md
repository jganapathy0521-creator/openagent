# openbooks — AI bookkeeping from a bank export, verified with LangSmith evals

Upload a business bank export for **Jan 1 – Dec 31, 2026** and get:

- every transaction categorized into a fixed chart of accounts (vendor rules first, then Claude),
- a **Profit & Loss statement** and a **Balance Sheet**,
- reconciliation checks (books balance, cash ties to the bank, every transaction posted),
- a review queue of low-confidence items for a human,
- an Excel workbook with all of the above.

The design rule: **the LLM only chooses categories; plain Python does all the math.** Statements are
sums over a double-entry journal, so they are deterministic and auditable. LangSmith evals then
measure the one non-deterministic step (categorization) and the end result (the statements).

```
bank export (.csv / .xlsx)
   │
   ▼
books/ingest.py ──── validation report (errors block the run)
   │
   ▼
books/categorizer.py ── 1. vendor rules (config/vendor_rules.csv)
   │                    2. Claude, structured output constrained to the chart of accounts
   │                    3. confidence < 0.8 or Uncategorized → review queue
   ▼
books/ledger.py ──── double-entry journal: opening balances, loan principal/interest split, depreciation
   │
   ▼
books/statements.py ─ P&L, Balance Sheet, checks, Excel export
```

## Quick start

From the repo root (one shared environment, see the top-level README):

```bash
uv sync                                  # or: pip install -r openbooks/requirements.txt
cp openbooks/.env.example .env           # add ANTHROPIC_API_KEY and LANGSMITH_API_KEY
cd openbooks

python -m books.pipeline data/sample_transactions_2026.xlsx            # full run with Claude
python -m books.pipeline data/sample_transactions_2026.xlsx --offline  # rules only, no API keys
```

Output lands in `output/`:

| File | What it is |
|---|---|
| `financials_2026.xlsx` | Sheets: Profit & Loss, Balance Sheet, Checks, Transactions (review rows highlighted), Review Queue, Journal, Loan Schedule |
| `statements.json` | Same statements as data, plus the validation report and categorizer settings |
| `review_queue.csv` | Low-confidence rows. Fill in `corrected_category`, then re-run with `--corrections output/review_queue.csv` |

Corrections are matched by `txn_id`, which is the row's position in the file, so re-run against the
same export.

## Using your own data

1. **Company config** — edit `config/company.json`:
   - `period_start` / `period_end`
   - `opening_balances` as of Jan 1, keyed by account code, as positive amounts. They must balance
     (assets = liabilities + equity); the pipeline refuses to run otherwise. Opening cash (`1000`)
     must match the bank's opening balance.
   - `loan.annual_interest_rate` so loan payments can be split into principal and interest.
   - `depreciation` for existing equipment and the useful life of new purchases.
2. **Chart of accounts** — `config/chart_of_accounts.csv`. The `description` column is what the LLM
   reads, so write it like instructions to a new bookkeeper. Only `categorizable=yes` accounts can be chosen.
3. **Vendor rules** — `config/vendor_rules.csv`. Regex on the description plus direction (`in`/`out`/`any`).
   Put recurring, unambiguous vendors here; leave ambiguous ones (Amazon, Uber, Google) to the LLM.
4. **Bank file** — CSV or Excel. The loader finds the header row even below a bank preamble, and
   accepts either a signed `Amount` column or `Withdrawals`/`Deposits`, plus an optional running `Balance`.

### What a bank export can and cannot tell you

A bank export only shows cash moving. This starter produces **cash-basis** statements:

- Unpaid customer invoices (receivables) and unpaid bills (payables) are not visible.
- Credit-card spending is not visible; a card payment from checking would need its own handling.
  (The sample data has no card.)
- Only one loan is modeled, with simple monthly interest on the running balance.
- Depreciation is straight-line, monthly, starting the month after purchase.

## LangSmith evals

### Setup

```bash
export LANGSMITH_API_KEY=...        # or put these in .env
export LANGSMITH_TRACING=true       # every pipeline run is traced
export LANGSMITH_PROJECT=openbooks

python -m evals.datasets            # uploads both datasets (once; --recreate after edits)
```

| Dataset | Examples | Built from |
|---|---|---|
| `openbooks-categorization-golden` | 91 labeled transactions, 32 of them hand-written hard cases (split `hard` / `typical`) | `data/golden_transactions.csv` |
| `openbooks-statements-scenarios` | 2 full-year bank exports with reference P&L and Balance Sheet | `data/scenarios/*.json` |

### Run experiments

```bash
python -m evals.run_evals categorization                        # rules + Claude
python -m evals.run_evals categorization --llm-only --judge     # Claude alone, plus LLM-as-judge
python -m evals.run_evals categorization --model claude-haiku-5-5 --experiment-prefix haiku-v1
python -m evals.run_evals statements
```

Each run is a LangSmith experiment tagged with model, prompt version, and whether rules were on, so
you can compare them side by side. Add `--local` to run the same evaluators without uploading, or
`--offline` to run with no API keys at all.

### What gets measured

**Categorization** (per transaction, then over the whole set)

| Metric | Meaning | Gate |
|---|---|---|
| `accuracy` | Exact match with the label | ≥ 0.90 |
| `critical_category_accuracy` | Accuracy on balance-sheet categories + Uncategorized. These errors move money between the P&L and the Balance Sheet | ≥ 0.95 |
| `high_confidence_error` | Wrong **and** not flagged for review, so it flows into the statements unseen | ≤ 0.03 |
| `error_caught` | Correct, or wrong but flagged for review | — |
| `macro_f1` | Average F1 over categories, so rare ones count | — |
| `review_rate` | Share sent to a human. A workload number | — |
| `category_defensible` (`--judge`) | For disagreements, does Claude judge the answer a defensible alternative? Use it to find bad labels, not to excuse errors | — |

**Statements** (per full-year scenario)

| Metric | Meaning | Gate |
|---|---|---|
| `balance_sheet_balances` | Assets = Liabilities + Equity, to the cent | 1.0 |
| `cash_ties_to_bank` | Balance-sheet cash = the bank's Dec 31 balance | 1.0 |
| `all_transactions_posted` | Every transaction appears in the journal exactly once | 1.0 |
| `net_income_accuracy` | 1 − relative error vs. the reference | ≥ 0.98 |
| `pnl_line_accuracy` / `bs_line_accuracy` | Share of lines within $50 or 2% of the reference | — |
| `scenario_categorization_accuracy` | Category accuracy across the whole year | — |

The balance and tie-out checks pass even when categories are wrong. A miscategorized owner draw
still balances; it just understates profit. That is why the line-level comparisons against a
reference exist.

`run_evals` exits with code 1 when a gate fails, so it can block a CI job:

```yaml
# .github/workflows/openbooks-evals.yml (sketch)
- run: cd openbooks && python -m pytest -q
- run: cd openbooks && python -m evals.run_evals categorization
  env: { ANTHROPIC_API_KEY: "${{ secrets.ANTHROPIC_API_KEY }}", LANGSMITH_API_KEY: "${{ secrets.LANGSMITH_API_KEY }}" }
```

Change the gates in `THRESHOLDS` at the top of `evals/run_evals.py`.

### Before you trust the scores

The datasets here are **synthetic**: generated by `data/generate_sample_data.py` with known
answers. They show the workflow. They do not show how the system performs on your transactions.
Before relying on it:

1. Hand-label 200–500 of your own real transactions (or have your accountant do it), weighted toward
   hard cases: transfers, owner draws, loan payments, refunds, Amazon/Uber/Google, checks.
2. Replace the statement scenarios with a prior year your accountant has already closed. Their
   statements become the reference.
3. Every correction from the review queue is a new labeled example. Add them to the golden set.

## Layout

```
openbooks/
├── books/                  # the pipeline
│   ├── ingest.py           #   load + normalize + validate bank exports
│   ├── chart_of_accounts.py
│   ├── config.py           #   company config: period, opening balances, loan, depreciation
│   ├── rules.py            #   deterministic vendor rules
│   ├── prompts.py          #   LLM prompt (bump PROMPT_VERSION when you edit it)
│   ├── categorizer.py      #   rules → Claude (structured output) → review flags
│   ├── ledger.py           #   double-entry journal
│   ├── statements.py       #   P&L, Balance Sheet, checks, Excel export
│   └── pipeline.py         #   CLI + run_pipeline()
├── config/                 # chart_of_accounts.csv, vendor_rules.csv, company.json
├── data/                   # sample bank export, answer key, golden set, scenarios, generator
├── evals/                  # LangSmith datasets, targets, evaluators, LLM judge, runner
└── tests/                  # pytest: ingest, categorizer (fake LLM), statements, evaluators
```

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude for categorization (without it, runs fall back to `--offline`) |
| `OPENBOOKS_MODEL` | `claude-sonnet-5-5` | Categorization model |
| `OPENBOOKS_JUDGE_MODEL` | `claude-haiku-5-5` | LLM-as-judge model |
| `LANGSMITH_API_KEY` | — | Datasets and experiments |
| `LANGSMITH_TRACING` | — | `true` to trace every pipeline run |
| `LANGSMITH_PROJECT` | `default` | Project that traces go to |
