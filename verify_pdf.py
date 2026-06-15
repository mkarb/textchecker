import importlib, sys, os, tempfile
import extract, checks
importlib.reload(extract); importlib.reload(checks)
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
                                Table, TableStyle, Spacer, PageBreak)

PATH = os.path.join(tempfile.gettempdir(), "verify_storm.pdf")
styles = getSampleStyleSheet()
body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=13)
head = ParagraphStyle("head", parent=styles["Heading1"], fontSize=16, leading=20)

def on_page(canvas, docu):
    canvas.saveState(); canvas.setFont("Helvetica", 9)
    canvas.drawCentredString(letter[0]/2, letter[1]-0.5*inch, "STORM II Reliability Program Plan")
    canvas.drawCentredString(letter[0]/2, 0.4*inch, "Page %d of 4" % canvas.getPageNumber())
    canvas.restoreState()

frame = Frame(inch, inch, letter[0]-2*inch, letter[1]-2*inch, id="f")
doc = BaseDocTemplate(PATH, pagesize=letter,
                      pageTemplates=[PageTemplate(id="t", frames=[frame], onPage=on_page)])
gloss = [["Acronym", "Definition"], ["CA", "Corrective Action"],
         ["FMECA", "Failure Modes, Effects, and Criticality Analysis"],
         ["PS", "Performance Specification"], ["DR", "Design Review"],
         ["CI", "Configuration Item"], ["STORM", "Small Tactical Optical Rifle Mounted"],
         ["SD-18", "NAVSEA Standard Drawing 18"]]
gt = Table(gloss, colWidths=[1.1*inch, 4.3*inch])
gt.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.black),
                        ("FONTSIZE", (0,0), (-1,-1), 10)]))
story = [Paragraph("Acronyms and Abbreviations", head), gt, Spacer(1, 14),
         Paragraph("1. Scope", head),
         Paragraph("The reliability program defines the Correc-<br/>tive action process "
                   "used throughout the STORM II system.", body),
         Paragraph("Every Line Replaceable<br/>Unit is analyzed for failure modes.", body),
         Paragraph("FMECA analysis was performed on all replaceable units.", body), PageBreak()]
for pg in range(2, 5):
    story.append(Paragraph("%d. Section %d" % (pg, pg), head))
    for k in range(3):
        story.append(Paragraph("Filler text page %d para %d on the Corrective Action "
                               "Process and Performance Specification." % (pg, k), body))
    if pg < 4: story.append(PageBreak())
doc.build(story)

P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

d = extract.load(PATH); g = d.glossary_defs; txt = d.text
ok("produced text blocks", sum(1 for b in d.blocks if b.kind != "page") > 0)
ok("PDF glossary CA", g.get("CA") == "Corrective Action")
ok("PDF glossary FMECA (commas verbatim)",
   g.get("FMECA") == "Failure Modes, Effects, and Criticality Analysis")
ok("PDF glossary SD-18", g.get("SD-18") == "NAVSEA Standard Drawing 18")
ok("PDF glossary STORM", g.get("STORM") == "Small Tactical Optical Rifle Mounted")
ok("header row skipped", "Acronym" not in g)
ok("de-hyphenated 'Corrective action'", "Corrective action" in txt)
ok("no 'Correc-' fragment", "Correc-" not in txt and "Correc tive" not in txt)
ok("wrapped 'Line Replaceable Unit' joined", any("Line Replaceable Unit" in b.text for b in d.blocks))
ok("running header stripped",
   not any(b.kind not in ("page", "table") and "Reliability Program Plan" in b.text for b in d.blocks))
pf, toc, total = checks.page_checks(d); probs = {x.problem for x in pf}
ok("page total == 4", total == 4)
ok("no duplicate_page_number", "duplicate_page_number" not in probs)
ok("no spurious page_gap", "page_gap" not in probs)
ok(">=3 headings detected", sum(1 for b in d.blocks if b.kind == "heading") >= 3)
_, defs = checks.acronym_consistency(d)
ok("FMECA -> existing_acronyms", "Failure Modes, Effects, and Criticality Analysis" in defs.get("FMECA", []))
ok("xml still loads (regression)", len(extract.load("sample_manual.xml").blocks) == 14)

print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
