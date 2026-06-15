import importlib, sys, judge, checks, requests
importlib.reload(checks); importlib.reload(judge)
P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)
g = lambda s: tuple(s.split())

# --- _is_spec_token units: mixed alphanumerics + lone letters are noise; a bare
#     integer is NOT (so legit terms with a number survive). [review item #2] ---
ok("spec: mixed alnum 50V", checks._is_spec_token("50V") is True)
ok("spec: part number ST2-611-A1", checks._is_spec_token("ST2-611-A1") is True)
ok("spec: L3 mixed", checks._is_spec_token("L3") is True)
ok("spec: lone letter V", checks._is_spec_token("V") is True)
ok("spec: bare integer 3 is NOT spec", checks._is_spec_token("3") is False)
ok("spec: ordinary word not spec", checks._is_spec_token("Maintenance") is False)

# --- _candidate_gram noise filter ---
ok("keep 'Corrective Action'", checks._candidate_gram(g("Corrective Action")) is True)
ok("keep 'Failure Mode'", checks._candidate_gram(g("Failure Mode")) is True)
ok("keep 'STORM II'", checks._candidate_gram(g("STORM II")) is True)
ok("keep 'Level 3 Maintenance' (bare number)", checks._candidate_gram(g("Level 3 Maintenance")) is True)
ok("keep 'Block 2 Upgrade' (bare number)", checks._candidate_gram(g("Block 2 Upgrade")) is True)
# known limit: a lone single-letter designator ('A') still trips the column-header guard,
# so 'Class A Mishap' is dropped. Documented, not asserted-as-kept.
ok("drop 'Class A Mishap' (lone letter 'A' -- known limit)",
   checks._candidate_gram(g("Class A Mishap")) is False)
ok("drop part-number gram", checks._candidate_gram(g("ST2-611-A1 Voltage V 50V 60 68")) is False)
ok("drop 'Voltage V' (lone letter)", checks._candidate_gram(g("Voltage V")) is False)
ok("drop 'L3 Insight' (mixed alnum)", checks._candidate_gram(g("L3 Insight")) is False)
ok("drop mfr part gram", checks._candidate_gram(g("CAP 10uF 50V 12105C106MAT2A ST2-611-A1 Voltage")) is False)

# --- judge() non-dict guard: a top-level JSON array must not crash; it degrades to a
#     parse-error dict with _meta still attached. [review item #1] ---
orig_post = requests.post
class Resp:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload
def fake_post(url, json=None, timeout=None):
    # native /api/chat shape, but the model emitted a JSON *array* as its content
    return Resp({"model": "qwen3:30b-a3b", "message": {"content": "[1, 2, 3]", "thinking": ""},
                 "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 4})
requests.post = fake_post
try:
    res = judge.judge({"recurring_phrases": [], "_doc_text": "x"})
    ok("non-object JSON -> dict (no crash)", isinstance(res, dict))
    ok("non-object JSON -> _parse_error set", "_parse_error" in res)
    ok("non-object JSON -> _meta still attached", isinstance(res.get("_meta"), dict))
finally:
    requests.post = orig_post

print(f"\n=== {len(P)} passed, {len(F)} failed ===")
sys.exit(1 if F else 0)
