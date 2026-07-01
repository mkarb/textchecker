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
            "prompt_eval_count": 100, "eval_count": 50, "model": "ornith:latest"}

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
ok("t1 default model is ornith:latest", b["model"] == "ornith:latest")
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
