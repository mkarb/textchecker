# Technical-Manual Proofreader — Code Review Report

## 1. Executive summary

The pipeline is functionally coherent, but two issues make it unsafe to run as-is for a CUI-handling service. Most urgent: the complete raw document body is persisted to `findings.json` and served back over the LAN API (`_doc_text` is never stripped outside the judge path), directly violating the pipeline's central "never persist/expose the document text" contract. Alongside this, the web tier has a cluster of availability problems — an unbounded decompression path for `.docx` zip bombs, a reaper that deletes still-running jobs by input-file mtime, and synchronous file I/O on the asyncio event loop. Correctness-wise, merged-cell handling in `extract.py` duplicates and in some cases destroys table text (breaking glossary ground-truth), the judge silently truncates its findings payload when `num_ctx` is unset, and the recurring-phrase pass is quadratic and will stall the single model lane on large manuals. Testing coverage is broad on negative assertions but has systematic gaps on the positive detection paths (page sequence, spelling, acronym conflict, customer consistency). None of the findings below are speculative; each was verified against the code.

## 2. Critical & high-severity findings

1. **Full CUI document text persisted to disk and served over HTTP** — `server.py:207` (also `:258`), **critical**.
   `main.gather()` stashes the entire normalized document into `findings["_doc_text"]` (`main.py:48`). `_stage_a` and `_model_worker` write `json.dumps({"deterministic": findings, ...})` verbatim to `<job>/findings.json`, and `GET /api/jobs/{job_id}/findings` returns that file as JSON. Unlike the judge path (`judge.py:80` strips every `_`-prefixed key before the model ever sees it), the persistence/serving path has no equivalent guard, so the whole manual lands in cleartext on disk for `RETAIN_HOURS` and is retrievable by the owning session. This is the exact leak the design exists to prevent.
   *Fix:* serialize `{k: v for k, v in findings.items() if not str(k).startswith("_")}` in both `_stage_a` and `_model_worker`; better, pass the doc text to `render_md`/`annotate_acronym_counts` as an explicit argument instead of smuggling it inside `findings`.
   *(Merged: reported identically by `deps-ops` at `server.py:207` and `architecture-design` at `server.py:208` — the same defect from a data-handling and a module-boundary lens.)*

2. **Zip-bomb / decompression DoS on `.docx` upload** — `server.py:319`, **high**.
   The 25 MB cap is checked on the compressed bytes only. A ~24 MB `.docx` whose internal `word/document.xml` (or media parts) inflates to gigabytes passes `len(data) <= MAX_BYTES`, then `from_docx` calls `docx.Document(...)` (`extract.py:157`), inflating and lxml-parsing it in RAM. With `STAGE_A_WORKERS` threads each doing this, the box OOMs and the service dies for everyone.
   *Fix:* before parsing, inspect the ZIP members' summed `file_size` / entry count / compression ratio and abort past a cap; and/or parse `document.xml` with a size-bounded, hardened lxml parser as `from_xml` already does.

3. **Reaper deletes still-active jobs by input-file mtime** — `server.py:287` (reaper body `286-290`), **high**.
   Retention is keyed on the upload file's mtime, set once and never refreshed. With `MODEL_LANES=1` and a deep queue (or a hung model), a job can sit in `awaiting_model`/`judging` past `RETAIN_HOURS` (default 12h); the reaper then `rmtree`s the live job dir and pops `_jobs[job_id]`. The worker's later write of `findings.json`/`report.md` fails on the missing dir (only logged), `_stage_a`'s `next(p for p in jdir.iterdir())` can raise `StopIteration`, and the owner's poll gets a 404 mid-run — an accepted document is silently lost.
   *Fix:* under the lock, skip directories whose `_jobs` record is non-terminal (ACTIVE) before `rmtree`, mirroring `_prune_jobs_locked`; or base retention on a completion timestamp rather than input mtime.
   *(Merged: raised three times — `robustness-concurrency` `server.py:287`, `architecture-design` `server.py:287`, and `security-webservice` `server.py:288` — same root cause, consolidated here.)*

4. **Recurring-phrase detection is O(P²) and stalls the single lane** — `checks.py:113`, **high**.
   `_suppress_subphrases` compares every distinct candidate phrase against every other and re-runs `s.split()`/`l.split()` inside the inner loop. On caps-heavy manuals (the documented normal case) P grows ~linearly with document size: measured 500 blocks → 0.57s, 2000 → 13.1s, 8000 → 160s. Because the server runs a single model lane with synchronous `gather`, one large upload freezes the whole service for minutes.
   *Fix:* pre-split each phrase to a cached token tuple; invert a `token → phrases` index and only compare a short phrase against longer phrases sharing its rarest token instead of all-pairs; raise the raw floor so fewer phrases enter suppression (see finding 15).

5. **Judge silently truncates its findings payload when `num_ctx` is unset** — `judge.py:134`, **high**.
   `_shrink_findings` budgets ~60k chars of findings JSON, but `_call_native` sets `body["options"]["num_ctx"]` only when `PROOFER_NUM_CTX` is present. With it unset, Ollama applies its small default context (2048/4096) and truncates the prompt server-side without error. The model then judges only the surviving head of `recurring_phrases`/`spelling_candidates`/`acronym_issues`, returns valid JSON with a normal finish reason, and no truncation/repair/validation retry fires. The report looks complete but omits real findings with zero disclosure.
   *Fix:* default `num_ctx` to something sized to the budget (e.g. `budget/3 + max_tokens` headroom) whenever `PROOFER_NUM_CTX` is unset, and reconcile the 60k budget with the configured context so the shrink target never exceeds what the model actually reads; optionally compare `prompt_eval_count` against an expected minimum to detect truncation.

## 3. Medium / low findings (grouped by module)

**extract.py**
- `extract.py:138` (medium) — `_walk_docx_table` duplicates text of `gridSpan` (horizontal) and `vMerge` (vertical) merged cells; `row.cells` yields the same `_Cell` per spanned column, inflating recurring-phrase/acronym passes with phantom repeats.
- `extract.py:574` (medium) — the same merged-cell duplication flattens a merged glossary acronym column to `ACR | ACR | Expansion`; `_gloss_pair` reads `exp==acr` and drops the definition, so formally defined acronyms are treated as undefined. Fixed by resolving `:138`.
- `extract.py:101` (low) — footnote/endnote XML parsed with lxml's DEFAULT parser (`etree.fromstring`), bypassing the hardened parser (`resolve_entities=False`, `load_dtd=False`, `no_network=True`, `huge_tree=False`) used by `from_xml`; safe only because of the pinned libxml2 2.11.9 amplification cap. Reuse the hardened parser here.

**checks.py**
- `checks.py:447` (medium) — `_org_key` strips descriptive words (Defense/Systems/Aerospace/…), collapsing distinct entities ("Acme Defense" vs "Acme Systems") into one cluster → false `inconsistent_org_form`. Restrict `_SUFFIX_WORDS` to true legal-form suffixes.
- `checks.py:429` (medium) — `ORG_RE`'s `(?:&|and|of)?` joiner bridges two company names ("Lockheed Martin and General Dynamics Corporation") into one captured org. Drop `and`/`of` or split on ` and `.
- `checks.py:144` (medium) — `find_recurring_phrases` feeds the full `c>=2` raw set into the O(P²) suppression before the `min_count=3` filter, maximizing P exactly where the quadratic runs. Apply `min_count` (or a "keep if `c>=min_count` or superphrase-of" pre-pass) before suppression. *(Same root perf issue as finding 4.)*
- `checks.py:385` (low) — the docx synthetic trailing `Page 1 of N` block is included in the `PAGE_OF` sequence, so real in-text `Page X of Y` strings make it trip `duplicate_page_number`/`out_of_order_page`. Exclude the `sections/pagination` block from `seq`.
- `checks.py:508` (low) — `annotate_acronym_counts` recompiles a regex and rescans the full `doc.text` per acronym row → O(rows×doc); measured ~6.9s for 150 rows over 2.6MB at render time. Tokenize once into a Counter / cache compiled patterns.

**judge.py**
- `judge.py:305` (medium) — `schema_constrained` is derived from the latched `served_native` flag, not the endpoint that produced the kept `result`; a native (schema) result kept after a failed `/v1` repair is falsely reported as *not* schema-constrained (and vice versa). Derive from `schema_on and endpoint.endswith('/api/chat')`.
- `judge.py:301` (low) — on a failed truncation/repair/validation retry, the second `_transport` call's usage/finish/response_chars are discarded, so `_meta.usage` under-reports token spend and mis-attributes the finish. Accumulate usage across all calls.

**main.py**
- `main.py:206` (medium) — `render_md` crashes (`TypeError` in `str.join`) when `customer.normalize`/`suspected_wrong` contains a non-string (unvalidated on the `/v1` fallback or `PROOFER_SCHEMA=0` path), aborting report generation for the job. Coerce with `str(x)` (lines 206 and 209).
- `main.py:118` (medium) — four near-identical acronym-table emit blocks (6-col vs 4-col × model vs deterministic branch); a schema change must be made in four places or branches silently diverge / emit malformed markdown. Extract one `_emit_acronym_table(...)` helper.
- `main.py:150` (low) — a valid model judgment with an empty/absent `acronym_table` is mislabeled provenance "deterministic — model echoed input, no table" when the model did not echo. Distinguish the empty-table case when `llm is not None`.

**server.py (concurrency / ops / config)**
- `server.py:316` (medium) — `create_job` does synchronous `mkdir`/`write_bytes` of up to 25 MB on the event loop; concurrent large uploads stall all requests including status polls. Wrap in `asyncio.to_thread`.
- `server.py:476` (low) — `report`/`findings`/`download`/index handlers read files synchronously (`read_text`/`FileResponse`) on the event loop; multi-MB reads freeze the API. Offload to `asyncio.to_thread`.
- `server.py:5` (medium) — module docstring claims the service "binds 127.0.0.1 by default … not internet facing," but `HOST` defaults to `0.0.0.0` (`:50`) — the opposite — for a CUI service. Fix the docstring to match reality or flip the default to loopback with explicit opt-in. *(Reported twice, `architecture-design` and `deps-ops`, same line; merged.)*
- `server.py:386` (low) — raw pipeline exception strings (including absolute server paths from `from_pdf`) are stored as the job `error` and returned verbatim via `GET /api/jobs/{job_id}`, leaking filesystem layout / library internals. Return a generic message; log detail server-side only.
- `server.py:418` (low) — after a restart, `_jobs` is empty and nothing rescans `DATA`, so completed on-disk reports become permanent 404 orphans (still retained on disk until reaped). Reconstruct minimal terminal records on startup or persist the job index.

**static/index.html (frontend)**
- `static/index.html:361` (medium) — `showResult()` never checks `r.ok`, so a 404 error body (report reaped/not-ready) is rendered as the report text. Check `r.ok` and wrap in try/catch.
- `static/index.html:351` (low) — poll loop only knows `queued/running/done/error`, but the server emits `extracting`/`awaiting_model`/`judging`/`canceled` and never `running`; intermediate progress and position/ETA are never shown, `canceled` loops forever, and `running` is dead code. Align client vocabulary with the server.
- `static/index.html:350` (low) — `poll()` doesn't check `r.ok`, so after a job is reaped the 404 falls through every branch and the client polls a dead job forever. Surface a terminal "job expired" and stop the loop.

**requirements.txt (deps)**
- `requirements.txt:9` (low) — all deps use open-ended `>=` with no upper bound; a fresh install can pull an unvetted major of fastapi/starlette/pydantic/uvicorn and break upload/serialization behavior. Add ceilings and/or ship a pinned lock.

**testing-coverage** (all medium/low — systematic gaps on positive-detection paths)
- `verify_docx_coverage.py:59` (medium) — page-sequence detectors are only ever asserted *not* to fire; no positive test, so a refactor that yields no page findings passes. Add a fixture asserting `duplicate_page_number`/`page_gap`/`out_of_order_page`/`inconsistent_total` fire.
- `verify_pdf.py:68` (medium) — `spelling_candidates` (hyphenation/prefix/compound/acronym-plural allowlisting) is never invoked; only negative de-hyphenation is checked. Add a test asserting a real misspelling is flagged and decoys are not.
- `verify_glossary.py:68` (medium) — `acronym_consistency` findings (`conflicting_expansions`, `multiple_acronyms_one_term`) are discarded; no positive coverage. Assert the RCM conflict is produced.
- `verify_glossary.py:79` (medium) — `customer_consistency` (org clustering, `inconsistent_org_form`, `possible_wrong_customer`) is never called by any test. Add a test using the sample fixtures.
- `verify_server.py:98` (low) — server model-failure branches (`judge` raises → `{'_error'}`; `_parse_error`) are untested; only the clean success path runs. Add fake-judge tests for both failure modes.
- `verify_judge_hardening.py:106` (low) — `_shrink_findings` is only tested when it shrinks back under budget; the "floor reached, still over budget" branch (must still yield valid JSON) is uncovered. Add an oversized-`existing_acronyms` test.
- `verify_pdf_upload.py:10` (low) — the rejection-message assertion parses `server.py` source text instead of exercising the 400 response; tests string layout, not behavior. Replace with a `TestClient` POST.

## 4. Per-dimension coverage

| Dimension | Confirmed | Raised |
|---|---:|---:|
| security-webservice | 3 | 5 |
| security-parsing | 1 | 1 |
| correctness-extract | 2 | 2 |
| correctness-checks | 3 | 4 |
| correctness-judge | 2 | 2 |
| correctness-main | 2 | 2 |
| robustness-concurrency | 3 | 6 |
| architecture-design | 4 | 4 |
| performance | 4 | 4 |
| testing-coverage | 7 | 8 |
| frontend | 3 | 3 |
| deps-ops | 4 | 4 |
| **Total** | **34** | **45** |

*Note:* the 34 confirmed findings include cross-dimension duplicates that were merged in this report — the CUI `_doc_text` leak (deps-ops + architecture-design) and the reaper-vs-inflight bug (robustness-concurrency ×2 + security-webservice) and the `0.0.0.0` docstring (architecture-design + deps-ops). Deduplicated, the report covers **30 distinct defects**.

## 5. Top 5 recommended fixes (priority order)

1. **Strip `_`-prefixed keys before writing/serving `findings.json`** (`server.py` `_stage_a` + `_model_worker`). Stops the CUI document-body leak. *Effort: small* — a one-line dict comprehension at each of the two write sites, plus a check that `render_md` still gets `_doc_text` from the in-memory record.
2. **Gate the reaper on job status, not input mtime** (`server.py:287`). Prevents destroying in-flight jobs. *Effort: small–medium* — look up `_jobs` under the lock and skip non-terminal records; ideally add a completion timestamp for retention.
3. **Add a decompressed-size / ratio guard for `.docx` uploads** (`server.py:319` / `extract.py:157`). Closes the zip-bomb DoS. *Effort: medium* — inspect ZIP member sizes before `docx.Document`, and reuse the hardened lxml parser (also fixes the footnote parser at `extract.py:101`).
4. **Default `num_ctx` to the findings budget in `_call_native`** (`judge.py:134`). Stops silent server-side prompt truncation that drops real findings. *Effort: small* — compute a default from the budget when `PROOFER_NUM_CTX` is unset; optionally assert on `prompt_eval_count`.
5. **Fix merged-cell extraction in `_walk_docx_table`** (`extract.py:138`). Removes duplicated table text and restores glossary-as-ground-truth (`extract.py:574`). *Effort: medium* — build `cells` from unique `_tc` elements and skip `vMerge='continue'` rows.

*Runner-up (worth batching with the web-tier work): make `_suppress_subphrases` sub-quadratic (`checks.py:113`/`:144`) and move blocking file/upload I/O off the event loop (`server.py:316`/`:476`) — together these keep the single-lane service responsive on large manuals. Effort: medium.*