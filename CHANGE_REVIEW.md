# Change Review — local manual proofreader

**For:** Claude Code. Point at the repo root (the folder containing `server.py`).
**Goal:** Reconcile the working tree with the reviewed state below. Some items were
already hand-patched locally — verify and complete them, don't duplicate.

---

## Project context
A local-LLM document proofreader. `extract.py` normalizes `.docx`/`.xml` into a
block model; `checks.py` runs deterministic passes (recurring-phrase→acronym,
acronym consistency, spelling, page numbers, customer name); `judge.py` sends the
findings to a local OpenAI-compatible model (Qwen via Ollama) for judgment;
`main.py` is the CLI; `server.py` + `static/` are the FastAPI web service. Pattern
throughout: **deterministic recall + model precision** — never make the model scan
the whole doc.

**Status:** running on Windows. The LLM pass is optional and falls back gracefully
when no model is reachable at `OPENAI_BASE_URL` (default `http://localhost:11434/v1`).

---

## Already hand-patched locally (verify, then finish)
- `encoding="utf-8"` was added by hand to several `server.py` reads/writes. Keep
  UTF-8 on **all** file I/O; the complete set is listed under change (2) — make sure
  none were missed (`main.py` writes were not in the local patch).
- A `static/` dir was created with copies of `index.html`/`references.md`. After
  change (2), duplicates are unnecessary: **keep exactly one copy of each (either the
  repo root or `static/`), delete the rest. Do not symlink.**
- `PROOFER_DATA` was set via env. Change (2) makes the default sane, so the env
  override becomes optional.

---

## (1) `extract.py` — XML walker rewrite  *(primary change)*

**Why.** The previous walker silently dropped text in two cases (both reproduced by
tests):
1. **Mixed content** — a block element with its own text *and* a block child lost
   the own text. Input `Inspect... <warning><para>Do not touch</para></warning> Then
   reconnect...` emitted only "Do not touch"; two sentences vanished.
2. **Unknown inline tag** — a tag not in `INLINE_TAGS` was treated as block, which
   flipped its parent into a "container" and discarded the surrounding sentence.
   `Press the <ui>red</ui> button firmly` collapsed to just `red`.

Root cause: the binary rule *"if an element has any block child, recurse and emit
none of its own text; else emit `itertext()`."* The fix classifies children
**structurally** (does it have element children / is it a known block tag?) and
preserves a container's loose text.

**Apply (authoritative full file is in `proofer.tar.gz`):**

a. Harden the XML parser — uploads are untrusted (XXE / entity expansion):
```python
parser = etree.XMLParser(
    recover=True, remove_comments=True, remove_pis=True,
    resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False,
)
```

b. Expand `INLINE_TAGS` and add a `BLOCK_TAGS` allowlist (known block-level leaves
so adjacent `<para>`/`<li>`/etc. stay separate):
```python
INLINE_TAGS = { ...existing..., "symbol", "verbatimtext", "applicref",
    "changeinline", "hotspot", "uicontrol", "menucascade", "wintitle", "ph" }

BLOCK_TAGS = ({
    "para","p","listitem","item","li","step","substep","stepexpansion",
    "note","notes","warning","caution","attention","hazard","danger","safety",
    "table","informaltable","tgroup","thead","tbody","row","tr","entry",
    "td","th","cell","section","chapter","topic","subsection","part","body",
    "frontmatter","abstract","summary","preface","procedure","proceduralstep",
    "figure","legend","example","equation","formula","def","definition",
    "deflist","termentry","desc","shortdesc","prereq","context","result",
    "postreq","troubleshooting","cmd","info","verbatim","screen",
    "programlisting","codeblock","literallayout","blockquote","epigraph",
    "label","line","listoffigures",
} | HEADING_TAGS | PAGE_TAGS | TOC_TAGS | GLOSSARY_PAIR_TAGS)
```

c. Replace `_has_block_children` with `_is_structural` + `_kind_of`:
```python
def _is_structural(c):
    cl = _local(c)
    if cl in INLINE_TAGS:
        return False
    if any(isinstance(g.tag, str) for g in c):   # has element children -> sub-container
        return True
    return cl in BLOCK_TAGS                        # known block leaf

def _kind_of(tag):
    if tag in HEADING_TAGS: return "heading"
    if tag in PAGE_TAGS:    return "page"
    if tag in TOC_TAGS:     return "toc"
    return "body"
```

d. Rewrite `walk()` inside `from_xml` to keep loose text and recurse only into
structural children (uses an inner `add(text, el)` that classifies + path-stamps):
```python
def add(text, el):
    t = _ws(text)
    if not t: return
    try: epath = tree.getelementpath(el)
    except Exception: epath = _local(el)
    kind = _kind_of(_local(el))
    doc.blocks.append(Block(t, epath, kind, page=t if kind == "page" else None))

def walk(el):
    tag = _local(el)
    if tag in SKIP_TAGS: return
    raw = [c for c in el if isinstance(c.tag, str)]
    if not any(_local(c) not in SKIP_TAGS and _is_structural(c) for c in raw):
        add(_itertext(el), el)                      # leaf: one block, inline folded
        return
    buf = [el.text or ""]                            # container: keep loose text
    for c in raw:
        cl = _local(c)
        if cl in SKIP_TAGS:
            buf.append(c.tail or ""); continue
        if _is_structural(c):
            add("".join(buf), el); buf = []
            walk(c)
            buf.append(c.tail or "")
        else:
            buf.append("".join(c.itertext())); buf.append(c.tail or "")
    add("".join(buf), el)
```

**Known residual (note it in a comment):** an *unknown block-level leaf* tag (no
element children, not in `BLOCK_TAGS`) is folded into its neighbor. Text is never
lost — only the block boundary blurs. Fix by adding the tag to `BLOCK_TAGS`.

The `from_docx` path and the glossary-pair harvesting loop are unchanged.

---

## (2) `server.py` — static serving, portability, encoding

**Why.** `StaticFiles` crashed at startup if `static/` was missing *and* would have
served `.py` source at `/static/server.py`. Downloading presented files individually
flattens `static/`. `PROOFER_DATA` default `/data/jobs` becomes `C:\data\jobs` on
Windows. Report glyphs (`→ ⚠ × …`) crash `write_text` under cp1252.

**Apply:**
- Remove `from fastapi.staticfiles import StaticFiles` and the
  `app.mount("/static", StaticFiles(...))` line.
- Portable data dir:
  ```python
  DATA = Path(os.environ.get("PROOFER_DATA", str(HERE / "data" / "jobs")))
  ```
- Replace the static mount with explicit, location-agnostic routes (looks in both
  `./static` and the repo root; never exposes source):
  ```python
  def _frontend(name):
      for d in (HERE / "static", HERE):
          p = d / name
          if p.exists(): return p
      return None

  @app.get("/", response_class=HTMLResponse)
  async def index():
      p = _frontend("index.html")
      if not p: raise HTTPException(500, "index.html not found (root or ./static).")
      return p.read_text(encoding="utf-8")

  @app.get("/static/references.md")
  async def references():
      p = _frontend("references.md")
      if not p: raise HTTPException(404, "references.md not found.")
      return PlainTextResponse(p.read_text(encoding="utf-8"),
                               media_type="text/markdown; charset=utf-8")
  ```
- **UTF-8 on all file I/O — complete set** (confirm each):
  worker `findings.json` write, worker `report.md` write, `/report` read,
  `/findings` read, `index()` read, `references()` read.

---

## (3) `main.py`

- UTF-8 on the CLI writes:
  ```python
  json_path.write_text(json.dumps(..., ensure_ascii=False), encoding="utf-8")
  md_path.write_text(render_md(findings, clean_llm), encoding="utf-8")
  ```
- **Verify present** (earlier review change): `gather()` must run
  `checks.customer_consistency(...)` **before** `checks.spelling_candidates(...)` and
  pass every org surface word into the spell allowlist (`extra=`), so company names
  like "Globex" aren't flagged as misspellings.

---

## (4) Verify-present only — earlier review changes (do not redo)
- `checks.py`:
  - `_contains(long_tokens, sub_tokens)` helper.
  - `_suppress_subphrases(counts, min_count)` uses **residual** logic: drop a
    sub-phrase only if `count - best_container_count < min_count`.
  - `find_recurring_phrases` collects n-grams at floor 2, suppresses, **then** filters
    `>= min_count`.
  - `ORG_RE` internal connector group is `(?:&|and|of)?` (no `for`/`the` bridges).
  - `DEF_FWD`/`DEF_REV` strip a leading article from the captured expansion:
    `re.sub(r"^(?:the|a|an)\s+", "", exp, flags=re.I)`.
- `judge.py`: `judge(..., context=None)` prepends author-supplied context to the user
  message.

---

## Verify (run after applying)

**A. Extraction regression (the two bugs + controls):**
```python
import extract, tempfile, os
def run(xml):
    p=tempfile.mktemp(suffix=".xml"); open(p,"w").write(xml)
    d=extract.load(p); os.remove(p); return [b.text for b in d.blocks]

# mixed content -> all 3 sentences preserved
assert run("<m><para>A sentence one.<warning><para>B warning.</para></warning> C sentence three.</para></m>") \
    == ["A sentence one.", "B warning.", "C sentence three."]
# unknown inline tag -> sentence intact
assert run("<m><para>Press the <ui>red</ui> button firmly.</para></m>") == ["Press the red button firmly."]
# adjacent block leaves stay separate
assert run("<m><s><para>First.</para><para>Second.</para></s></m>") == ["First.", "Second."]
print("extraction OK")
```

**B. Full pipeline on the bundled fixture (no model needed):**
```bash
python main.py sample_manual.xml --no-llm --expected-customer "Acme Defense Systems, Inc." --min-count 2
```
Expect in the report: phrases `Line Replaceable Unit ×3 → LRU`, `Built-In Test
Equipment ×2 → BITE`, `Acme Defense Systems ×2 → ADS`; one acronym issue
(`RCM` conflicting_expansions); one misspelling (`maintenence`); page issues
`duplicate_page_number`, `page_gap`, `out_of_order_page`, `xref_out_of_range`;
customer `inconsistent_org_form` + `possible_wrong_customer`.

**C. Web service:**
```bash
uvicorn server:app --host 0.0.0.0 --port 8080
```
- `GET /` → 200, contains `PROOFER`.
- `GET /static/references.md` → 200, contains "Document context".
- `GET /static/server.py` → **404** (source must not be served).
- Upload `sample_manual.xml` via the UI → report renders with the `→` glyph intact.

---

## Still open (NOT in this change set — needs a decision)
- **docx gaps.** `from_docx` reads only the body, so it misses: (a) **footers**, where
  "Page X of Y" lives; (b) **Word TOC fields** (entries styled `TOC 1/2/3`, not the
  literal text); (c) **custom/localized heading styles**. Two options:
  - *Quick:* iterate `section.header`/`section.footer`, broaden heading detection
    beyond the "Heading" style prefix, detect `TOC` styles. No new deps; page checks
    stay text-heuristic.
  - *Better:* render `docx → PDF` via headless LibreOffice and extract from the PDF —
    fixes all three **and** gives true pagination, so page-number checks become real
    verification. Adds a LibreOffice dependency to the container.
- **checks.py review (Part 2).** Pressure-test n-gram suppression and org clustering
  on messier inputs than the fixture (very long phrases, overlapping orgs, noisy
  capitalization).
