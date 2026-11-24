# Prospect Engine

A learning-project CLI that turns an Ideal Customer Profile into a scored, de-duplicated list of US B2B prospects (companies + decision-makers + verified work emails) for a human to review.

**Status:** under construction. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and task list, and [`docs/PROVIDERS.md`](docs/PROVIDERS.md) for the verified API details. A full README (results, costs, limitations) comes at the end of the build.

## Ground rules

- Licensed data APIs only (Prospeo, TheirStack, Hunter, Tavily, Claude). No LinkedIn scraping.
- The tool never sends email, calls anyone, or writes to a CRM. Output is local CSV files with an empty `approved` column.
- Every paid call is cached, budgeted (default $5 per run, $25 per month) and logged.

## Setup (Windows / PowerShell)

```powershell
cd prospect-engine
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in your own keys in .env (never commit it)
python -m pytest              # offline tests, no API calls
```

## Commands

```powershell
python -m src.run report --run RUN_ID                       # cost breakdown of a run
python -m src.run suppress add someone@example.com --reason "opted out"
python -m src.run pipeline --mock                            # offline end-to-end run (fake data, $0)
python -m src.run estimate --accounts 25 --budget 5          # calls, credits and cost; spends nothing
python -m src.run estimate --accounts 5 --balances           # + live free-credit balances (needs keys)
python -m src.run pipeline --dry-run --accounts 5            # discovery only, after you type "yes"
python -m src.run pipeline --accounts 25 --budget 5          # live run: shows estimate, asks "yes" first
```
