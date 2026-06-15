import importlib, sys, main, checks
importlib.reload(checks); importlib.reload(main)
P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

base = {"recurring_phrases": [{"phrase": "Cascade Module", "count": 14, "proposed": "CM", "locations": []},
                              {"phrase": "III Loss", "count": 21, "proposed": "IL", "locations": []}],
        "existing_acronyms": {"CA": ["Corrective Action"],
                              "FMECA": ["Failure Modes, Effects, and Criticality Analysis"],
                              "LRU": ["Line Replaceable Unit"]},
        "acronym_issues": [], "spelling_candidates": [],
        "page": {"issues": [], "toc_entries": [], "total_pages_detected": None, "note": "n"},
        "organizations": {"clusters": {"pole zero": {"surfaces": {"Pole Zero Corporation": 3}}}, "issues": []},
        "_doc_text": "FMECA Corrective Action Cascade Module Line Replaceable Unit " * 4}

# 1) model ECHOED the input (clean dict, no acronym_table) -> deterministic table built
echo = {"existing_acronyms": base["existing_acronyms"], "recurring_phrases": base["recurring_phrases"],
        "_meta": {"model_reported": "qwen3:30b-a3b", "usage": {"completion_tokens": 3322}, "latency_ms": 26073}}
md = main.render_md(base, echo)
ok("echo: table built (not 'Recurring long phrases')", "## Acronym table" in md and "Recurring long phrases" not in md)
ok("echo: FMECA existing (verbatim commas)", "| FMECA | Failure Modes, Effects, and Criticality Analysis | existing |" in md)
ok("echo: CA existing", "| CA | Corrective Action | existing |" in md)
ok("echo: Cascade Module -> proposed CM", "| CM | Cascade Module | proposed |" in md)
ok("echo: existing acronym not duplicated", md.count("| CA |") == 1)
ok("echo: deterministic customer surfaced", "Pole Zero Corporation" in md)

# 2) proper model table still WINS (no fallback)
good = {"acronym_table": [{"acronym": "XYZ", "expansion": "X Y Z", "status": "existing", "note": ""}],
        "customer": {"primary": "Boeing"},
        "_meta": {"model_reported": "m", "usage": {"completion_tokens": 1}, "latency_ms": 1}}
md2 = main.render_md(base, good)
ok("model table wins", "| XYZ | X Y Z | existing |" in md2)
ok("model customer wins", "Primary: **Boeing**" in md2)

# 3) no model at all -> deterministic table
md3 = main.render_md(base, None)
ok("no-model: table built", "## Acronym table" in md3 and "| FMECA |" in md3)

print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
