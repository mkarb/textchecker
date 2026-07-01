"""Deterministic proofreading passes over a Doc.

Philosophy: these passes do the *recall* (exhaustively surface candidates the
cheap, reliable way). The model does the *precision* (judges the candidates).
Nothing here calls an LLM.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections import Counter, defaultdict
import re

STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "be", "this", "that", "these", "those",
    "its", "it", "into", "per", "via", "not", "no", "than", "then", "but", "if",
    "so", "such", "may", "can", "will", "shall", "each",
}
# Ultra-common acronyms we don't flag as "used but undefined".
COMMON_ACR = {"US", "USA", "ID", "OK", "TBD", "NA", "FAQ", "PDF", "URL", "FAA", "DOD", "DOT", "OEM"}
# The "used but never defined" check is noisy on caps-heavy technical docs (every
# all-caps label trips it). Off by default; the model's acronym table is the better
# source for what's actually defined. Flip to True to re-enable (dictionary-filtered).
REPORT_UNDEFINED = False

WORD_RE = re.compile(r"[A-Za-z]+(?:['\-][A-Za-z]+)*")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&./'\-]*")


def _ws(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _tokens(text):
    return TOKEN_RE.findall(text)


def _is_caps(tok):
    return bool(tok) and tok[:1].isupper()


def _significant(tok):
    return tok.lower() not in STOPWORDS and any(ch.isalpha() for ch in tok)


def _snippet(text, word, width=40):
    i = text.find(word)
    if i < 0:
        return _ws(text[: 2 * width])
    s, e = max(0, i - width), min(len(text), i + len(word) + width)
    return ("…" if s > 0 else "") + _ws(text[s:e]) + ("…" if e < len(text) else "")


# ------------------------------------------------ recurring phrases -> acronyms
@dataclass
class PhraseCand:
    phrase: str
    count: int
    proposed: str
    locations: list


def _is_spec_token(tok):
    """True for tokens that are BOM/spec noise rather than words that could form an
    acronym: a *mixed* alphanumeric (part number / measurement -- 50V, 10uF, 947nm,
    ST2-611-A1, 12105C106MAT2A, SD-18, L3) or a lone letter (stray table-column header
    like 'V'). A bare integer is NOT spec noise on its own, so legitimate terms like
    'Level 3 Maintenance' or 'Class A Mishap' survive (the digit token is simply
    non-significant and the surrounding words still carry the phrase)."""
    core = tok.strip("-/.,()")
    has_digit = any(ch.isdigit() for ch in tok)
    has_alpha = any(ch.isalpha() for ch in tok)
    if has_digit and has_alpha:                    # mixed alphanumeric: part number / spec
        return True
    if len(core) == 1 and core.isalpha():          # lone letter: stray column header
        return True
    return False


def _candidate_gram(gram):
    if len(gram) < 2:
        return False
    if any(_is_spec_token(g) for g in gram):          # drop part numbers / specs / lone letters
        return False
    if gram[0].lower() in STOPWORDS or gram[-1].lower() in STOPWORDS:
        return False
    sig = [g for g in gram if _significant(g)]
    if len(sig) < 2:
        return False
    caps = [g for g in sig if _is_caps(g) or g.isupper()]
    if len(caps) < max(2, (len(sig) + 1) // 2):      # most significant tokens capitalised
        return False
    if all(g.islower() for g in gram):
        return False
    return True


def _propose_acronym(phrase):
    letters = []
    for tok in phrase.split():
        if not _significant(tok):
            continue
        for part in re.split(r"[-/]", tok):
            if part and part[0].isalpha():
                letters.append(part[0].upper())
    return "".join(letters)


def _contains(long_tokens, sub_tokens):
    m = len(sub_tokens)
    return any(long_tokens[i:i + m] == sub_tokens for i in range(len(long_tokens) - m + 1))


def _suppress_subphrases(counts, min_count):
    """Drop a shorter phrase only if it is contained in a longer phrase AND its
    occurrences *outside* that longer phrase (count - best container count) fall
    below min_count -- i.e. it doesn't independently recur often enough to matter.
    This keeps the most specific phrase even when the shorter one counts higher.

    Semantics-preserving inverted-index form (was O(P^2) all-pairs with a re-split per
    comparison, which turned recurring-phrase detection quadratic on large caps-heavy
    manuals). Any longer phrase that contains `s` must contain EVERY token of `s`,
    including its rarest one, so scanning only the phrases sharing s's rarest token
    finds the same best container as the all-pairs scan."""
    items = [(s, tuple(s.split()), c) for s, c in counts.items()]
    by_token = defaultdict(list)
    for entry in items:
        for t in set(entry[1]):
            by_token[t].append(entry)
    drop = set()
    for s, st, cs in items:
        if not st:
            continue
        rarest = min(st, key=lambda t: len(by_token[t]))
        best = 0
        for l, lt, cl in by_token[rarest]:
            if l != s and len(lt) > len(st) and cl > best and _contains(lt, st):
                best = cl
        if best and cs - best < min_count:
            drop.add(s)
    return {p: c for p, c in counts.items() if p not in drop}


def find_recurring_phrases(doc, min_count=3, max_len=6):
    counts = Counter()
    locs = defaultdict(list)
    for b in doc.blocks:
        toks = _tokens(b.text)
        for n in range(2, max_len + 1):
            for i in range(len(toks) - n + 1):
                gram = toks[i:i + n]
                if _candidate_gram(gram):
                    phrase = " ".join(gram)
                    counts[phrase] += 1
                    if len(locs[phrase]) < 5:
                        locs[phrase].append(b.path)
    raw = {p: c for p, c in counts.items() if c >= 2}        # low floor so long
    kept = _suppress_subphrases(raw, min_count)               # phrases survive to
    kept = {p: c for p, c in kept.items() if c >= min_count}  # suppress their subs
    out = [PhraseCand(p, c, _propose_acronym(p), locs[p]) for p, c in kept.items()]
    out.sort(key=lambda x: (-x.count, -len(x.phrase)))
    return out


# --------------------------------------------------- acronym consistency / errors
DEF_FWD = re.compile(
    r"([A-Z][A-Za-z0-9&./'\-]*(?:\s+[A-Za-z0-9&./'\-]+){1,6})\s*\(\s*([A-Z][A-Za-z0-9&./\-]{1,9})\s*\)"
)
DEF_REV = re.compile(
    r"\b([A-Z][A-Z0-9&./\-]{1,9})\s*\(\s*([A-Z][A-Za-z0-9&./'\-]+(?:\s+[A-Za-z0-9&./'\-]+){1,6})\s*\)"
)
ACR_RE = re.compile(r"\b([A-Z]{2,6}s?)\b")


@dataclass
class AcronymFinding:
    acronym: str
    problem: str
    detail: str
    locations: list


def _norm_exp(s):
    return _ws(s).lower()


def _initials(expansion):
    out = []
    for tok in expansion.split():
        if tok.lower() in STOPWORDS:
            continue
        for part in re.split(r"[-/]", tok):
            if part and part[0].isalpha():
                out.append(part[0].lower())
    return "".join(out)


def _sig_words(exp):
    return [w for w in exp.split() if _significant(w)]


def _defines(acr, exp):
    """Reject clause-pollution captures: accept `exp` as defining `acr` only if the
    acronym's first letter matches the expansion's first significant word AND the
    number of significant words is within one of the acronym's letter count. A real
    'Long Name (ACR)' satisfies both; a captured sentence fragment does not."""
    letters = re.sub(r"[^A-Za-z]", "", acr)
    sig = _sig_words(exp)
    if not letters or not sig:
        return False
    if sig[0][:1].lower() != letters[0].lower():
        return False
    return len(letters) - 1 <= len(sig) <= len(letters) + 1


def acronym_consistency(doc):
    defs = defaultdict(set)        # acronym -> {expansions}
    def_locs = defaultdict(list)
    used = defaultdict(list)       # acronym -> locations

    for a, e in doc.glossary_defs.items():
        defs[a.rstrip("s")].add(e)

    for b in doc.blocks:
        for m in DEF_FWD.finditer(b.text):
            acr = m.group(2).strip()
            exp = re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip(), flags=re.I)
            exp = re.sub(rf"^{re.escape(acr)}\s+", "", exp)        # drop leading repeat of the acronym
            if _defines(acr, exp):
                defs[acr.rstrip("s")].add(exp)
                def_locs[acr.rstrip("s")].append(b.path)
        for m in DEF_REV.finditer(b.text):
            acr = m.group(1).strip()
            exp = re.sub(r"^(?:the|a|an)\s+", "", m.group(2).strip(), flags=re.I)
            exp = re.sub(rf"^{re.escape(acr)}\s+", "", exp)
            if _defines(acr, exp):
                defs[acr.rstrip("s")].add(exp)
                def_locs[acr.rstrip("s")].append(b.path)
        for m in ACR_RE.finditer(b.text):
            used[m.group(1).rstrip("s")].append(b.path)

    findings = []

    # one acronym, multiple distinct expansions
    for acr, exps in defs.items():
        norm = {}
        for e in exps:
            norm.setdefault(_norm_exp(e), e)
        if len(norm) > 1:
            findings.append(AcronymFinding(acr, "conflicting_expansions",
                                           "; ".join(norm.values()), def_locs.get(acr, [])[:5]))

    # multiple acronyms, one expansion
    exp_to_acr = defaultdict(set)
    for acr, exps in defs.items():
        for e in exps:
            exp_to_acr[_norm_exp(e)].add(acr)
    for e, acrs in exp_to_acr.items():
        if len(acrs) > 1:
            findings.append(AcronymFinding("/".join(sorted(acrs)),
                                           "multiple_acronyms_one_term", e, []))

    # used but never defined -- off by default (see REPORT_UNDEFINED). When on, skip
    # real dictionary words so ordinary all-caps labels (AND, FOR, POWER...) don't flood.
    if REPORT_UNDEFINED:
        from spellchecker import SpellChecker
        _dict = SpellChecker(distance=1)
        for acr, where in used.items():
            if acr in defs or acr in COMMON_ACR or acr.lower() in _dict:
                continue
            findings.append(AcronymFinding(acr, "used_not_defined",
                                           f"appears {len(where)}x, no '(...)' definition found",
                                           where[:5]))

    return findings, {a: sorted(s) for a, s in defs.items()}


# ------------------------------------------------------------------- spelling
@dataclass
class SpellFinding:
    word: str
    suggestion: str
    path: str
    context: str


def build_allowlist(doc, phrases, acro_defs, extra=()):
    """Domain vocabulary that should NOT be flagged: phrase words, acronyms +
    their expansions, and anything the caller passes in (e.g. customer name)."""
    allow = {w.lower() for w in extra if w}
    for pc in phrases:
        allow.update(w.lower() for w in WORD_RE.findall(pc.phrase))
    for a, exps in acro_defs.items():
        allow.add(a.lower())
        for e in exps:
            allow.update(w.lower() for w in WORD_RE.findall(e))
    return allow


_SPELL_PREFIXES = {"non", "co", "re", "pre", "post", "anti", "multi", "sub", "self",
                   "cross", "inter", "intra", "over", "under", "semi", "mid", "bi", "tri", "de"}
_DICT_CACHE = None


def load_extra_dictionary(path=None):
    """Words the spell-checker should treat as correct -- abbreviations and domain terms
    (one per line, '#' comments allowed). Sibling file dictionary_extra.txt; {} if absent,
    so the pipeline runs fine without it. Extend it whenever a valid term is flagged."""
    global _DICT_CACHE
    if path is None and _DICT_CACHE is not None:
        return _DICT_CACHE
    from pathlib import Path
    p = Path(path) if path else (Path(__file__).resolve().parent / "dictionary_extra.txt")
    out = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            w = line.split("#", 1)[0].strip().lower()
            if w:
                out.add(w)
    if path is None:
        _DICT_CACHE = out
    return out


def spelling_candidates(doc, allow, max_findings=200):
    from spellchecker import SpellChecker
    spell = SpellChecker(distance=1)
    extra = load_extra_dictionary()
    if allow or extra:
        spell.word_frequency.load_words(list(set(allow) | extra))
        allow = set(allow) | extra
    seen, out = set(), []
    for b in doc.blocks:
        for raw in WORD_RE.findall(b.text):
            low = raw.lower()
            if len(raw) <= 3 or raw.isupper() or any(c.isdigit() for c in raw):
                continue
            if low in allow or low in seen:
                continue
            seen.add(low)
            # acronym plural (FBDs, CCAs, SRUs): the stem is an all-caps acronym
            stem = raw[:-2] if raw[-2:].lower() == "es" else (raw[:-1] if raw[-1:].lower() == "s" else raw)
            if len(stem) >= 2 and stem.isupper():
                continue
            # plural of a known word the dictionary lacked as a plural (vias -> via)
            if low.endswith("s") and (low[:-1] in spell or low[:-1] in allow):
                continue
            if low in spell:
                continue
            sugg = spell.correction(low) or ""
            if not sugg or sugg == low:
                continue
            if "-" in low:                                   # hyphenated forms aren't misspellings
                parts = [s for s in low.split("-") if s]
                head, tail = parts[0], "-".join(parts[1:])
                prefix_ok = head in _SPELL_PREFIXES and (tail in spell or tail in allow)
                compound_ok = all((s in spell or s in allow) for s in parts)
                if prefix_ok or compound_ok or sugg == low.replace("-", ""):
                    continue
            out.append(SpellFinding(raw, sugg, b.path, _snippet(b.text, raw)))
            if len(out) >= max_findings:
                return out
    return out


# ---------------------------------------------------------------- page numbers
PAGE_OF = re.compile(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", re.I)
XREF = re.compile(r"\b(?:see|refer to|shown on|listed on|on|per)\s+page\s+(\d+)\b", re.I)
TOC_ENTRY = re.compile(r"^(.*?\S)[\.\s]{2,}(\d+)\s*$")


@dataclass
class PageFinding:
    problem: str
    detail: str
    location: str


def page_checks(doc):
    findings, seq, totals = [], [], set()

    # collect markers in document order
    for b in doc.blocks:
        if b.kind == "page" and b.page:
            m = re.search(r"(\d+)", b.page)
            if m:
                seq.append((int(m.group(1)), b.path))
        for m in PAGE_OF.finditer(b.text):
            totals.add(int(m.group(2)))
            # The docx extractor appends one synthetic 'Page 1 of N' marker (path
            # 'sections/pagination') just to carry the total; it is NOT a real in-order
            # page label, so harvest its Y but keep its X out of the sequence -- otherwise
            # real in-text 'Page X of Y' strings make it trip duplicate/out-of-order.
            if b.path != "sections/pagination":
                seq.append((int(m.group(1)), b.path))

    if len(totals) > 1:
        findings.append(PageFinding("inconsistent_total",
                                    f"'Page X of Y' uses multiple totals: {sorted(totals)}", ""))
    total = max(totals) if totals else None

    seen, prev = set(), None
    for n, path in seq:                       # seq already in document order
        if n in seen:
            findings.append(PageFinding("duplicate_page_number",
                                        f"page {n} labeled more than once", path))
        seen.add(n)
        if prev is not None and n > prev + 1:
            findings.append(PageFinding("page_gap", f"jumps from {prev} to {n}", path))
        if prev is not None and n < prev:
            findings.append(PageFinding("out_of_order_page", f"{n} follows {prev}", path))
        prev = n

    for b in doc.blocks:
        for m in XREF.finditer(b.text):
            n = int(m.group(1))
            if total and (n < 1 or n > total):
                findings.append(PageFinding("xref_out_of_range",
                                            f"reference to page {n} (document has {total})", b.path))

    # table of contents entries
    toc_entries, in_toc = [], False
    for b in doc.blocks:
        if b.kind == "toc" or b.text.lower() in ("table of contents", "contents"):
            in_toc = True
            continue
        if in_toc:
            m = TOC_ENTRY.match(b.text)
            if m:
                toc_entries.append((m.group(1).strip(), int(m.group(2)), b.path))
            elif b.kind == "heading":
                in_toc = False
    if total:
        for title, pg, path in toc_entries:
            if pg > total:
                findings.append(PageFinding("toc_page_out_of_range",
                                            f"TOC lists '{title}' on page {pg} (> {total})", path))

    return findings, toc_entries, total


# ------------------------------------------------------------- customer / orgs
ORG_SUFFIX = (r"(?:Inc|Inc\.|LLC|Corp|Corp\.|Corporation|Company|Co|Co\.|Ltd|Ltd\.|"
              r"GmbH|PLC|LP|LLP|Technologies|Systems|Industries|Defense|Aerospace|"
              r"Group|International|Solutions|Holdings|Enterprises)")
# Note: 'and' is deliberately NOT an inner joiner -- it would bridge two distinct
# companies ("Lockheed Martin and General Dynamics Corporation") into one captured org.
# '&' and 'of' stay (real intra-name joiners: "Booz Allen & Hamilton", "Department of
# Defense").
ORG_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\-]+(?:\s+(?:&|of)?\s*[A-Z][A-Za-z0-9&.\-]+){0,5},?\s+"
    + ORG_SUFFIX + r")\b"
)
# Only TRUE legal-form suffixes are stripped for the cluster key. Descriptive words
# (Defense/Systems/Aerospace/...) distinguish separate entities ("Acme Defense" vs
# "Acme Systems") and must stay in the key, or distinct orgs collapse into one and
# fire a false inconsistent_org_form. They remain in ORG_SUFFIX for *matching* only.
_SUFFIX_WORDS = re.compile(
    r"\b(inc|llc|corp|corporation|company|co|ltd|gmbh|plc|lp|llp)\b"
)


@dataclass
class OrgCluster:
    key: str
    surfaces: dict          # surface form -> count
    contexts: list


def _org_key(name):
    s = re.sub(r"[.,]", "", name.lower())
    s = _SUFFIX_WORDS.sub("", s)
    return _ws(s)


def customer_consistency(doc, expected=None):
    surfaces, contexts = Counter(), defaultdict(list)
    for b in doc.blocks:
        for m in ORG_RE.finditer(b.text):
            name = _ws(m.group(1))
            surfaces[name] += 1
            if len(contexts[name]) < 3:
                contexts[name].append(_snippet(b.text, name))

    clusters = {}
    for name, cnt in surfaces.items():
        c = clusters.setdefault(_org_key(name), OrgCluster(_org_key(name), {}, []))
        c.surfaces[name] = cnt
        c.contexts.extend(contexts[name])

    findings = []
    for c in clusters.values():
        if len(c.surfaces) > 1:
            findings.append(("inconsistent_org_form",
                             f"same entity written multiple ways: {dict(c.surfaces)}"))

    if expected:
        ekey = _org_key(expected)
        for c in clusters.values():
            if c.key and c.key != ekey:
                top = max(c.surfaces, key=c.surfaces.get)
                findings.append(("possible_wrong_customer",
                                 f"unexpected organization '{top}' "
                                 f"(expected primary customer '{expected}')"))
    return clusters, findings


# --------------------------------------------------------------- occurrence counts
# Exact, deterministic counts of how often an acronym token or an expansion phrase
# appears in the document. These are computed here (never by the model) and joined
# onto the model's acronym table at render time -- a "proposed" acronym whose phrase
# count is tiny is a sign it should not have been proposed.
def count_acronym(text, acr):
    """Occurrences of an acronym as a standalone token (case-sensitive, not inside a
    larger alphanumeric run). Handles slashes/digits, e.g. I/F, R/T, ICE5200."""
    if not acr:
        return 0
    pat = r"(?<![A-Za-z0-9])" + re.escape(acr) + r"(?![A-Za-z0-9])"
    return len(re.findall(pat, text or ""))


def count_phrase(text, phrase):
    """Occurrences of an expansion phrase (case-insensitive, tolerant of variable
    whitespace, bounded so it is not matched inside a longer word)."""
    toks = (phrase or "").split()
    if not toks:
        return 0
    body = r"\s+".join(re.escape(t) for t in toks)
    pat = r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"
    return len(re.findall(pat, text or "", flags=re.IGNORECASE))


def annotate_acronym_counts(table, text):
    """Attach exact occurrence counts to each acronym-table row, in place. Instead of
    re-scanning the whole document once per row (O(rows x doc) -- ~7s for 150 rows over a
    few MB at render time), scan once. Acronyms never overlap under the non-alnum
    boundary, so a single longest-first alternation is exactly equivalent to per-row
    count_acronym. Expansions CAN overlap (a phrase inside a longer phrase), where a
    greedy alternation would undercount, so each DISTINCT expansion is counted with the
    exact count_phrase but deduped so identical expansions are scanned only once."""
    rows = table or []
    text = text or ""

    acrs = sorted({r.get("acronym", "") for r in rows if r.get("acronym")},
                  key=len, reverse=True)
    acr_counts = Counter()
    if acrs:
        pat = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(a) for a in acrs)
                         + r")(?![A-Za-z0-9])")
        acr_counts = Counter(pat.findall(text))

    exp_counts = {}
    for e in {" ".join((r.get("expansion", "") or "").split()) for r in rows}:
        if e:
            exp_counts[e.lower()] = count_phrase(text, e)

    for r in rows:
        r["acronym_count"] = acr_counts.get(r.get("acronym", ""), 0)
        exp_key = " ".join((r.get("expansion", "") or "").split()).lower()
        r["expansion_count"] = exp_counts.get(exp_key, 0)
    return table
