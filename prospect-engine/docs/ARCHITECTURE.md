# ARCHITECTURE.md — Prospect Engine

Status: **approved at CHECKPOINT 1** (2026-11-24). `docs/BUILD_SPEC.md` is the source of truth. This file explains how I understand that spec, the changes I propose, the risks I see, and the task list I will work from. Nothing in "Proposed deviations" happens until you approve it.

---

## 1. What we are building, in plain language

A command-line tool. You give it a description of your ideal customer (the ICP). It returns a CSV of US B2B companies that match, with 1 to 3 decision-makers at each one and their work emails where possible. Every row has a score, the reasons behind it, and links to the evidence. A human reviews the list. The tool never contacts anyone.

The core idea is **"buy the data, build the logic."** We don't scrape. We pay licensed data providers, mostly on free tiers, for raw facts. Our code decides which facts matter, scores them, cleans them, and records where each one came from.

## 2. Pipeline (the order that saves money)

```
ICP JSON
  │
  ▼
discover ── TheirStack: companies posting SDR/BDR jobs (hiring signal + tech signal)
  │          Prospeo search-company: US, 50–500 employees, SaaS subtype → firmographics
  │          (+ technology data when present = 2nd independent tech signal, D10)
  ▼
qualify ─── Tavily: text of up to 3 public pages + 1 funding-news search per company
  │          Claude Sonnet: "does this company fit the ICP?" (strict JSON, evidence URLs checked)
  ▼
score ───── fit (0–100) + intent (0–100) → total → tier A / B / C / excluded
  │          weights come from config/scoring.yaml
  ▼   (only tier ≥ B continues; this is where most of the cost is saved)
contacts ── Prospeo search-person: all qualifying domains in one search
  │          (company websites + title/seniority filters, US, max N per company)
  │          title classifier: rules first, then Claude Haiku for the leftovers
  ▼
enrich ──── email waterfall: Prospeo enrich-person (by person_id) → Hunter → "no_email"; then verify
  ▼
clean ───── normalize, validate, dedupe (fuzzy matches flagged, never auto-merged), suppress
  ▼
export ──── output/<run_id>_prospects.csv, _accounts.csv, _needs_review.csv, _changes.csv
             (optional Google Sheet)
```

The key point: **we only buy contacts and emails for accounts that already scored well.** Those calls are the expensive ones.

## 3. How the pieces fit

| Layer | Files | Job |
|---|---|---|
| CLI | `src/run.py` | Parses commands (`icp`, `estimate`, `pipeline`, `report`, `suppress`) and calls the stages. |
| Stages | `discover.py`, `qualify.py`, `score.py`, `contacts.py`, `enrich.py`, `clean.py`, `export.py` | Business logic only. Stages never make HTTP calls themselves; they ask a provider module. |
| Providers | `src/providers/*.py` | One small module per API. Each one builds requests and parses responses. All of them go through `base.py`. |
| Gatekeeper | `src/providers/base.py` | **The only place a network call happens.** For every call: (1) check the cache, (2) check the budget, (3) send with a timeout and retry on 429/5xx with exponential backoff, (4) log to `api_calls`, (5) save the response to the cache. |
| Storage | `src/db.py` → `data/prospects.db` | SQLite tables from spec section 4. Every write is an "upsert" (insert, or update if the row exists), so re-running never creates duplicates. |
| Models | `src/models.py` | Pydantic classes (ICP, Company, Contact, Assessment, Score). They validate data as it moves between stages. |
| Config | `config/*.yaml`, `config/*.json`, `config/suppression.csv` | Budgets, tier thresholds, weights, model names, ICP. No numbers hard-coded in Python. |

### The budget guard, step by step
1. Before a call, the provider module runs `estimate_cost(call)`, which gives a dollar estimate and a credit estimate.
2. `base.py` checks: run spend so far + estimate ≤ `--budget`, **and** month spend so far + estimate ≤ the monthly cap ($25 in `settings.yaml`), **and** (see deviation D2) month credits used + estimate ≤ that provider's free-tier credit cap.
3. If any check fails, the call is refused. The refusal is logged and the stage moves on with that record marked as "skipped: budget". The run doesn't crash.
4. After the call, the actual cost (for example, Claude's real token counts) replaces the estimate in `api_calls`.
5. Cache hits are logged with `cached = true` and cost $0. That's why a second identical run costs about $0.

### Provenance
Every value any provider returns is also written to `field_values` with `source`, `retrieved_at` and `confidence`. The `companies` and `contacts` tables hold the "current best" value, and `field_values` keeps the full history. The `sources` and `retrieved_at` columns in the export come from there.

## 4. Proposed deviations (need your OK)

| # | Proposal | Why |
|---|---|---|
| D1 | **Website text only through Tavily (Firecrawl as backup). No plain-HTTP fetching at all.** | The spec allows plain HTTP if it obeys robots.txt and waits 3 seconds per domain. Dropping that path removes a whole category of compliance risk and code. We can add it later if we need it. |
| D2 | **Track free-tier credits as well as dollars.** `settings.yaml` gets a `plan: free \| paid` and a `monthly_credit_cap` for each provider. On a free plan the dollar cost is $0, but the guard stops before you run out of credits. | With only a dollar budget, free-tier calls look "free" and could silently burn your monthly allowance, or start costing money if a plan auto-upgrades. Dollar estimates at paid rates are still shown so you can see what it *would* cost. |
| D3 | **Get the tech signal (HubSpot) from the TheirStack job-search call itself, if the API supports filtering or returning technologies on jobs.** Use the separate technographics lookup only as a fallback. **Approved with adjustment:** a tech match found in job posts is stored as a `signals` row with **confidence = medium**. No match is recorded as **"unknown"**, never as "no", and doesn't count against fit. *Extended by D10:* a Prospeo technology match is a second, independent tech signal (also medium confidence). | Per the spec's research, TheirStack's free tier has only 50 company credits/month at about 3 credits per lookup. That's roughly 16 lookups, too few for 50 candidates. I'll confirm this against the live docs in step 4. |
| D4 | ~~**PDL Person Search with `size: 3` instead of 5 by default (configurable).**~~ *Superseded by D10: PDL is no longer used.* | PDL free tier = 100 records per product per month. 25 accounts × 5 = 125, which is over. 25 × 3 = 75 fits, with room for the 5-account test run. |
| D5 | **`--mock` uses a separate database, `data/mock.db`.** | Keeps fake fixture data from ever mixing with real prospects. |
| D6 | **Cache entries expire, and the expiry is set per provider in `config/settings.yaml`.** **Approved defaults:** 7 days for TheirStack job/signal data and Tavily searches. 90 days for PDL company and person data, Prospeo, Hunter, and page extractions (Tavily extract / Firecrawl). Claude responses (keyed on the exact prompt + model) default to 90 days. Anything not listed falls back to a `default` of 30 days. | Hiring and funding signals go stale fast. Firmographics and emails change slowly. Re-runs inside the window still cost about $0. |
| D7 | **Use `httpx` for our own HTTP calls instead of `requests`.** *Correction after install:* `anthropic` 2.13.0 depends on `httpx2`, not `httpx`, so `httpx` 0.28.1 is a direct dependency we chose. You confirmed keeping it on 2026-11-24. | Stable, well-documented API with timeouts built in. `requests` is still installed (pulled in by `tldextract`), but our code won't use it. |
| D9 | **The real suppression list lives in `data/suppression.csv` (gitignored) and the `suppression` table. `config/suppression.csv` stays a header-only template.** `suppress add` writes only to the private copies. Approved 2026-11-24. | The spec puts the list in `config/`, but the repo is public, and suppressed emails belong to real people (spec rule 8). |
| D10 | **People Data Labs is replaced by Prospeo everywhere** (you asked for this on 2026-11-24; PDL needs a business email to sign up). (a) Firmographics: `POST /search-company` with `company_location_search` = United States, `company_headcount_custom` {50, 500}, `company_type.subtypes` = SaaS. Industry is optional; technology = HubSpot only if `use_technology_filter` is on, because that filter needs the paid Starter plan. (b) Contacts: one `POST /search-person` covering all qualifying domains (`company.websites`), with `person_job_title` (CONTAINS), `person_seniority`, US location and `max_person_per_company`. (c) Emails: `POST /enrich-person` with `person_id`, then Hunter as fallback. (d) A Prospeo technology match is a second, independent tech signal alongside TheirStack (medium confidence; no match = unknown). `PDL_API_KEY` is removed. Details and sources are in `docs/PROVIDERS.md`. **Decided 2026-11-24: free plan, technology filter off.** HubSpot stays a scoring signal from TheirStack and Prospeo, never a hard filter. (Numbered D10 because D9 was already taken.) | No PDL account possible. Prospeo search bills 1 credit per page of 25 results, so it's much cheaper than PDL's per-record billing. |
| D8 | **Libraries not in the spec's "allowed extras" list:** `pydantic`, `python-dotenv` and `pytest`. The spec itself requires them (sections 2, 5 and 9), and `pydantic` and `httpx` come with `anthropic` anyway. | Spec section 3 says to ask before adding libraries that aren't on the list, so I'm asking now rather than at install time. |

## 5. Risks and open questions

1. **Python 3.15 wheels.** `pydantic-core` (needed by `pydantic` and `anthropic`) is compiled Rust code. If it has no wheel for 3.15 yet, install will fail. Per the spec, I'll **stop and report** and won't downgrade Python. `rapidfuzz` is in the same situation.
2. **Free-tier capacity for the 25-account run** (estimates from the spec's research; to be verified in step 4):
   - TheirStack: 200 API credits at 1 per job → keep the job search tight, about 100–150 jobs max.
   - Prospeo (D10): company search is about 2–4 credits (25 companies per page), contact search about 3–6 credits (all domains in one search), plus 1 credit per email found (about 75). The free-plan credit amount couldn't be read from the pricing page, so the tool reads the real balance from the free `GET /account-information` endpoint before live runs.
   - Hunter: 50 credits as fallback for emails Prospeo doesn't find. Verification is 0.5 per email.
   - Tavily: about 50 companies × (1 extract + 1 search) ≈ 100–150 out of 1,000. Fine.
   - Claude: about 50 Sonnet qualification calls at roughly 7k tokens in each is about $0.75–1.00, plus a few cents of Haiku. This is the main real-dollar cost.
3. **Funding signal is weak** (as the spec says). It will be labelled low-confidence and weighted lightly in `scoring.yaml`.
4. **Model IDs and prices** (`claude-sonnet-5-5`, `claude-haiku-5-5`) and the structured-outputs syntax get checked against current Anthropic docs before `providers/claude.py` is written.
5. **Public repo.** Fixtures use only `*.example` domains and obviously fake names. `data/` and `output/` are already in `.gitignore`; `data/logs/` sits under `data/`, so it's covered too.

## 6. Ground rules I will follow (from spec section 2 and your CLAUDE.md)

- I never create, read, print or edit `.env`. I'll maintain `.env.example` with empty values only.
- I never run `git commit` or `git push`.
- Licensed APIs only. No LinkedIn scraping or Sales Navigator automation of any kind.
- Every paid call goes through `providers/base.py` (cache + budget + log).
- Before writing each provider module, I fetch its official docs and record URL + date in `docs/PROVIDERS.md`. No guessed endpoints.
- I explain each change before making it, work in small steps, and **ask before running commands** (pip installs, pytest, the pipeline) or changing many files at once.
- Live paid runs print an estimate and wait for you to type `yes`.

## 7. Task list

**Step 1 — Plan**
1. Read spec + existing files. ✅
2. Write this `docs/ARCHITECTURE.md`. ✅
3. **CHECKPOINT 1:** you approve or edit the deviations D1–D8. ✅ Approved 2026-11-24, with adjustments to D3 and D6.

**Step 2 — Foundation** (I'll ask before installing anything)
4. Create `requirements.txt` (unpinned starting list), `.env.example` and the `README.md` stub. Install packages and report any Python 3.15 wheel failures.
5. Config files: `config/settings.yaml`, `scoring.yaml`, `icp_default.json`, `suppression.csv` (header only).
6. `src/models.py`: Pydantic models.
7. `src/db.py`: schema, upsert helpers, suppression helpers.
8. `src/providers/base.py`: cache, budget guard (dollars + credits), retries with backoff, timeouts, `api_calls` logging, mock mode switch.
9. `src/clean.py` and `src/score.py`: pure logic, no network.
10. `src/run.py`: CLI skeleton with all five commands, plus logging setup (console INFO, file DEBUG in `data/logs/`).
11. Unit tests for db, base (cache hit, budget refusal, retry), clean, score.

**Step 3 — Mock pipeline**
12. Provider modules (prospeo, theirstack, hunter, tavily, firecrawl, claude), each written only after reading its official docs, each reading its key from the environment with a clear "missing variable X" error and an `estimate_cost()`. Fixtures in `tests/fixtures/` with fake `*.example` data.
13. Stage modules: `icp.py`, `discover.py`, `qualify.py`, `contacts.py`, `enrich.py`, `export.py`.
14. `python -m src.run pipeline --mock` runs end to end and writes all four CSVs.
15. `python -m pytest` all green, with no network.
16. **CHECKPOINT 2:** show the mock CSV head + test summary.

**Step 4 — Docs verification**
17. For each provider (Anthropic, Prospeo, TheirStack, Hunter, Tavily, Firecrawl): fetch the official docs, then fix endpoints, params, auth, response parsing and costs in the module and its fixtures.
18. Write `docs/PROVIDERS.md` (endpoints, auth, limits, cost per call, doc links, date verified).
19. Implement `estimate` (calls and cost per provider for N accounts).
20. **CHECKPOINT 3:** list the required keys. You create the accounts and fill `.env` yourself.

**Step 5 — First live run**
21. `pipeline --accounts 5 --budget 2`: show the estimate, wait for `yes`, run, report results, spend and errors.
22. **CHECKPOINT 4.**

**Step 6 — Fix and tune**
23. Adjust filters, find-rate handling and title-classifier rules based on the 5-account results. Confirm a re-run costs about $0 and adds no duplicates.

**Step 7 — Target run**
24. `pipeline --accounts 25 --budget 5` (same estimate → `yes` flow).
25. **CHECKPOINT 5:** summary, cost per contact, top 10 rows, known issues.

**Step 8 — Polish**
26. README (what it does, setup, usage, Mermaid diagram, cost and accuracy results, limitations, compliance notes).
27. `docs/DECISIONS.md` (the deviations above + anything decided during tuning).
28. Optional `--sheet` Google Sheets export.
29. Final check against the spec's "Definition of done", including `git status` showing no `.env`, `data/`, `output/` or `.venv/`.
