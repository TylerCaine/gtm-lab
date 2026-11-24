# PROVIDERS.md

For each provider: what we call, how we authenticate, limits, cost, and **where each fact was verified and when**. If something is listed as "not stated", the docs didn't say it, and the code must not assume it.

| Provider | Status |
|---|---|
| Prospeo | ✅ Verified 2026-11-24 (below) |
| TheirStack | ✅ Verified 2026-11-24 (below) |
| Hunter | ✅ Verified 2026-11-24 (below) |
| Tavily | ✅ Verified 2026-11-24 (below) |
| Firecrawl (optional) | ⏸ Not used (D1: Tavily only). Verify before ever enabling |
| Anthropic Claude | ✅ Verified 2026-11-24 (below) |
| ~~People Data Labs~~ | Removed (D10) |

---

## Prospeo

Replaces People Data Labs (D10). Used for firmographics (search-company), contacts (search-person), emails (enrich-person), canonical filter values (search-suggestions) and balance (account-information).

### Sources read (2026-11-24)

| Page | Last updated (per page) |
|---|---|
| https://prospeo.io/api-docs (index) | 2026-06-11 |
| https://prospeo.io/api-docs/authentication | — |
| https://prospeo.io/api-docs/rate-limits | 2026-05-04 |
| https://prospeo.io/api-docs/account-information | — |
| https://prospeo.io/api-docs/search-company | 2026-07-29 |
| https://prospeo.io/api-docs/search-person | 2026-07-29 |
| https://prospeo.io/api-docs/enrich-person | — |
| https://prospeo.io/api-docs/filters-documentation | — |
| https://prospeo.io/api-docs/search-suggestions | 2026-07-30 |
| https://prospeo.io/api-docs/person-object | 2026-06-11 |
| https://prospeo.io/api-docs/company-object | 2026-06-11 |
| https://prospeo.io/api-docs/enum/seniorities | 2026-02-04 |
| https://prospeo.io/api-docs/enum/departments | 2026-06-11 |
| https://prospeo.io/api-docs/enum/employee-ranges | 2026-02-04 |
| https://prospeo.io/pricing | ❌ Could not read: rendered by JavaScript, only the title was returned |

### Auth and general

- Base URL: `https://api.prospeo.io` (HTTPS).
- Auth: header `X-KEY: <key>`, plus `Content-Type: application/json`. Putting the key in the URL isn't documented, so we never do it.
- Every response has an `error` boolean. Errors look like `{"error": true, "error_code": "NO_MATCH"}`.
- **HTTP 400 is used for all API errors, including "no match"/"no results".** 429 means rate limited. The code must read `error_code` to tell "nothing found" (cache it, $0) apart from real errors.

### Endpoints we use

| Endpoint | Purpose | Cost (docs) |
|---|---|---|
| `POST /search-company` | Firmographic filtering | 1 credit per request that returns at least 1 company (up to 25 per page). Zero results: implied free. Same page repeated within 30 days is free (`"free": true`). |
| `POST /search-person` | Contacts at qualifying companies | 1 credit per request that returns at least 1 person (up to 25 per page). Same 30-day repeat rule. **No emails or phones in results.** |
| `POST /enrich-person` | Email for a `person_id` from search | 1 credit per email found. Nothing charged on no match. Mobile costs 10 credits (we never request it). Re-enriching the same record within 90 days is free (`free_enrichment: true`). |
| `POST /search-suggestions` | Canonical filter values (locations, titles, industries, technologies) | **Free**, no credits. 15 requests/second on all plans. |
| `GET /account-information` | Remaining credits, plan, renewal date | **Free.** |

Not used: `/bulk-enrich-person`, `/enrich-company`, `/bulk-enrich-company`.

### Rate limits (rate-limits page)

| | Starter | Growth | Pro |
|---|---|---|---|
| Search: per second / minute / day | 1 / 30 / 1,000 | 2 / 60 / 4,000 | 5 / 180 / 250,000 |
| Enrich: per second / minute / day | 5 / 300 / 2,000 | 5 / 300 / 5,000 | 30 / 1,800 / 500,000 |

Free-plan limits: not stated. Settings throttle searches to one every 1.1 seconds and enrich calls to one every 0.25 seconds. The response headers `x-minute-request-left` and `x-daily-request-left` exist.

### Search request shape (search-company and search-person)

```json
{ "page": 1, "filters": { ... } }
```
- `page` is optional (default 1). Results are fixed at 25 per page, up to 1,000 pages / 25,000 results. To get the next page, send the same body with `page + 1`.
- Response: `error`, `free`, `results[]`, `pagination {current_page, per_page, total_page, total_count}`.
  - search-company: each result is `{ "company": {...} }`.
  - search-person: each result is `{ "person": {...}, "company": {...} }` (company is present when the person has a current job).
- A search needs at least one positive (include) filter. Exclude-only searches are rejected. Max 20,000 filter values in total.

### Filters we use (filters-documentation)

| Purpose | Filter | Value we send | Plan |
|---|---|---|---|
| US only | `company_location_search` | `{"include": ["United States"]}`. Must be the exact value from `location_search` suggestions (the docs' own example uses `"United States"`, type COUNTRY) | not stated |
| 50–500 employees | `company_headcount_custom` | `{"min": 50, "max": 500}`. Can't be combined with `company_headcount_range`. Whether the bounds are inclusive isn't stated, so we re-check `employee_count` ourselves | not stated |
| (fallback) | `company_headcount_range` | `["51-100", "101-200", "201-500"]`. Allowed: `1-10, 11-20, 21-50, 51-100, 101-200, 201-500, 501-1000, 1001-2000, 2001-5000, 5001-10000, 10000+` | not stated |
| SaaS | `company_type` | `{"subtypes": {"include": ["SaaS"]}}`. "SaaS" is a **subtype**, not an industry | Free |
| Industry (optional) | `company_industry` | `{"include": [...]}`, values from the industry enum (256 values, e.g. "Software Development") | not stated |
| HubSpot (optional) | `company_technology` | `{"include": ["HubSpot"]}`, max 20 values | **Starter (paid)**. Returns `PLAN_REQUIRED` otherwise |
| Company domains | `company.websites` | `{"include": ["acme.example", ...]}`. Root domains only, max 500 names + websites combined | not stated |
| Titles | `person_job_title` | `{"include": [...], "match_mode": "CONTAINS"}`. Modes: CONTAINS, EXACT, SIMILAR. Include max 100 | not stated |
| Seniority | `person_seniority` | `{"include": [...]}`. Include and exclude can't be combined. Max 11 values | not stated |
| Person in US | `person_location_search` | `{"include": ["United States"]}` | not stated |
| Cap per company | `max_person_per_company` | integer 1–100 | not stated |

**Seniority values** (enum page): `Founder/Owner`, `C-Suite`, `Partner`, `Vice President`, `Head`, `Director`, `Manager`, `Senior`, `Entry`, `Intern`. Our ICP seniorities map like this: c_suite → `C-Suite`, vp → `Vice President`, head → `Head`, director → `Director`, manager → `Manager`.

**Department values** (for reference): the top-level `Sales` department includes sub-departments such as Sales Development, Revenue Operations, Account Executive, Account Management. "Sales Leader" sits under `C-Suite`. Not used as a filter for now, because titles plus seniority are more precise.

Filters above your plan return HTTP 400 `PLAN_REQUIRED` (`filter_error` names the minimum plan). Other plan-gated filters we don't use: `company_funding`, `company_job_posting_hiring_for` (both Starter).

### Response fields we read

**Person object** (person-object page). Any field can be null.
- `person_id`, `first_name`, `last_name`, `full_name`, `linkedin_url`, `current_job_title`, `headline`
- `location {country, country_code, state, city}`
- `job_history[] {title, company_name, current, seniority, departments[], company_id, ...}`. **Seniority and department exist only inside `job_history`**, so we read them from the current job.
- `email {status, revealed, email, verification_method, email_mx_provider}`. Documented `status` values: **`VERIFIED`, `UNAVAILABLE`** only. `verification_method`: `SMTP`, `BOUNCEBAN`. Not revealed in search results.
- `mobile {...}`: never requested.

**Company object** (company-object page). Any field can be null.
- `company_id`, `name`, `website`, `domain` (root domain), `industry`, `type`, `employee_count`, `employee_range`
- `location {country, country_code, state, city}`
- `attributes {is_b2b, has_pricing, ...}` (each true/false/null)
- `technology {count, technology_names[], technology_list[{name, category}]}`
- `funding {count, total_funding, latest_funding_date, latest_funding_stage, funding_events[{amount, raised_at, stage, link}]}`
- `job_postings {active_count, active_titles[]}`
- **Not stated:** whether `technology`, `funding` and `job_postings` are filled in *search* results or only by enrich-company. The code uses them when present and treats them as unknown when missing. We'll find out from the first live call.

### Enrich-person request

```json
{ "only_verified_email": false, "enrich_mobile": false, "data": { "person_id": "<from search-person>" } }
```
Other accepted identifiers include first+last name + company website, or `linkedin_url`. We only send `person_id`.

### Email status mapping (ours ← Prospeo)

| Prospeo `email.status` | `email.email` present | Our `email_status` | Next step |
|---|---|---|---|
| `VERIFIED` | yes | `valid` | done |
| anything else / `UNAVAILABLE` | no | — | try Hunter |
| error `NO_MATCH` | — | — | try Hunter |

### Error codes → how the code treats them

| `error_code` | HTTP | Our handling |
|---|---|---|
| `NO_MATCH` (enrich), `NO_RESULTS` (search) | 400 | Normal "nothing found": return an empty result, cache it, $0 |
| `SERVICE_TEMPORARILY_UNAVAILABLE` | 400 | Retry with backoff |
| "Rate limit exceeded" | 429 | Retry with backoff |
| `INSUFFICIENT_CREDITS` | 400 | Stop all further Prospeo calls this run (treated like a budget refusal) |
| `PLAN_REQUIRED` | 400 | Fail with a message naming the filter. Turn the filter off in settings |
| `INVALID_API_KEY` | 400 | Fail: check `PROSPEO_API_KEY` |
| `INVALID_FILTERS`, `INVALID_DATAPOINTS`, `INVALID_REQUEST`, `INTERNAL_ERROR` | 400 | Fail for that record and continue |

### Still unknown

- Free-plan credit amount and rate limits (pricing page unreadable). The real balance comes from `/account-information`.
- Whether search results include `technology`, `funding` and `job_postings`.
- Full list of `email.status` values beyond VERIFIED and UNAVAILABLE. Anything unrecognised is treated as "not found" and falls through to Hunter.

---

## TheirStack

Used for the hiring signal (US SDR/BDR job posts) and as a tech signal: technology slugs found in the company's job posts (D3).

### Sources read (2026-11-24)
https://theirstack.com/docs/llms.txt · /en/docs/api-reference/jobs/search_jobs_v1.md · /en/docs/api-reference/companies/search_companies_v1.md · /en/docs/api-reference/companies/technographics_v1.md · /en/docs/api-reference/authentication.md · /en/docs/pricing/credits.md · /en/docs/pricing/plans.md · /en/docs/api-reference/rate-limit.md · /en/docs/api-reference/features/count-results.md · /en/docs/api-reference/guides/avoid-getting-same-jobs-twice.md · /en/docs/api-reference/catalog/get_catalog_keywords_v0.md · https://api.theirstack.com/openapi.json (partial) · https://theirstack.com/en/technology/hubspot (slug). No last-updated dates were shown.

### Facts
- Base `https://api.theirstack.com`, auth header `Authorization: Bearer <key>`.
- **Job search:** `POST /v1/jobs/search`. Needs at least one date or company filter. Fields we use:
  - `job_title_or` (case-insensitive, all words in any order, not regex; **a comma inside a value gives 422**)
  - `job_country_code_or: ["US"]`, `company_country_code_or: ["US"]`
  - `posted_at_max_age_days`
  - `min_employee_count` / `max_employee_count`
  - `limit` (default 25), `page` (0-based)
- **Response:** `{data: [job], metadata: {total_results?, truncated_results, ...}}`.
  - Job fields: `id`, `job_title`, `url`, `final_url`, `source_url`, `date_posted`, `company_domain`, `technology_slugs`, `company_object`.
  - `company_object` fields: `name`, `domain`, `employee_count`, `country_code`, `industry`, `is_recruiting_agency`, `technology_slugs`, `funding_stage`, `last_funding_round_date`, `linkedin_url`.
- **HubSpot slug: `hubspot`.** Confirmed by the official technology page's own API example (`company_technology_slug_or: ["hubspot"]`).
- **Credits:** two buckets.
  - API credits: job search costs **1 per job returned**. Zero results are free. Repeat requests are charged again, so our own cache matters.
  - Company credits: app use only, not used by the API.
  - **Free plan: 200 API credits per month (confirmed).** Max 25 results per page and 5 pages on the free plan.
  - Responses carry `X-Credits-Charged` and `X-Credits-Remaining` headers.
- **Technographics** `POST /v1/companies/technologies` costs 3 credits per lookup. Not used: the tech data in job posts is free with the job search (D3).
- **Rate limits (free):** 4/second, 10/minute, 50/hour, 400/day. Settings throttle to one call every 6.5 seconds.
- **Errors:** `{error: {code, title, description}}` on 400/402/422/500. 429 means rate limited (retry with backoff). **402 has no description in the docs; we treat it as out of credits** and stop calling TheirStack for the run. `metadata.truncated_results > 0` also means credits ran out mid-request.

## Hunter

Fallback email finder (after Prospeo) and email verifier.

### Sources read (2026-11-24)
https://hunter.io/api-documentation (long page: the finder and verifier sections were only summarized) and **https://hunter.io/openapi.json** (machine-readable spec: security schemes and response schemas read directly). No last-updated date.

### Facts
- Base `https://api.hunter.io/v2`. Auth: `api_key` query param, **`X-API-KEY` header**, or `Authorization: Bearer`. **We use the `X-API-KEY` header** (rule 7: the key never goes in URLs).
- **Email Finder:** `GET /email-finder?domain=&first_name=&last_name=` (or `full_name`), plus `max_duration` 3–20 (default 10).
  - 200 response: `data {email (nullable), score (nullable int), accept_all, position, verification {date, status}, sources[...]}`.
  - **404 = no matching profile** (we treat it as "not found", cached, no charge).
  - A credit is charged only when an email is found.
- **Email Verifier:** `GET /email-verifier?email=`.
  - 200 response: `data {status, result (deprecated), score, accept_all, webmail, disposable, ...}`.
  - `status` values (docs): `valid`, `invalid`, `accept_all`, `webmail`, `disposable`, `unknown`.
  - **202** means verification is still running: poll again (we retry with backoff). **222** means the remote SMTP server gave an unexpected response, retry later: we record `unknown`.
  - Costs **0.5 credit**.
- **Account:** `GET /account`, free. `data.requests.credits|searches|verifications.{used, available, remaining}`.
- **Free-plan monthly credits: not stated** (the spec's 50 is unconfirmed). The real balance comes from `/account`.
- **Errors:** `{errors: [{id, code, details}]}`.
  - **429 = usage limit reached (out of credits).** We stop calling Hunter for the run.
  - **403 = rate limit** (also `FreeCreditRestricted` on the finder). We retry only if `details` mentions a rate limit; otherwise we fail.
  - 451 = email claimed for removal: treated as not found, never output.
- **Rate limits:** finder 15/second, 500/minute; verifier 10/second, 300/minute.

**Status mapping (ours ← Hunter):** valid → `valid`; accept_all → `catch_all`; invalid → `invalid` (dropped from export); webmail / disposable / unknown / 222 → `unknown`.

## Tavily

Page text for qualification (extract) and funding-news search (D1: the only way we read company websites).

### Sources read (2026-11-24)
https://docs.tavily.com/ · /documentation/api-reference/endpoint/search · /documentation/api-reference/endpoint/extract · /documentation/api-credits · /documentation/rate-limits · /documentation/api-headers · /documentation/best-practices/best-practices-extract · /llms.txt. No last-updated dates.

### Facts
- Base `https://api.tavily.com`, header `Authorization: Bearer tvly-...`.
- **Search:** `POST /search`. Fields we use:
  - `query`, `search_depth: "basic"` (1 credit; advanced = 2), `topic: "news"`
  - `start_date: "YYYY-MM-DD"` (publish-date filter, used for the 180-day funding window)
  - `max_results` (0–20), `include_usage: true`
- Search response: `results[] {title, url, content, score, published_date}`, `usage {credits}`, `request_id`.
- **Extract:** `POST /extract`. Fields we use:
  - `urls` (1–20 per request), `extract_depth: "basic"`, `format: "markdown"`, `include_usage: true`
- Extract response: `results[] {url, raw_content}`, `failed_results[] {url, error}`, `usage`. The docs say to check both arrays even on HTTP 200.
- **Credits:** basic extract costs 1 credit per 5 *successful* URLs. **Failed extractions are never charged.** **Free plan: 1,000 credits per month (confirmed).** Pay-as-you-go is $0.008 per credit.
- **Rate limit (free):** 100 requests per minute.
- **Errors:** 400, 401 (bad key), 422, 429 (honour `Retry-After`), **432 = key or plan limit exceeded** and **433 = PayGo limit exceeded** (both treated as out of credits), 500.
- **robots.txt:** the docs don't say whether Tavily honours robots.txt. Per spec rule 3 we only *read public pages through Tavily*. We never fetch sites ourselves (D1).

## Anthropic Claude

ICP translation (Sonnet), qualification (Sonnet), title classification (Haiku).

### Sources (2026-11-24)
Claude API skill reference bundled with Claude Code (models table cached 2026-11-03; Python SDK structured outputs, JSON-schema limits, error classes), plus the **installed SDK `anthropic` 2.13.0** itself (signatures checked in `resources/messages` and `resources/beta/messages`).

### Facts
- **Models and prices** (per million tokens, input / output):
  - `claude-sonnet-5-5`: $2.00 / $10.00
  - `claude-haiku-5-5`: $0.10 / $0.50 (prompts up to 100K tokens)
  - Batch API is 50% off (not used yet).
- **Structured output:** `client.messages.parse(model=..., max_tokens=..., messages=..., output_format=PydanticModel)` gives a validated `response.parsed_output`. Schemas need `additionalProperties: false` (Pydantic `extra="forbid"`).
- **Schema limits:** `minimum`/`maximum`/`minLength` etc. aren't supported by the API. **The Python SDK strips them and validates client-side**, so our `Confidence` (0–1) range is still enforced.
- **Thinking:**
  - Sonnet 5.5 can't disable thinking (`disabled` gives a 400). We leave it adaptive and set `output_config={"effort": ...}` from settings.
  - Haiku 5.5 defaults to effort `medium`; we set `low` for title classification.
- **Refusals:** check `stop_reason == "refusal"` before reading output. **Sonnet calls use the server-side fallback** (`client.beta.messages.parse(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`). The installed SDK's beta `parse` accepts both `fallbacks` and `output_format`. Haiku 5.5 has no server-side fallback, so a refusal there is recorded as a failure.
- **Which model answered:** `response.model` is the model that produced the message. A `fallback_message` entry in `response.usage.iterations` means the fallback served it (Claude API reference, verified 2026-11-24). We store `response.model` in `assessments.model` and log when the fallback was used. Cost is priced at the served model's rate if configured, else the requested model's.
- **Usage for cost:** `response.usage.input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.
- **Errors (Python):** `RateLimitError` (429), `InternalServerError` (≥500, including 529 overloaded), `APIConnectionError` / `APITimeoutError` (network) are retryable. `AuthenticationError`, `BadRequestError` etc. are not. The SDK's own retries are turned off (`max_retries=0`) so our gatekeeper does the retrying and logging.
- **Note:** the skill's general default is Claude Opus 5.5. The build spec explicitly names Sonnet 5.5 and Haiku 5.5, so those are used (configurable in `settings.yaml`).
