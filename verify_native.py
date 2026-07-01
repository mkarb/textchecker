import importlib, os, sys, judge, requests
importlib.reload(judge)
P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

orig_post = requests.post
NATIVE = {"status": 200, "content": '{"acronym_table":[],"acronym_issues":[],"misspellings":[],"customer":{"primary":"X"}}', "done": "stop"}
# complete shape (all four required keys) so _validate_judgment passes and no repair round fires
OPENAI = {"content": '{"acronym_table":[{"acronym":"AB","expansion":"A B","status":"existing","note":""}],"acronym_issues":[],"misspellings":[],"customer":{"primary":"Y"}}', "finish": "stop"}
calls = []
class Resp:
    def __init__(self, p, s=200): self.p = p; self.s = s
    def raise_for_status(self):
        if self.s >= 400: raise requests.HTTPError(str(self.s))
    def json(self): return self.p
def fake_post(url, json=None, timeout=None):
    calls.append((url, json))
    if url.endswith("/api/chat"):
        if NATIVE["status"] >= 400: return Resp({}, NATIVE["status"])
        return Resp({"model": "qwen3:30b-a3b", "message": {"content": NATIVE["content"], "thinking": ""},
                     "done_reason": NATIVE["done"], "prompt_eval_count": 893, "eval_count": 2100})
    return Resp({"model": "qwen3:30b-a3b", "usage": {"prompt_tokens": 893, "completion_tokens": 2100, "total_tokens": 2993},
                 "choices": [{"message": {"content": OPENAI["content"]}, "finish_reason": OPENAI["finish"]}]})
requests.post = fake_post
findings = {"recurring_phrases": [{"phrase": "Corrective Action", "count": 14, "proposed": "CA", "locations": []}],
            "_doc_text": "SENTINELDOC must never be sent"}
try:
    os.environ.pop("PROOFER_OLLAMA_NATIVE", None); calls.clear(); NATIVE["status"] = 200; NATIVE["done"] = "stop"
    res = judge.judge(findings); url, body = calls[-1]
    ok("native /api/chat", url.endswith("/api/chat"))
    ok("think:false", body.get("think") is False)
    ok("format is the constrained schema", body.get("format") == judge.OUTPUT_SCHEMA)
    ok("num_predict bounded", body["options"]["num_predict"] == 8000)
    ok("_doc_text stripped", "SENTINELDOC" not in body["messages"][1]["content"])
    ok("endpoint native", res["_meta"]["base_url"].endswith("/api/chat"))
    ok("usage mapped", res["_meta"]["usage"]["completion_tokens"] == 2100 and res["_meta"]["usage"]["prompt_tokens"] == 893)
    ok("parsed clean", "_parse_error" not in res)
    calls.clear(); NATIVE["status"] = 404
    res2 = judge.judge(findings)
    ok("auto-fallback to openai", calls[0][0].endswith("/api/chat") and calls[-1][0].endswith("/chat/completions"))
    ok("endpoint openai on fallback", res2["_meta"]["base_url"].endswith("/chat/completions"))
    os.environ["PROOFER_OLLAMA_NATIVE"] = "0"; calls.clear()
    res3 = judge.judge(findings)
    ok("forced openai single call", len(calls) == 1 and calls[0][0].endswith("/chat/completions"))
    ok("chat_template_kwargs set", calls[0][1].get("chat_template_kwargs") == {"enable_thinking": False})
    os.environ.pop("PROOFER_OLLAMA_NATIVE", None)
    calls.clear(); NATIVE["status"] = 200; NATIVE["done"] = "length"; NATIVE["content"] = '{"acronym_table":[{"acro'
    res4 = judge.judge(findings)
    ok("truncation reported", "truncated" in res4.get("_parse_error", "").lower() and "length" in res4.get("_parse_error", ""))
finally:
    requests.post = orig_post
print(f"\n=== {len(P)} passed, {len(F)} failed ==="); sys.exit(1 if F else 0)
