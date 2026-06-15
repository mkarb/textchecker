import sys
from pathlib import Path
P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

import server
ok(".pdf in server.ALLOWED", ".pdf" in server.ALLOWED)

s = Path("server.py").read_text(encoding="utf-8")
ok("server rejection message updated", "accepted" in s and ".pdf" in s.split("accepted")[0].rsplit("Only", 1)[-1])

hp = Path("static/index.html") if Path("static/index.html").exists() else Path("index.html")
h = hp.read_text(encoding="utf-8")
ok("file input accepts .pdf", 'accept=".docx,.pdf,.xml"' in h)
ok("help copy mentions .pdf", h.count(".pdf") >= 2)

print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
