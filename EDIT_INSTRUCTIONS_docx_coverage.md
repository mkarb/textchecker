# Claude Code edit instructions -- docx coverage: footers/headers, footnotes, text boxes, nested tables

**Target:** `extract.py` only (3 edits). Idempotent. No new dependencies, no other files.

## What this fixes
`from_docx` walks only the document **body**, so four whole content classes were invisible:
1. **Headers/footers** -- separate per-section parts. This is where `Page X of Y` lives, so the
   entire page-number subsystem (totals, `see page N` range checks, TOC ranges) was dead for
   docx input, the most common format.
2. **Footnotes/endnotes** -- separate package parts; typos there were unscannable.
3. **Text boxes** -- caution decals/callouts inside shapes (`w:txbxContent`).
4. **Nested tables** -- a table inside a table cell vanished (`cell.text` doesn't include it).

## Two subtleties the naive fix gets wrong
- **Fields, not text.** `Page X of Y` is `{PAGE} of {NUMPAGES}` field code. python-docx's
  `.text` drops `w:fldSimple` cached results entirely (verified on 1.2.0: a footer reading
  "Page 3 of 42" comes back `"Page 3 of "`). Extraction must read `w:t` at the XML level --
  which also correctly *excludes* `w:instrText` instructions.
- **Footers are per-SECTION templates, not per-page instances.** One footer renders on every
  page of its section. Emitting its cached "Page 9 of 42" as a normal block would feed the
  page-sequence checks a fake marker -- two sections and you've manufactured a `page_gap`.
  So: harvest the **total** (Y), strip the marker from the emitted footer text, and append a
  single synthetic `Page 1 of <total>` block -- sequence checks stay quiet, `XREF`/TOC range
  checks light up. `checks.py` needs zero changes.

Also handled: linked sections (`is_linked_to_previous`) are skipped, so inherited footers don't
duplicate; `mc:Fallback` copies of text boxes are skipped (Word stores each box twice);
separator footnotes are skipped; merged cells don't double-walk a nested table.

## Edit 1 -- insert the helpers (idempotent)
**Skip if** `extract.py` already defines `_docx_hf_blocks`. Insert immediately **before** the
line `def from_docx(path) -> Doc:`:
```python
# ------------------------------------------------- docx coverage: sections & parts
# python-docx's body walk misses four whole classes of content:
#   headers/footers (separate section parts -- where "Page X of Y" lives),
#   footnotes/endnotes (separate package parts), text boxes (inside drawings),
#   and tables nested in table cells. The helpers below recover all four.
_WNS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PAGE_OF_RE = re.compile(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", re.I)


def _w_text(el):
    """All literal text under el: every w:t, which includes cached field results
    (even inside w:fldSimple, which python-docx's .text silently drops) and never
    includes w:instrText field instructions."""
    return _ws("".join(t.text or "" for t in el.iter(_WNS + "t")))


def _docx_hf_blocks(d):
    """Header/footer blocks + the page total. Footers are per-SECTION templates --
    one footer renders on every page of its section -- so a 'Page X of Y' found here
    must NOT be emitted as a page-sequence marker (two sections' cached values would
    fake a page_gap). Instead: harvest Y into the total, strip the marker from the
    emitted text, and let the caller synthesize a single pagination block."""
    blocks, totals, seen = [], set(), set()
    for si, sec in enumerate(d.sections):
        for name in ("header", "footer", "first_page_header", "first_page_footer",
                     "even_page_header", "even_page_footer"):
            hf = getattr(sec, name, None)
            if hf is None or hf.is_linked_to_previous:
                continue                       # linked = inherits previous section's part
            pid = id(hf._element)
            if pid in seen:
                continue
            seen.add(pid)
            short = "header" if "header" in name else "footer"
            items = [(p._p, f"section[{si}]/{short}/p[{j}]")
                     for j, p in enumerate(hf.paragraphs)]
            for ti, tbl in enumerate(getattr(hf, "tables", []) or []):
                for ri, row in enumerate(tbl.rows):
                    items.append((row._tr, f"section[{si}]/{short}/tbl[{ti}]/tr[{ri}]"))
            for el, path in items:
                txt = _w_text(el)
                if not txt:
                    continue
                for m in _PAGE_OF_RE.finditer(txt):
                    totals.add(int(m.group(2)))
                residual = _ws(_PAGE_OF_RE.sub(" ", txt))
                if residual:
                    blocks.append(Block(residual, path, short))
    return blocks, (max(totals) if totals else None)


def _docx_note_blocks(d):
    """Footnotes/endnotes from their package parts (python-docx has no API for them)."""
    blocks = []
    for reltail, tag in (("/footnotes", "footnote"), ("/endnotes", "endnote")):
        part = None
        for rel in d.part.rels.values():
            if rel.reltype.endswith(reltail):
                part = rel.target_part
                break
        if part is None:
            continue
        try:
            root = etree.fromstring(part.blob)
        except Exception:
            continue
        for note in root.iter(_WNS + tag):
            if note.get(_WNS + "type") in ("separator", "continuationSeparator"):
                continue
            txt = _w_text(note)
            if txt:
                blocks.append(Block(txt, f"{tag}s/{tag}[{note.get(_WNS + 'id')}]", tag))
    return blocks


def _docx_textbox_blocks(d):
    """Text inside shapes/text boxes (w:txbxContent). Word writes each box twice --
    mc:Choice (modern) and mc:Fallback (legacy VML) -- so Fallback copies are skipped
    to avoid double extraction."""
    blocks, n = [], 0
    for tx in d.element.body.iter(_WNS + "txbxContent"):
        anc, skip = tx.getparent(), False
        while anc is not None:
            if etree.QName(anc).localname == "Fallback":
                skip = True
                break
            anc = anc.getparent()
        if skip:
            continue
        txt = _w_text(tx)
        if txt:
            blocks.append(Block(txt, f"body/textbox[{n}]", "textbox"))
            n += 1
    return blocks


def _walk_docx_table(table, base, blocks):
    """Emit a table's rows as blocks and recurse into tables nested in cells
    (cell.text does not include nested-table text, so without this they vanish)."""
    for r, row in enumerate(table.rows):
        cells = [_ws(c.text) for c in row.cells]
        txt = " | ".join(c for c in cells if c)
        if txt:
            blocks.append(Block(txt, f"{base}/tr[{r}]", "table"))
        seen_nested = set()
        for c in row.cells:
            for k, nested in enumerate(c.tables):
                nid = id(nested._tbl)
                if nid in seen_nested:        # merged cells repeat the same object
                    continue
                seen_nested.add(nid)
                _walk_docx_table(nested, f"{base}/tr[{r}]/tbl[{k}]", blocks)
```

## Edit 2 -- recursive table walk
**Skip if** the `tbl` branch already calls `_walk_docx_table`. Find:
```python
        elif tag == "tbl":
            table = Table(child, d)
            for r, row in enumerate(table.rows):
                cells = [_ws(c.text) for c in row.cells]
                txt = " | ".join(c for c in cells if c)
                if txt:
                    doc.blocks.append(Block(txt, f"body/tbl[{idx}]/tr[{r}]", "table"))
            idx += 1
```
Replace with:
```python
        elif tag == "tbl":
            _walk_docx_table(Table(child, d), f"body/tbl[{idx}]", doc.blocks)
            idx += 1
```

## Edit 3 -- wire the new sources into from_docx's tail
**Skip if** the tail already calls `_docx_textbox_blocks`. Find (the from_docx tail -- the
from_xml tail looks similar but lacks the `idx += 1` line):
```python
            idx += 1
    harvest_glossary(doc)
    return doc
```
Replace with:
```python
            idx += 1
    doc.blocks.extend(_docx_textbox_blocks(d))
    doc.blocks.extend(_docx_note_blocks(d))
    hf_blocks, page_total = _docx_hf_blocks(d)
    doc.blocks.extend(hf_blocks)
    if page_total:
        # one synthetic marker: "pagination exists, total = N". A single block keeps
        # the page-sequence checks quiet while lighting up the XREF/TOC range checks.
        doc.blocks.append(Block(f"Page 1 of {page_total}", "sections/pagination", "page"))
    harvest_glossary(doc)
    return doc
```

## Known limits
- A footer whose PAGE/NUMPAGES fields have **no cached values** (programmatically generated,
  never opened in Word) yields no total -- nothing to harvest. Word-saved documents always
  carry caches.
- The cached NUMPAGES is the total **as of last save**; a stale cache gives a stale total.
  That is also exactly what the document's reader sees, so flagging against it is correct.
- Comments and tracked-changes text remain out of scope (separate parts again) -- say the word.

## Verify (run from the repo root) -- expect `15 passed, 0 failed`
Builds all fixtures itself (invented content only). Save as `verify_docx_coverage.py`:
```python
import importlib, sys, zipfile, shutil
import extract, checks
importlib.reload(extract); importlib.reload(checks)
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

P, F = [], []
def ok(n, c): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n)

def field(p, instr, cached):
    r = OxmlElement('w:r'); fc = OxmlElement('w:fldChar'); fc.set(qn('w:fldCharType'),'begin'); r.append(fc); p._p.append(r)
    r = OxmlElement('w:r'); it = OxmlElement('w:instrText'); it.set(qn('xml:space'),'preserve'); it.text = f" {instr} "; r.append(it); p._p.append(r)
    r = OxmlElement('w:r'); fc = OxmlElement('w:fldChar'); fc.set(qn('w:fldCharType'),'separate'); r.append(fc); p._p.append(r)
    if cached is not None:
        r = OxmlElement('w:r'); t = OxmlElement('w:t'); t.text = str(cached); r.append(t); p._p.append(r)
    r = OxmlElement('w:r'); fc = OxmlElement('w:fldChar'); fc.set(qn('w:fldCharType'),'end'); r.append(fc); p._p.append(r)

def page_field_para(p):
    p.add_run("Page ")
    field(p, "PAGE", 3)
    r = OxmlElement('w:r'); t = OxmlElement('w:t'); t.set(qn('xml:space'),'preserve'); t.text = " of "; r.append(t); p._p.append(r)
    fs = OxmlElement('w:fldSimple'); fs.set(qn('w:instr'), " NUMPAGES ")
    rr = OxmlElement('w:r'); tt = OxmlElement('w:t'); tt.text = "42"; rr.append(tt); fs.append(rr); p._p.append(fs)

# ---------- 0) the python-docx deficiency, on a standalone doc ----------
d0 = Document(); page_field_para(d0.sections[0].footer.paragraphs[0])
ok("python-docx .text drops fldSimple cached text", d0.sections[0].footer.paragraphs[0].text == "Page 3 of ")
ok("_w_text recovers full field text", extract._w_text(d0.sections[0].footer.paragraphs[0]._p) == "Page 3 of 42")

# ---------- 1) sections fixture: add sections FIRST, then write footers ----------
d = Document()
d.add_paragraph("The widget assembly is described in this manual.")
d.add_paragraph("Details are listed in the appendix, see page 99 for the full matrix.")
d.add_paragraph("Torque values are given per page 7 of this manual.")
d.add_section(); d.add_section()
s0, s1, s2 = d.sections[0], d.sections[1], d.sections[2]
page_field_para(s0.footer.paragraphs[0])                      # field-based Page X of Y
s1.footer.is_linked_to_previous = False
s1.footer.paragraphs[0].text = "ACME Proprietary Page 9 of 42" # different cached page
# s2 stays linked -> must not duplicate
d.save("/tmp/cov_sections.docx")

doc = extract.load("/tmp/cov_sections.docx")
pages = [b for b in doc.blocks if b.kind == "page"]
footers = [b for b in doc.blocks if b.kind == "footer"]
ok("ONE synthetic pagination block", len(pages) == 1 and pages[0].text == "Page 1 of 42")
ok("footer residual kept, page marker stripped",
   any("ACME Proprietary" in b.text and "Page 9" not in b.text for b in footers))
ok("linked section adds no duplicate footer", sum("ACME" in b.text for b in footers) == 1)

findings, toc, total = checks.page_checks(doc)
probs = [x.problem for x in findings]
ok("page total detected = 42", total == 42)
ok("no fake page_gap/out_of_order/duplicate from section footers",
   not any(p in probs for p in ("page_gap", "out_of_order_page", "duplicate_page_number")))
ok("xref out-of-range fires (page 99 > 42)",
   any(x.problem == "xref_out_of_range" and "99" in x.detail for x in findings))
ok("in-range xref (page 7) not flagged", not any("page 7 " in x.detail for x in findings))

# ---------- 2) footnotes via package surgery ----------
FOOT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
 '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
 '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
 '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
 '<w:footnote w:id="2"><w:p><w:r><w:t>Recieve inspection occurs at the dock.</w:t></w:r></w:p></w:footnote>'
 '</w:footnotes>')
with zipfile.ZipFile("/tmp/cov_sections.docx") as zin, \
     zipfile.ZipFile("/tmp/cov_full.docx", "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.namelist():
        data = zin.read(item)
        if item == "[Content_Types].xml":
            data = data.replace(b"</Types>",
                b'<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
        if item == "word/_rels/document.xml.rels":
            data = data.replace(b"</Relationships>",
                b'<Relationship Id="rIdFn9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>')
        zout.writestr(item, data)
    zout.writestr("word/footnotes.xml", FOOT)
doc2 = extract.load("/tmp/cov_full.docx")
fns = [b for b in doc2.blocks if b.kind == "footnote"]
ok("footnote extracted (separators skipped)", len(fns) == 1 and "Recieve inspection" in fns[0].text)
ok("footnote text reaches doc.text", "Recieve" in doc2.text)

# ---------- 3) textbox: Choice + Fallback, extracted once ----------
d3 = Document()
d3.add_paragraph("Body paragraph before the shape.")
NS = ('xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:v="urn:schemas-microsoft-com:vml"')
AC = (f'<mc:AlternateContent {NS}>'
      '<mc:Choice Requires="wps"><wps:txbx><w:txbxContent>'
      '<w:p><w:r><w:t>Caution decal: do not exceed 42 PSI</w:t></w:r></w:p>'
      '</w:txbxContent></wps:txbx></mc:Choice>'
      '<mc:Fallback><v:textbox><w:txbxContent>'
      '<w:p><w:r><w:t>Caution decal: do not exceed 42 PSI</w:t></w:r></w:p>'
      '</w:txbxContent></v:textbox></mc:Fallback>'
      '</mc:AlternateContent>')
run = d3.add_paragraph().add_run()
run._r.append(etree.fromstring(AC))
d3.save("/tmp/cov_txbx.docx")
doc3 = extract.load("/tmp/cov_txbx.docx")
boxes = [b for b in doc3.blocks if b.kind == "textbox"]
ok("textbox extracted exactly once (Fallback skipped)", len(boxes) == 1 and "Caution decal" in boxes[0].text)
ok("no duplicate in doc.text", doc3.text.count("Caution decal") == 1)

# ---------- 4) nested table ----------
d4 = Document()
t = d4.add_table(rows=1, cols=2)
t.rows[0].cells[0].text = "Outer cell A"
nested = t.rows[0].cells[1].add_table(rows=1, cols=2)
nested.rows[0].cells[0].text = "NestedTerm"
nested.rows[0].cells[1].text = "Nested Definition"
d4.save("/tmp/cov_nested.docx")
doc4 = extract.load("/tmp/cov_nested.docx")
ok("nested table row emitted with nested path",
   any("NestedTerm | Nested Definition" in b.text and b.path.count("tbl") == 2 for b in doc4.blocks))
ok("nested text not duplicated", doc4.text.count("NestedTerm") == 1)

print("\n=== %d passed, %d failed ===" % (len(P), len(F)))
sys.exit(1 if F else 0)
```

## Rollback
`git checkout -- extract.py`.
