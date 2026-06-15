# Claude Code edit instructions -- validated repair: code validates, the model only fixes

**Targets:** `judge.py` (Edits 1-2) and `main.py` (Edit 3). **Apply AFTER
`EDIT_INSTRUCTIONS_judge_hardening.md`** -- the anchors live in code that doc creates.
Idempotent; no new dependencies (deliberately no `jsonschema` package).

> Same drift caveat as the hardening doc: if a Find block does not match exactly once,
> apply semantically or paste the current file.

## What this adds
The hardening doc's repair round fires only when JSON fails to **parse**. This closes the
remaining gap: output that parses but is the wrong **shape** -- missing required keys, wrong
container types, an out-of-enum `status`, empty required strings. A ~40-line dependency-free
validator (mirroring `OUTPUT_SCHEMA`) now checks every successful parse; on violations it runs
ONE repair round whose message lists the **specific** errors, and code re-validates the reply.

Two deliberate design choices, stated so nobody "improves" them later:
- **Structural validation only, never semantic.** Rules like "the expansion's initials must
  match the acronym" misfire on legitimate entries (MX for maintenance), and a false
  validation error triggers a repair that can mutate a *good* judgment. The render-time
  count-join + phantom-row filter already police semantic quality deterministically.
- **A failed repair keeps the ORIGINAL.** A parsed-but-imperfect judgment is partially
  usable (`render_md` is defensive); discarding it for a `_parse_error` would be strictly
  worse. The attempt is disclosed in `_meta.retries.validation` and the report provenance.

Where this earns its keep: the `/v1` fallback and `PROOFER_SCHEMA=0` paths, where no decoder
grammar protects the shape. On the schema-constrained native path it is a cheap no-op
(confirmed: your Ollama build accepts a full JSON Schema as `format`, so `PROOFER_SCHEMA=0`
is purely an escape hatch). Bound: at most one corrective call per job from this edit.

## Edit 1 -- `judge.py`: insert `_validate_judgment`
**Skip if** `_validate_judgment` is already defined. Insert immediately **before**:
```python
def judge(findings, base_url=None, model=None, timeout=180, context=None):
```
the following:
```python
def _validate_judgment(result):
    """Structural validation ONLY -- required keys, container types, enum, non-empty
    required strings -- mirroring OUTPUT_SCHEMA. Deliberately NOT semantic: rules like
    "expansion initials must match the acronym" misfire on legitimate entries (MX for
    maintenance, nested acronyms), and a false validation error triggers a repair round
    that can mutate a perfectly good judgment. Code validates; the model only repairs.
    Matters most on the /v1 fallback and PROOFER_SCHEMA=0 paths, where no decoder
    grammar protects the shape. Returns a bounded list of error strings."""
    errs = []
    if not isinstance(result, dict):
        return ["top-level value is not a JSON object"]
    for key, typ in (("acronym_table", list), ("acronym_issues", list),
                     ("misspellings", list), ("customer", dict)):
        if key not in result:
            errs.append(f"missing required key: {key}")
        elif not isinstance(result[key], typ):
            errs.append(f"{key} must be a JSON {'array' if typ is list else 'object'}")
    for i, row in enumerate(result.get("acronym_table") or []):
        if not isinstance(row, dict):
            errs.append(f"acronym_table[{i}] is not an object")
            continue
        for req in ("acronym", "expansion", "status"):
            if not isinstance(row.get(req), str) or not row.get(req, "").strip():
                errs.append(f"acronym_table[{i}].{req} missing or empty")
        if isinstance(row.get("status"), str) and row["status"] not in ("existing", "proposed"):
            errs.append(f"acronym_table[{i}].status must be 'existing' or 'proposed', got {row['status']!r}")
    for i, row in enumerate(result.get("misspellings") or []):
        if not isinstance(row, dict):
            errs.append(f"misspellings[{i}] is not an object")
            continue
        if not isinstance(row.get("word"), str) or not row.get("word", "").strip():
            errs.append(f"misspellings[{i}].word missing or empty")
    cust = result.get("customer")
    if isinstance(cust, dict) and not isinstance(cust.get("primary", ""), str):
        errs.append("customer.primary must be a string")
    return errs[:12]                       # bound the repair prompt


def judge(findings, base_url=None, model=None, timeout=180, context=None):
```
(so the file reads validator-then-`def judge(...)`).

## Edit 2 -- `judge.py`: validation-triggered repair branch
**Skip if** the retry chain already mentions `_validate_judgment`. Find (the tail of the
malformed-JSON repair branch -- unique via the `retries["repair"]` line):
```python
        if "_parse_error" not in r2:
            content, finish, usage, model_reported, endpoint, result = (
                content2, finish2, usage2, mr2, ep2, r2)
            retries["repair"]["recovered"] = True
```
Replace with:
```python
        if "_parse_error" not in r2:
            content, finish, usage, model_reported, endpoint, result = (
                content2, finish2, usage2, mr2, ep2, r2)
            retries["repair"]["recovered"] = True
    elif retry_on and "_parse_error" not in result:
        # Parsed fine -- now CODE validates the shape; the model only ever repairs.
        verrs = _validate_judgment(result)
        if verrs:
            repair = messages + [
                {"role": "assistant", "content": content[:6000]},
                {"role": "user", "content": "Your JSON parsed but failed validation:\n- "
                                            + "\n- ".join(verrs)
                                            + "\nReturn ONLY the corrected JSON object -- "
                                              "no prose, no fences."}]
            retries["validation"] = {"attempted": True, "errors": verrs}
            content2, finish2, usage2, mr2, ep2 = _transport(repair, max_tokens)
            r2 = _parse_json(content2)
            if "_parse_error" not in r2 and not _validate_judgment(r2):
                content, finish, usage, model_reported, endpoint, result = (
                    content2, finish2, usage2, mr2, ep2, r2)
                retries["validation"]["recovered"] = True
            # else: KEEP the original parsed result -- partially valid beats discarded;
            # render_md is defensive (.get) and the attempt is disclosed in _meta.
```

## Edit 3 -- `main.py`: disclose validation repairs in the provenance line
**Skip if** `_provenance` already mentions `validation`. Find:
```python
    if r.get("repair", {}).get("recovered"):
        notes.append("recovered via one JSON-repair round")
```
Replace with:
```python
    if r.get("repair", {}).get("recovered"):
        notes.append("recovered via one JSON-repair round")
    if r.get("validation", {}).get("recovered"):
        notes.append("recovered via one validation-repair round")
    elif r.get("validation", {}).get("attempted"):
        notes.append("a validation repair was attempted; the original judgment was kept")
```

## Verify (run from the repo root) -- expect `33 passed, 0 failed`
This suite **supersedes** the hardening verify: t1-t7 re-prove all hardening behaviour, t8-t9
prove validated repair (error-specific message, recovery, and keep-original-on-failure).
No Ollama needed; transport faked and restored; invented content only.
Save as `verify_validated_repair.py`:
```python
import importlib, json, os, sys
import requests as _real_requests
import judge
importlib.reload(judge)

P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

GOOD = json.dumps({"acronym_table": [{"acronym": "WGT", "expansion": "Widget Group Tracker",
                                      "status": "existing", "note": ""}],
                   "acronym_issues": [], "misspellings": [], "customer": {"primary": "ACME"}})
BAD_SHAPE = json.dumps({"acronym_table": [{"acronym": "WGT", "expansion": "",
                                           "status": "bogus", "note": ""}],
                        "acronym_issues": [], "misspellings": [], "customer": {"primary": "ACME"}})

class FakeResp:
    def __init__(self, body): self._b = body
    def raise_for_status(self): pass
    def json(self):
        if self._b is None: raise ValueError("not json")
        return self._b

def native_body(content, finish="stop"):
    return {"message": {"content": content}, "done_reason": finish,
            "prompt_eval_count": 100, "eval_count": 50, "model": "qwen3:30b-a3b"}

def openai_body(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "fallback-model"}

class FakeReq:
    RequestException = _real_requests.RequestException
    def __init__(self, script): self.script = list(script); self.calls = []
    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "body": json})
        return FakeResp(self.script.pop(0))
    def get(self, *a, **k): raise _real_requests.RequestException("no")

os.environ.pop("MODEL", None)
for var in ("PROOFER_SCHEMA", "PROOFER_RETRY", "PROOFER_THINK"):
    os.environ.pop(var, None)
findings = {"recurring_phrases": [], "existing_acronyms": {"WGT": ["Widget Group Tracker"]},
            "acronym_issues": [], "spelling_candidates": [],
            "_doc_text": "SECRET BODY " * 50}

# ---- t1-t7: the hardening suite (unchanged behaviour must hold) ----
fr = FakeReq([native_body(GOOD)]); judge.requests = fr
r = judge.judge(findings)
b = fr.calls[0]["body"]
ok("t1 single call", len(fr.calls) == 1)
ok("t1 format is the full schema", isinstance(b["format"], dict)
   and b["format"].get("additionalProperties") is False
   and set(b["format"]["required"]) == {"acronym_table", "acronym_issues", "misspellings", "customer"})
ok("t1 think off by default", b["think"] is False)
ok("t1 _doc_text never sent", "SECRET BODY" not in json.dumps(b))
ok("t1 default model is qwen3:30b-a3b", b["model"] == "qwen3:30b-a3b")
ok("t1 parsed table present", r["acronym_table"][0]["acronym"] == "WGT")
ok("t1 meta says schema_constrained", r["_meta"]["schema_constrained"] is True)
ok("t1 no retries recorded (validator passes silently)", "retries" not in r["_meta"])

fr = FakeReq([native_body('{"acronym_table":[{"acron', "length"), native_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t2 truncation retry happened", len(fr.calls) == 2)
ok("t2 num_predict doubled", fr.calls[1]["body"]["options"]["num_predict"]
   == 2 * fr.calls[0]["body"]["options"]["num_predict"])
ok("t2 recovered", "_parse_error" not in r and r["customer"]["primary"] == "ACME")
ok("t2 meta records truncation retry", r["_meta"]["retries"]["truncation"]["recovered"] is True)

fr = FakeReq([native_body("Sure! Here you go: not json"), native_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t3 repair round ran", len(fr.calls) == 2)
ok("t3 repair message present", any("not valid JSON" in m.get("content", "")
   for m in fr.calls[1]["body"]["messages"]))
ok("t3 recovered", "_parse_error" not in r)
ok("t3 meta records repair", r["_meta"]["retries"]["repair"]["recovered"] is True)

fr = FakeReq([native_body('["a","b"]'), native_body('["a","b"]')])
judge.requests = fr
r = judge.judge(findings)
ok("t4 non-object guarded", "_parse_error" in r and "non-object" in r["_parse_error"])
ok("t4 _meta still attached", "_meta" in r)

fr = FakeReq([None, openai_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t5 fell back to OpenAI endpoint", fr.calls[1]["url"].endswith("/chat/completions"))
ok("t5 fallback parsed fine", r["customer"]["primary"] == "ACME")

big = {"recurring_phrases": [{"phrase": f"Widget Phrase {i}", "count": i,
                              "locations": [f"body/p[{j}]" for j in range(40)]}
                             for i in range(300)],
       "existing_acronyms": {"WGT": ["Widget Group Tracker"]},
       "acronym_issues": [], "spelling_candidates": [], "_doc_text": "X" * 100}
fr = FakeReq([native_body(GOOD)]); judge.requests = fr
r = judge.judge(big)
sent = fr.calls[0]["body"]["messages"][-1]["content"]
sent_json = sent.split("Deterministic scan output:\n", 1)[1].rsplit("\n\n/no_think", 1)[0]
parsed = json.loads(sent_json)
ok("t6 payload VALID json after shrink", isinstance(parsed, dict))
ok("t6 under/near budget", len(sent_json) <= 60000)
ok("t6 existing_acronyms preserved", parsed["existing_acronyms"]["WGT"] == ["Widget Group Tracker"])
ok("t6 shrink disclosed", r["_meta"]["findings_shrunk"]["original_chars"] > 60000)

os.environ["PROOFER_SCHEMA"] = "0"
fr = FakeReq([native_body(GOOD)]); judge.requests = fr
r = judge.judge(findings)
ok("t7 schema opt-out -> format 'json'", fr.calls[0]["body"]["format"] == "json")
os.environ.pop("PROOFER_SCHEMA", None)

# ---- t8: parses but fails CODE validation -> error-specific repair, recovered ----
fr = FakeReq([native_body(BAD_SHAPE), native_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t8 validation repair fired (2 calls)", len(fr.calls) == 2)
msg = fr.calls[1]["body"]["messages"][-1]["content"]
ok("t8 repair message names the SPECIFIC errors",
   "failed validation" in msg and "status" in msg and "expansion" in msg)
ok("t8 recovered to clean judgment", r["acronym_table"][0]["status"] == "existing")
ok("t8 meta records validation retry", r["_meta"]["retries"]["validation"]["recovered"] is True
   and len(r["_meta"]["retries"]["validation"]["errors"]) >= 2)

# ---- t9: validation repair fails -> ORIGINAL parsed result kept, attempt disclosed ----
fr = FakeReq([native_body(BAD_SHAPE), native_body(BAD_SHAPE)])
judge.requests = fr
r = judge.judge(findings)
ok("t9 original judgment kept (not discarded)", r["acronym_table"][0]["status"] == "bogus"
   and "_parse_error" not in r)
ok("t9 attempt disclosed, no recovered flag",
   r["_meta"]["retries"]["validation"]["attempted"] is True
   and "recovered" not in r["_meta"]["retries"]["validation"])
ok("t9 bounded: exactly one corrective call", len(fr.calls) == 2)
ok("t9 _meta attached", "_meta" in r)

judge.requests = _real_requests
print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
```

## Rollback
`git checkout -- judge.py main.py`.
