# Claude Code edit instructions -- judge hardening: schema-constrained output, structural shrink, retries

**Targets:** `judge.py` (Edits 1-6) and one optional edit to `main.py` (Edit 7). Idempotent.

> **Drift caveat -- read first.** These anchors assume the `judge_truncation` and
> `ollama_native_thinking` edits are applied (they are, per your runs). If the agent has
> changed `judge.py` since and any Find block does not match **exactly once**, the file has
> drifted: apply the edit **semantically** (each edit states its intent) or paste the current
> `judge.py` back to me. The embedded verify validates behaviour either way.

## What this fixes
1. **Malformed/echoed JSON becomes structurally impossible.** The full output schema now goes
   into Ollama's `format` (constrained decoding). The decoder cannot emit invalid JSON, and
   `additionalProperties:false` blocks the failure mode where the model echoes the input
   findings keys back instead of transforming them. `PROOFER_SCHEMA=0` reverts to `"json"`.
2. **The silent `[:60000]` slice is gone.** It cut mid-token, so on a large manual the model
   received *broken* findings JSON with no signal. `_shrink_findings` now drops the bulkiest,
   least-needed fields first (per-item locations -> spelling contexts -> progressive list caps,
   floor 10, recurring phrases kept by highest count), always emits **valid** JSON, never
   reduces `existing_acronyms`, and reports what was elided in `_meta.findings_shrunk`.
3. **The one network call can now recover.** Truncation (`finish_reason` length/limit) gets one
   retry at doubled `num_predict` (cap 16000). Any other parse failure gets one repair round
   ("return ONLY the corrected JSON"). `PROOFER_RETRY=0` disables. Recorded in `_meta.retries`.
4. **Top-level-array guard.** A non-object reply (e.g. `["..."]`) used to crash the
   `result["_meta"]` attach with a TypeError; now it is a clean `_parse_error`.
5. **Fallback actually falls back.** The native path now also falls back on `ValueError`
   (server answered, but not with JSON) -- not just on transport errors.
6. **Real default model.** `qwen3:27b` does not exist; default is now `qwen3:30b-a3b`
   (both `judge()` and `probe()`).

Design note: with the schema constraint on, the repair round is nearly unreachable on the
native path -- it earns its keep on the `/v1` OpenAI-compat fallback (which has no schema
parameter) and under `PROOFER_SCHEMA=0`.

## Edit 1 -- `judge.py`: add `OUTPUT_SCHEMA` + `_shrink_findings` after `SYSTEM`
**Skip if** `OUTPUT_SCHEMA` is already defined. Find (the closing lines of `SYSTEM`):
```python
    '{"acronym_table":[{"acronym":"","expansion":"","status":"existing|proposed","note":""}],'
    '"acronym_issues":[{"acronym":"","problem":"","note":""}],'
    '"misspellings":[{"word":"","suggestion":"","context":""}],'
    '"customer":{"primary":"","normalize":[""],"suspected_wrong":[""]}}'
)
```
Replace with:
```python
    '{"acronym_table":[{"acronym":"","expansion":"","status":"existing|proposed","note":""}],'
    '"acronym_issues":[{"acronym":"","problem":"","note":""}],'
    '"misspellings":[{"word":"","suggestion":"","context":""}],'
    '"customer":{"primary":"","normalize":[""],"suspected_wrong":[""]}}'
)

# The same schema, machine-enforced. Passed as Ollama's `format`, the server constrains
# decoding so the model PHYSICALLY cannot return anything else -- which eliminates both
# malformed JSON and the failure mode where the model echoes the input findings keys
# back instead of transforming them. additionalProperties:false is what blocks the echo.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "acronym_table": {"type": "array", "items": {
            "type": "object",
            "properties": {"acronym": {"type": "string"}, "expansion": {"type": "string"},
                           "status": {"type": "string", "enum": ["existing", "proposed"]},
                           "note": {"type": "string"}},
            "required": ["acronym", "expansion", "status"],
            "additionalProperties": False}},
        "acronym_issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"acronym": {"type": "string"}, "problem": {"type": "string"},
                           "note": {"type": "string"}},
            "required": ["acronym", "problem"],
            "additionalProperties": False}},
        "misspellings": {"type": "array", "items": {
            "type": "object",
            "properties": {"word": {"type": "string"}, "suggestion": {"type": "string"},
                           "context": {"type": "string"}},
            "required": ["word", "suggestion"],
            "additionalProperties": False}},
        "customer": {"type": "object",
                     "properties": {"primary": {"type": "string"},
                                    "normalize": {"type": "array", "items": {"type": "string"}},
                                    "suspected_wrong": {"type": "array", "items": {"type": "string"}}},
                     "required": ["primary"],
                     "additionalProperties": False},
    },
    "required": ["acronym_table", "acronym_issues", "misspellings", "customer"],
    "additionalProperties": False,
}


def _shrink_findings(findings, budget=60000):
    """Size-bound the findings payload WITHOUT ever corrupting it. The old
    json.dumps(...)[:60000] sliced mid-token, silently feeding the model broken JSON on
    large manuals. This drops the bulkiest, least-needed fields first and always emits
    valid JSON; what was elided is reported so the provenance block can disclose it.
    Returns (json_str, shrink_meta_or_None). existing_acronyms is never reduced."""
    payload = {k: v for k, v in findings.items() if not str(k).startswith("_")}
    s = json.dumps(payload, ensure_ascii=False)
    if len(s) <= budget:
        return s, None
    import copy
    p = copy.deepcopy(payload)
    steps = []

    def out():
        return json.dumps(p, ensure_ascii=False)

    for key in ("recurring_phrases", "acronym_issues", "spelling_candidates"):
        for r in p.get(key) or []:
            if isinstance(r, dict):
                r.pop("locations", None)
    steps.append("dropped per-item locations")
    if len(out()) > budget:
        for r in p.get("spelling_candidates") or []:
            if isinstance(r, dict) and isinstance(r.get("context"), str):
                r["context"] = r["context"][:60]
        steps.append("trimmed spelling contexts")
    caps = {"recurring_phrases": 60, "spelling_candidates": 80, "acronym_issues": 80}
    while len(out()) > budget:
        shrunk = False
        for key, floor in (("recurring_phrases", 10), ("spelling_candidates", 10),
                           ("acronym_issues", 10)):
            lst = p.get(key)
            if isinstance(lst, list) and len(lst) > caps.get(key, floor):
                if key == "recurring_phrases":
                    lst.sort(key=lambda r: -(r.get("count") or 0) if isinstance(r, dict) else 0)
                p[key] = lst[:caps[key]]
                steps.append(f"capped {key} at {caps[key]}")
                caps[key] = max(floor, caps[key] // 2)
                shrunk = True
                if len(out()) <= budget:
                    break
        if not shrunk:
            break                       # floor reached: send valid JSON even if over budget
    final = out()
    return final, {"original_chars": len(s), "sent_chars": len(final), "steps": steps}
```

## Edit 2 -- `judge.py`: `_call_native` takes a `fmt` parameter
**Skip if** the signature already has `fmt=`. Two one-line replacements. Find:
```python
def _call_native(native_url, model, messages, think_off, max_tokens, timeout):
```
Replace with:
```python
def _call_native(native_url, model, messages, think_off, max_tokens, timeout, fmt="json"):
```
Find:
```python
        "format": "json",                   # force a well-formed JSON object
```
Replace with:
```python
        "format": fmt,                      # "json" or a full JSON schema (constrained decoding)
```

## Edit 3 -- `judge.py`: real default model (TWO occurrences -- `judge()` and `probe()`)
**Skip if** `qwen3:27b` no longer appears. Replace **both** occurrences of:
```python
os.environ.get("MODEL", "qwen3:27b")
```
with:
```python
os.environ.get("MODEL", "qwen3:30b-a3b")
```

## Edit 4 -- `judge.py`: non-object guard in `_parse_json`
**Skip if** `_parse_json` already mentions `non-object`. Find:
```python
    try:
        return json.loads(t)
    except Exception as e:
        return {"_parse_error": str(e), "_raw": text[:2000]}
```
Replace with:
```python
    try:
        obj = json.loads(t)
    except Exception as e:
        return {"_parse_error": str(e), "_raw": text[:2000]}
    if not isinstance(obj, dict):
        # a top-level array/string would crash result["_meta"] = ... downstream
        return {"_parse_error": "model returned non-object JSON (top-level %s)"
                                % type(obj).__name__, "_raw": text[:2000]}
    return obj
```

## Edit 5 -- `judge.py`: structural shrink replaces the `[:60000]` slice
**Skip if** `judge()` already calls `_shrink_findings`. Find:
```python
    # Send only the deterministic findings, never the document text. Internal keys
    # (anything starting with "_", e.g. _doc_text used for render-time counting) are
    # stripped -- keeps the prompt small and honours "NOT the document itself".
    payload = {k: v for k, v in findings.items() if not str(k).startswith("_")}
    user = "Deterministic scan output:\n" + json.dumps(payload, ensure_ascii=False)[:60000]
```
Replace with:
```python
    # Send only the deterministic findings, never the document text. Internal keys
    # ("_"-prefixed, e.g. _doc_text) are stripped, and oversized findings are shrunk
    # structurally (valid JSON always) instead of char-sliced mid-token.
    user_json, shrunk = _shrink_findings(findings)
    user = "Deterministic scan output:\n" + user_json
```

## Edit 6 -- `judge.py`: transport closure + truncation/repair retries
**Skip if** `judge()` already defines `_transport`. Find:
```python
    t0 = time.time()
    served_native = use_native
    if use_native:
        try:
            content, finish, usage, model_reported, endpoint = _call_native(
                native_url, model, messages, think_off, max_tokens, timeout)
        except requests.RequestException:
            served_native = False           # non-Ollama or native unreachable -> fall back
    if not served_native:
        content, finish, usage, model_reported, endpoint = _call_openai(
            base_url, model, messages, think_off, max_tokens, timeout)

    result = _parse_json(content)
```
Replace with:
```python
    # Constrained decoding: pass the full output schema as Ollama's `format` so the
    # model cannot emit malformed JSON or echo the input keys. PROOFER_SCHEMA=0 reverts
    # to plain "json" mode (e.g. for servers that predate schema support).
    schema_on = os.environ.get("PROOFER_SCHEMA", "1").lower() not in ("0", "false", "no")
    fmt = OUTPUT_SCHEMA if schema_on else "json"

    t0 = time.time()
    served_native = use_native

    def _transport(msgs, mt):
        nonlocal served_native
        if served_native:
            try:
                return _call_native(native_url, model, msgs, think_off, mt, timeout, fmt)
            except (requests.RequestException, ValueError):
                served_native = False       # unreachable OR non-JSON body -> fall back
        return _call_openai(base_url, model, msgs, think_off, mt, timeout)

    retry_on = os.environ.get("PROOFER_RETRY", "1").lower() not in ("0", "false", "no")
    retries = {}
    content, finish, usage, model_reported, endpoint = _transport(messages, max_tokens)
    result = _parse_json(content)

    if retry_on and "_parse_error" in result and finish in ("length", "limit"):
        # Truncated mid-JSON: one retry with double the output budget.
        bigger = min(max_tokens * 2, 16000)
        retries["truncation"] = {"num_predict": [max_tokens, bigger]}
        content2, finish2, usage2, mr2, ep2 = _transport(messages, bigger)
        r2 = _parse_json(content2)
        if "_parse_error" not in r2:
            content, finish, usage, model_reported, endpoint, result = (
                content2, finish2, usage2, mr2, ep2, r2)
            retries["truncation"]["recovered"] = True
    elif retry_on and "_parse_error" in result:
        # Malformed despite a normal finish: one repair round. With the schema
        # constraint on this is nearly unreachable, but it costs one branch.
        repair = messages + [
            {"role": "assistant", "content": content[:6000]},
            {"role": "user", "content": "That reply was not valid JSON for the required "
                                        "schema. Return ONLY the corrected JSON object -- "
                                        "no prose, no fences."}]
        retries["repair"] = {"attempted": True}
        content2, finish2, usage2, mr2, ep2 = _transport(repair, max_tokens)
        r2 = _parse_json(content2)
        if "_parse_error" not in r2:
            content, finish, usage, model_reported, endpoint, result = (
                content2, finish2, usage2, mr2, ep2, r2)
            retries["repair"]["recovered"] = True
```

## Edit 7 (optional) -- `judge.py` + `main.py`: disclose shrink/retries in the report
Makes the provenance line say e.g. *"oversized findings were shrunk to fit the prompt budget,
84,213 -> 59,800 chars (dropped per-item locations; capped recurring_phrases at 60)"* and
*"recovered from a truncated reply by retrying at num_predict 8000 -> 16000"*.

In `judge.py` -- **skip if** `_meta` already includes `schema_constrained`. Find:
```python
        "response_chars": len(content),
        "latency_ms": round((time.time() - t0) * 1000),
    }
```
Replace with:
```python
        "response_chars": len(content),
        "latency_ms": round((time.time() - t0) * 1000),
        "schema_constrained": bool(schema_on and served_native),
    }
    if shrunk:
        result["_meta"]["findings_shrunk"] = shrunk
    if retries:
        result["_meta"]["retries"] = retries
```

In `main.py` -- **skip if** `_provenance` already mentions `findings_shrunk`. Find:
```python
    # clean judgment
    extra = f", {tok} completion tokens" if tok is not None else ""
    extra += f", {lat} ms" if lat is not None else ""
    return f"_Generated by model `{who}`{extra}._"
```
Replace with:
```python
    # clean judgment
    extra = f", {tok} completion tokens" if tok is not None else ""
    extra += f", {lat} ms" if lat is not None else ""
    notes = []
    s = meta.get("findings_shrunk")
    if s:
        notes.append(f"oversized findings were shrunk to fit the prompt budget, "
                     f"{s.get('original_chars', 0):,} -> {s.get('sent_chars', 0):,} chars "
                     f"({'; '.join(s.get('steps') or [])})")
    r = meta.get("retries") or {}
    if r.get("truncation", {}).get("recovered"):
        np = r["truncation"].get("num_predict") or ["?", "?"]
        notes.append(f"recovered from a truncated reply by retrying at num_predict {np[0]} -> {np[-1]}")
    if r.get("repair", {}).get("recovered"):
        notes.append("recovered via one JSON-repair round")
    tail = (" _Note: " + "; ".join(notes) + "._") if notes else ""
    return f"_Generated by model `{who}`{extra}._" + tail
```

## Environment switches (all optional)
`PROOFER_SCHEMA=0` plain-"json" mode | `PROOFER_RETRY=0` no retries | `PROOFER_MAX_TOKENS`
first-attempt output budget (default 8000) | `PROOFER_OLLAMA_NATIVE=0` force `/v1` |
`MODEL` model name.

## Known limits
- Schema-as-`format` needs Ollama >= 0.5 (any build that runs qwen3 qualifies). Non-Ollama
  servers reached via the fallback are unconstrained -- the repair round covers them.
- If even floor-capped lists exceed the budget (pathological), the payload is sent valid but
  over budget rather than corrupted -- `num_ctx` then governs, and it is still disclosed.
- On a retry, `_meta.usage`/`finish_reason` describe the final attempt; `_meta.retries`
  records the path taken.
- `status` is constrained to `existing|proposed`; to add a value later, extend `OUTPUT_SCHEMA`
  and `SYSTEM` together.

## Verify (run from the repo root) -- expect `25 passed, 0 failed`
No Ollama needed: the transport is faked and restored; invented content only.
Save as `verify_judge_hardening.py`:
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
    """Scripted transport: pops the next body per POST, records every call."""
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

# t1: happy path -- schema format, think off, no retries, _doc_text stripped
fr = FakeReq([native_body(GOOD)]); judge.requests = fr
r = judge.judge(findings)
b = fr.calls[0]["body"]
ok("t1 single call", len(fr.calls) == 1)
ok("t1 format is the full schema (dict w/ required keys)",
   isinstance(b["format"], dict) and b["format"].get("additionalProperties") is False
   and set(b["format"]["required"]) == {"acronym_table", "acronym_issues", "misspellings", "customer"})
ok("t1 think off by default", b["think"] is False)
ok("t1 _doc_text never sent", "SECRET BODY" not in json.dumps(b))
ok("t1 default model is qwen3:30b-a3b", b["model"] == "qwen3:30b-a3b")
ok("t1 parsed table present", r["acronym_table"][0]["acronym"] == "WGT")
ok("t1 meta says schema_constrained", r["_meta"]["schema_constrained"] is True)
ok("t1 no retries recorded", "retries" not in r["_meta"])

# t2: truncation -> one retry with doubled num_predict, recovered
fr = FakeReq([native_body('{"acronym_table":[{"acron', "length"), native_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t2 two calls (retry happened)", len(fr.calls) == 2)
ok("t2 num_predict doubled", fr.calls[1]["body"]["options"]["num_predict"]
   == 2 * fr.calls[0]["body"]["options"]["num_predict"])
ok("t2 recovered to a clean parse", "_parse_error" not in r and r["customer"]["primary"] == "ACME")
ok("t2 meta records truncation retry", r["_meta"]["retries"]["truncation"]["recovered"] is True)

# t3: malformed at finish=stop -> repair round, recovered
fr = FakeReq([native_body("Sure! Here you go: not json"), native_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t3 repair round ran", len(fr.calls) == 2)
ok("t3 repair message present", any("not valid JSON" in m.get("content", "")
   for m in fr.calls[1]["body"]["messages"]))
ok("t3 recovered", "_parse_error" not in r)
ok("t3 meta records repair", r["_meta"]["retries"]["repair"]["recovered"] is True)

# t4: top-level array twice -> guarded _parse_error, _meta attaches, no crash
fr = FakeReq([native_body('["a","b"]'), native_body('["a","b"]')])
judge.requests = fr
r = judge.judge(findings)
ok("t4 non-object guarded (no TypeError)", "_parse_error" in r and "non-object" in r["_parse_error"])
ok("t4 _meta still attached", "_meta" in r)

# t5: native returns non-JSON body -> ValueError -> falls back to /v1
fr = FakeReq([None, openai_body(GOOD)])
judge.requests = fr
r = judge.judge(findings)
ok("t5 fell back to OpenAI endpoint", fr.calls[1]["url"].endswith("/chat/completions"))
ok("t5 fallback parsed fine", r["customer"]["primary"] == "ACME")

# t6: oversized findings -> structural shrink, valid JSON, disclosed
big = {"recurring_phrases": [{"phrase": f"Widget Phrase {i}", "count": i,
                              "locations": [f"body/p[{j}]" for j in range(40)]}
                             for i in range(300)],
       "existing_acronyms": {"WGT": ["Widget Group Tracker"]},
       "acronym_issues": [], "spelling_candidates": [],
       "_doc_text": "X" * 100}
fr = FakeReq([native_body(GOOD)]); judge.requests = fr
r = judge.judge(big)
sent = fr.calls[0]["body"]["messages"][-1]["content"]
sent_json = sent.split("Deterministic scan output:\n", 1)[1].rsplit("\n\n/no_think", 1)[0]
parsed = json.loads(sent_json)            # raises if the payload were corrupted
ok("t6 payload is VALID json after shrink", isinstance(parsed, dict))
ok("t6 under/near budget", len(sent_json) <= 60000)
ok("t6 existing_acronyms preserved", parsed["existing_acronyms"]["WGT"] == ["Widget Group Tracker"])
ok("t6 shrink disclosed in meta", r["_meta"]["findings_shrunk"]["original_chars"] > 60000
   and len(r["_meta"]["findings_shrunk"]["steps"]) >= 1)

# t7: PROOFER_SCHEMA=0 -> plain "json" format
os.environ["PROOFER_SCHEMA"] = "0"
fr = FakeReq([native_body(GOOD)]); judge.requests = fr
r = judge.judge(findings)
ok("t7 schema opt-out -> format 'json'", fr.calls[0]["body"]["format"] == "json")
os.environ.pop("PROOFER_SCHEMA", None)

judge.requests = _real_requests
print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
```

## Rollback
`git checkout -- judge.py main.py`.
