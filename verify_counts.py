import importlib, sys, checks, main, extract
importlib.reload(checks); importlib.reload(main)
P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

d = extract.load("sample_manual.xml"); t = d.text
ok("acr LRU == 1", checks.count_acronym(t, "LRU") == 1)
ok("acr RCM == 2", checks.count_acronym(t, "RCM") == 2)
ok("phrase 'Line Replaceable Unit' == 4", checks.count_phrase(t, "Line Replaceable Unit") == 4)
ok("phrase 'Built-In Test Equipment' == 3 (hyphen)", checks.count_phrase(t, "Built-In Test Equipment") == 3)
ok("phrase case-insensitive", checks.count_phrase(t, "line REPLACEABLE unit") == 4)
ok("no false partial", checks.count_acronym("RCMX aRCM", "RCM") == 0)

f = main.gather(d, "Acme Defense Systems", 3)
ok("gather stashes _doc_text", isinstance(f.get("_doc_text"), str))
mr = {"acronym_table": [
        {"acronym": "LRU", "expansion": "Line Replaceable Unit", "status": "existing", "note": ""},
        {"acronym": "RCM", "expansion": "Reliability Centered Maintenance", "status": "existing", "note": ""},
        {"acronym": "BITE", "expansion": "Built-In Test Equipment", "status": "proposed", "note": ""}],
      "_meta": {"model_reported": "fake", "usage": {"completion_tokens": 1}, "latency_ms": 1}}
md = main.render_md(f, mr)
ok("count columns present", "Acronym uses" in md and "Expansion uses" in md)
ok("LRU 1/4", "| LRU | Line Replaceable Unit | existing | 1 | 4 |" in md)
ok("RCM 2/1", "| RCM | Reliability Centered Maintenance | existing | 2 | 1 |" in md)
ok("BITE 0/3", "| BITE | Built-In Test Equipment | proposed | 0 | 3 |" in md)
md2 = main.render_md({k: v for k, v in f.items() if k != "_doc_text"}, mr)
ok("backward-compatible fallback", "Acronym uses" not in md2)
print(f"\n=== {len(P)} passed, {len(F)} failed ==="); sys.exit(1 if F else 0)
