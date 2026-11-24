# BUILD_SPEC.md — Prospect Engine prototype

> Put this file at `prospect-engine/docs/BUILD_SPEC.md`. Claude Code: read this whole file before doing anything. It is the source of truth for what to build. Research behind it was done on 2026-11-24; prices and endpoints may have changed, so verify against live docs as instructed below.

## 1. Goal

Build a working Python command-line prototype that turns an Ideal Customer Profile (ICP) into a **real, scored, de-duplicated list of US B2B prospects** (companies + 1 to 3 decision-makers each, with verified work emails where possible), written to a CSV (and optionally a Google Sheet) for a human to review.

Default ICP for the first runs (store in `config/icp_default.json`):

> US B2B SaaS companies with 50 to 500 employees that are hiring Sales Development Representatives, use HubSpot, and raised funding in roughly the last 180 days. Contacts: Head of Sales, VP Sales, VP Revenue, CRO.

Target for the first successful live run: **25 accounts**. Design for up to 500.

## 2. Non-negotiable rules

Stop and ask the user if any task would break one of these.

### Compliance and data sources
1. **US companies only.** Filter on country = US at every stage.
2. **Licensed APIs only** for people and contact data (the providers in section 6). **Never scrape LinkedIn** or automate Sales Navigator in any way. LinkedIn's terms forbid it.
3. Company websites may be read **only** through Tavily or Firecrawl, or with plain HTTP requests that: read `robots.txt` first and obey it, send an honest User-Agent (`ProspectEngine/0.1 (learning project)`), wait at least 3 seconds between requests to the same domain, read public pages only (no logins), and never bypass CAPTCHAs, rate limits or other anti-bot controls.
4. **The tool never sends emails, makes calls, or writes to any CRM.** Its only outputs are local files and (optionally) a Google Sheet. Every output row has an empty `approved` column for a human.
5. Maintain a suppression list (`suppression` table + `config/suppression.csv`). Any email or domain on it must never appear in output.

### Security
6. API keys live only in `.env` (already in `.gitignore`). **Never create, read, print, or edit `.env`.** Read keys with `python-dotenv` + `os.environ`. Keep `.env.example` updated with key names and empty values.
7. Never log full API keys or put them in URLs where a library would log them; prefer headers.
8. The repo is **public**. Real data goes only in `data/` and `output/` (both must be in `.gitignore`; add `output/` if missing). Never put real names or emails in tests, fixtures, or docs. Fixtures use obviously fake data (`*.example` domains).
9. **Do not run `git commit` or `git push`.** The user commits by hand.

### Cost
10. Every paid API call goes through one helper that: checks a cache first (SQLite table keyed on provider + endpoint + normalized params), logs the call to `api_calls` with estimated cost, and refuses to run if the run's spend would exceed `--budget` (default **$5.00**) or the month's logged spend would exceed **$25.00** (configurable in `config/settings.yaml`).
11. Before the first live paid run, print an estimated cost and the number of calls per provider, then **stop and wait for the user to confirm**.
12. Prefer free tiers. Use People Data Labs sandbox endpoints for development where available.

## 3. Environment (already set up)

- Windows, PowerShell, VS Code. Working directory: `C:\Users\tycai\dev\gtm-lab\prospect-engine` (inside the public `gtm-lab` repo).
- Python 3.15 virtual environment at `.venv` (activate with `.\.venv\Scripts\Activate.ps1`). Always install with `python -m pip`.
- May already exist: `.gitignore`, `.env.example`, `README.md`, `requirements.txt`, `src/__init__.py`, `tests/test_setup.py`. Check first; extend rather than overwrite.
- Allowed extra libraries: `anthropic`, `pyyaml`, `rapidfuzz`, `tenacity` (retries), `rich` (console output), `gspread` + `google-auth` (only for optional Sheets export), `phonenumbers`, `tldextract`. Ask before adding others. Regenerate `requirements.txt` with `pip freeze | Out-File -Encoding utf8 requirements.txt`.
- If a package fails to install on Python 3.15 (no wheel), stop and report the error rather than downgrading Python.

## 4. Architecture

"Buy the data, build the logic." Licensed APIs supply records; this code does routing, qualification, scoring, cleaning and provenance.

```
ICP JSON ─► discover ─► contacts ─► enrich ─► qualify ─► score ─► clean ─► export (CSV / Sheet)
              │            │           │          │
              └────────────┴───────────┴──────────┴──► providers/* (cached, budgeted, logged)
                                                    └──► SQLite (data/prospects.db) with provenance
```

Order matters for cost: **qualify and score accounts before buying contacts** for them. Concretely: discover → qualify (website + LLM) → score fit/intent → only for accounts at or above `min_tier_for_contacts` (default tier B) run contacts → enrich → clean → export.

### Repository layout

```
prospect-engine/
  README.md
  .env.example
  requirements.txt
  config/
    icp_default.json
    scoring.yaml
    settings.yaml          budgets, tiers, contacts_per_account, model names
    suppression.csv
  src/
    __init__.py
    run.py                 CLI entry point (argparse)
    icp.py                 natural language -> validated ICP (Pydantic + Claude structured output)
    discover.py
    qualify.py
    score.py
    contacts.py
    enrich.py
    clean.py               normalize, validate, dedupe, suppress
    export.py              CSV, optional Google Sheet
    db.py                  SQLite schema + helpers
    models.py              Pydantic models: ICP, Company, Contact, Assessment, Score
    providers/
      base.py              shared HTTP client: retries w/ backoff on 429/5xx, timeout, cache, cost log, budget
      pdl.py
      theirstack.py
      prospeo.py
      hunter.py
      tavily.py
      firecrawl.py         optional alternative to tavily for page text
      claude.py            thin wrapper for structured-output calls
  tests/
    fixtures/              fake API responses (*.example domains only)
  data/                    gitignored: prospects.db, cache
  output/                  gitignored: CSV exports
  docs/
    BUILD_SPEC.md          this file
    ARCHITECTURE.md
    PROVIDERS.md           per provider: endpoints used, auth, limits, cost per call, doc links, date verified
    DECISIONS.md
```

### Data model (SQLite)

| Table | Key columns |
|---|---|
| `runs` | run_id, started_at, finished_at, icp_id, budget_usd, spent_usd, status, notes |
| `companies` | domain (PK, normalized: lowercase, no scheme, no `www.`), name, employee_count, country, industry, linkedin_url (only if returned by a licensed API) |
| `contacts` | contact_id, domain, full_name, first_name, last_name, title, persona, seniority, email, email_status, phone, phone_type |
| `field_values` | entity_type, entity_id, field, value, source, retrieved_at, confidence — **every value received from every provider**, for provenance |
| `signals` | domain, signal_type (hiring / tech / funding), detail, observed_at, source_url, confidence |
| `assessments` | domain, icp_fit (strong/medium/weak/no), business_model, reasons (JSON), disqualifiers (JSON), evidence_urls (JSON), confidence, model, created_at |
| `scores` | domain, fit_score, intent_score, total, tier, parts (JSON) |
| `api_calls` | id, run_id, provider, endpoint, params_hash, status_code, est_cost_usd, credits, cached (bool), at |
| `cache` | provider, endpoint, params_hash, response_json, fetched_at |
| `suppression` | value (email or domain), reason, added_at |

Writes must be idempotent: upsert on natural keys (domain, email) so re-runs never create duplicates.

## 5. Pipeline behavior

### 5.1 ICP (`src/icp.py`)
- `python -m src.run icp "plain English description" --out config/icp_xxx.json` calls Claude (model `claude-sonnet-5-5`) with **structured outputs** to produce the ICP JSON matching the Pydantic `ICP` model: `hard_filters` (country, employee_min, employee_max, business_model), `soft_signals` (hiring_titles, technologies, funding_within_days), `personas` (name, titles, seniorities), `exclusions` (domains, industries), `target_accounts`.
- Claude structured outputs: `client.messages.create(..., output_config={"format": {"type": "json_schema", "schema": {...}}})`, or `client.messages.parse(..., output_format=PydanticModel)` in the Python SDK. Schemas need `additionalProperties: false`. Verify current syntax at https://platform.claude.com/docs/en/build-with-claude/structured-outputs before coding.
- Print the ICP and ask the user to confirm or edit before saving.

### 5.2 Discover (`src/discover.py`)
1. **Signal set first:** TheirStack job search for US job postings in the last 30 days whose titles match `hiring_titles` (e.g. "Sales Development Representative", "SDR", "BDR"). Collect hiring company domains and job URLs → `signals` (type hiring, source_url = job URL).
2. **Tech filter:** use TheirStack technographics (company technology detected from job posts) to check for each technology in `soft_signals.technologies` (e.g. HubSpot). Record as `signals` (type tech) with confidence. Missing tech detection = unknown, not "no".
3. **Firmographics:** PDL Company Enrichment by domain (or Company Search with hard filters) to get employee_count, country, industry. Apply hard filters. Store every field in `field_values`.
4. Stop when `target_accounts × 2` candidates pass hard filters (headroom for qualification losses) or budget is hit.

### 5.3 Qualify (`src/qualify.py`)
1. Get text for up to 3 public pages per company (home, pricing or product, careers) via Tavily extract or Firecrawl scrape (Markdown). Truncate to ~6,000 tokens total.
2. **Funding signal:** one Tavily search per company like `"<company name>" raised funding round` restricted to the last 180 days if the API supports a time filter; let Claude decide from returned snippets whether a funding event within the window is evidenced. Store as `signals` (type funding) with source_url and confidence. Funding is the least reliable signal; label it as such.
3. Send the page text + signals to Claude (`claude-sonnet-5-5`) with a strict schema returning: `business_model` (enum), `sells_to`, `icp_fit` (enum strong/medium/weak/no), `fit_reasons` (list), `disqualifiers` (list), `evidence_urls` (list, must be URLs that were actually supplied), `funding_evidence` (nullable object: date, round, url), `confidence` (0 to 1).
4. The model may only use supplied text. Validate that every evidence URL was in the input; drop any that were not and lower confidence.
5. Use the Batch API only if a run exceeds 100 companies (optional).

### 5.4 Score (`src/score.py`)
- Weights in `config/scoring.yaml`, never hard-coded. Fit score 0 to 100 from: employee range, business model match, LLM `icp_fit`, tech match. Intent score 0 to 100 from: hiring signal (count of matching jobs), funding evidence, each decayed with half-life 30 days by age.
- `total = 0.6 × fit + 0.4 × intent` (weights configurable). Tiers: A ≥ 75, B ≥ 55, C ≥ 40, else exclude (configurable). Store score parts in JSON so every score is explainable.

### 5.5 Contacts (`src/contacts.py`) — only for tier ≥ `min_tier_for_contacts`
1. PDL Person Search by company domain + title keywords from personas, `location_country = united states`, limited to ~5 results per company.
2. Classify each title: rules first (exact/regex matches for common titles), then Claude Haiku (`claude-haiku-5-5`) with a strict schema (`persona`, `seniority`, `department`, `is_match` bool, `confidence`) for the rest. Batch several titles per call to save tokens.
3. Keep up to `contacts_per_account` (default 3) best matches, preferring economic buyer (CRO / VP Sales / VP Revenue) then Head of Sales.

### 5.6 Enrich (`src/enrich.py`)
- Email waterfall: **Prospeo** email finder → if not found, **Hunter** Email Finder → if not found, mark `no_email`.
- Verify found emails (Hunter Email Verifier, or Prospeo's built-in verification status if returned). Store `email_status` ∈ {valid, catch_all, unknown, invalid}. Drop `invalid` from export; keep others with status visible.
- Phone numbers: optional, off by default (costly; also requires calling-compliance decisions). If ever enabled, store `phone_type` (mobile/landline/unknown) and format E.164 with `phonenumbers`.

### 5.7 Clean (`src/clean.py`)
- Normalize: trim, lowercase emails/domains, title-case names, E.164 phones.
- Validate: email syntax, required fields (company, domain, contact name, title).
- Dedupe: exact on email; exact on domain for companies; fuzzy company name + domain root with `rapidfuzz` (score ≥ 90) → flag for review, **do not auto-merge**.
- Suppress: remove anything matching the suppression list.
- Write `output/<run_id>_needs_review.csv` for flagged rows and `output/<run_id>_changes.csv` describing every change (row, field, old, new, rule).

### 5.8 Export (`src/export.py`)
- Main file `output/<run_id>_prospects.csv`, one row per contact, sorted by tier then score, columns:
  `tier, score, fit_score, intent_score, company, domain, employee_count, contact, title, persona, email, email_status, top_signal, hiring_signal_url, funding_evidence_url, fit_reasons, evidence_url, confidence, sources, retrieved_at, approved`
- Also `output/<run_id>_accounts.csv` (one row per account, including tier C and excluded with reason) and a run summary printed to console: accounts found, qualified, contacts, emails by status, total spend, cost per contact.
- Optional `--sheet <spreadsheet_id>`: write the same rows to a Google Sheet tab via `gspread` with a service account JSON whose path comes from `.env` (`GOOGLE_SERVICE_ACCOUNT_FILE`). Write in batches, not cell by cell.

## 6. Providers

**Before writing each provider module, fetch and read its official API docs with your web tools. Never guess endpoint paths, parameter names, auth headers or response fields.** Record what you verified (URL + date) in `docs/PROVIDERS.md`. If docs are unclear, stop and ask.

| Provider | Use | Docs to read | Notes from research (2026-11-24, verify) |
|---|---|---|---|
| People Data Labs | Company enrich/search, Person search | https://docs.peopledatalabs.com | Endpoints seen: `POST /v5/company/search`, `GET /v5/company/enrich`, `POST /v5/person/search` on `https://api.peopledatalabs.com`. Free: 100 records per product per month. Pro: person $0.28, company $0.10 per match. Search bills 1 credit per record returned, so set `size` small. Free plan may hide some contact fields; that's fine (emails come from Prospeo/Hunter). Sandbox endpoints exist for development. |
| TheirStack | Hiring signals, technographics | https://theirstack.com/en/docs (index: https://theirstack.com/docs/llms.txt) | Free: 200 API credits + 50 company credits per month. Reported: 1 credit per job returned, 3 per company/technographics lookup. Set result limits low. |
| Prospeo | Email finder (first in waterfall) | Prospeo API docs (find from https://prospeo.io) | Reported: free 75 credits/month; email finder 1 credit, charged only when found; mobile 10 credits (do not use). |
| Hunter | Email finder fallback + verifier | https://hunter.io/api-documentation | Base `https://api.hunter.io/v2`. Free 50 credits/month. Finder 1 credit per email found; verifier 0.5; no charge when nothing found. Prefer sending the key in a header if the API supports it. |
| Tavily | Page extraction + funding news search | https://docs.tavily.com | Free 1,000 credits/month; basic search 1 credit, advanced 2; `POST https://api.tavily.com/search` with Bearer auth. |
| Firecrawl (optional) | Page to Markdown | https://docs.firecrawl.dev | Free 1,000 credits/month; 1 credit per page; JSON extraction +4 (don't use; let Claude extract). |
| Anthropic Claude | ICP translation, qualification, title classification | https://platform.claude.com/docs | Models: `claude-sonnet-5-5` ($2 in / $10 out per million tokens), `claude-haiku-5-5` ($0.10 / $0.50). Batch API 50% off. Structured outputs on all models. |

`.env.example` must list: `ANTHROPIC_API_KEY`, `PDL_API_KEY`, `THEIRSTACK_API_KEY`, `PROSPEO_API_KEY`, `HUNTER_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY` (optional), `GOOGLE_SERVICE_ACCOUNT_FILE` (optional).

Each provider module must: read its key from the environment and fail with a clear message naming the missing variable; implement a `estimate_cost(call)` function; go through `providers/base.py`; and have tests using fixtures in `tests/fixtures/` (no live calls in tests).

## 7. CLI

```powershell
python -m src.run icp "description..." --out config/icp_new.json
python -m src.run estimate --icp config/icp_default.json --accounts 25
python -m src.run pipeline --icp config/icp_default.json --accounts 25 --budget 5 [--dry-run] [--mock] [--sheet SHEET_ID]
python -m src.run report --run RUN_ID      # summary + cost breakdown from api_calls
python -m src.run suppress add someone@example.com --reason "opted out"
```

- `--mock`: every provider returns fixtures; zero network, zero cost. Must run end to end.
- `--dry-run`: real discovery limited to the first stage, prints what would be called and the estimated cost, buys nothing beyond that stage.
- Live runs print a cost estimate and require typing `yes` before spending.

## 8. Build order and checkpoints

Work in this order. At each **CHECKPOINT**, stop, summarise what you built and what you verified, and wait for the user.

1. **Plan.** Read this spec and the existing files. Write `docs/ARCHITECTURE.md` (your understanding + any deviations you propose) and a numbered task list. **CHECKPOINT 1.**
2. **Foundation.** `models.py`, `db.py`, `providers/base.py` (cache, retries with exponential backoff on 429/5xx, timeouts, budget guard, cost log), config files, CLI skeleton, `clean.py`, `score.py`, with unit tests.
3. **Mock pipeline.** All provider modules with fixture-based tests; full pipeline running with `--mock` producing the CSVs. `python -m pytest` all green. **CHECKPOINT 2** (show the mock CSV head and test summary).
4. **Docs verification.** For each provider, read live docs, update modules and `docs/PROVIDERS.md`. Implement `estimate`. **CHECKPOINT 3:** list required keys; the user creates accounts and fills `.env` themselves.
5. **First live run:** `--accounts 5 --budget 2`. Show the estimate, wait for `yes`, run, then report results, spend, and any errors. **CHECKPOINT 4.**
6. **Fix and tune** based on the 5-account run (filters too strict/loose, find rates, title classifier misses).
7. **Target run:** `--accounts 25 --budget 5`. **CHECKPOINT 5:** summary, cost per contact, sample of top 10 rows, known issues.
8. **Polish:** README (what it does, setup, how to run, architecture diagram in Mermaid, cost and accuracy results, limitations, compliance notes), `docs/DECISIONS.md`, optional Sheets export.

## 9. Quality bar

- Type hints and docstrings on public functions; small modules; no secrets in code.
- Every network call has a timeout; every failure is logged to `api_calls` and the run continues with that record marked, rather than crashing.
- Re-running the same pipeline twice spends ~$0 the second time (cache) and creates no duplicate rows.
- Logging via the `logging` module (INFO to console, DEBUG to `data/logs/<run_id>.log`, gitignored).
- Tests: `python -m pytest` passes with no network access.

## 10. Definition of done

- [ ] `python -m src.run pipeline --mock` produces CSVs offline; all tests pass.
- [ ] A live 25-account run under $5 produces `output/<run_id>_prospects.csv` with tiers, reasons, evidence URLs, email statuses and an empty `approved` column.
- [ ] Every value in the database has source, timestamp and confidence.
- [ ] Second identical run costs ~$0 and adds no duplicates.
- [ ] `git status` shows no `.env`, `data/`, `output/` or `.venv/` files.
- [ ] README explains setup, usage, architecture, results, costs and limitations.
