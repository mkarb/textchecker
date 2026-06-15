import importlib, sys, re
import extract
importlib.reload(extract)
from extract import Doc, Block, apply_reference_acronyms, load_reference_acronyms
P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

ref = load_reference_acronyms()
ok("reference CSV present & non-empty", len(ref) >= 50)
ok("missing path -> {}", load_reference_acronyms("/no/such/file.csv") == {})
ok("org acronym present (ALE)", ref.get("ALE") == "Acquisition Logistics Engineering")

# a document that USES house acronyms but defines none of them
doc = Doc(blocks=[Block("The ALE team submitted the CDRL to ANSI per the ATP and CDR schedule.", "p[0]", "body")])
n = apply_reference_acronyms(doc)
ok("merged the house acronyms used in the doc", n >= 4)
for a in ("ALE", "CDRL", "ANSI", "CDR"):
    ok(f"recognized from house list: {a}", a in doc.glossary_defs)
ok("expansion matches the reference", doc.glossary_defs.get("CDRL") == ref.get("CDRL"))
ok("unused house acronym (AGS) NOT injected", "AGS" not in doc.glossary_defs)

# the document's own definition always wins over the house list
doc2 = Doc(blocks=[Block("ATP is used throughout this plan.", "p[0]", "body")])
doc2.glossary_defs["ATP"] = "Advanced Test Plan"
apply_reference_acronyms(doc2)
ok("document definition wins over house list", doc2.glossary_defs["ATP"] == "Advanced Test Plan")

# absent reference file -> no-op (pipeline still works)
empty = Doc(blocks=[Block("ALE and CDRL appear here.", "p[0]", "body")])
ok("empty reference -> nothing merged", apply_reference_acronyms(empty, ref={}) == 0)

print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
