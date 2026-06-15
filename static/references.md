# How to get the best results

This tool works in two stages. A **deterministic scan** reads your document and
finds *candidates* — repeated phrases that might deserve an acronym, acronyms
that look inconsistent, possible misspellings, page-number problems, and company
names. Then the **local model** reviews those candidates and makes the judgment
calls: which acronym expansion is correct, which "misspellings" are really domain
terms, who the primary customer is.

The model only sees the candidates and whatever context you give it — it does not
re-read the whole manual. So the single biggest lever you have is **telling it
about your document** in the *Document context* box before you upload.

---

## The Document context box

Anything you type there is given to the model as background. A few sentences is
plenty. Good things to include:

**What kind of document it is.** "This is an avionics field-maintenance manual"
vs "this is a software user guide" completely changes which terms are normal and
which acronyms are expected. The model judges domain terminology far better when
it knows the domain.

**The primary customer.** Even though there's a separate *Expected customer*
field, restating it here helps: "This manual is for Acme Defense Systems. Any
other company name is almost certainly a copy-paste leftover and should be
flagged."

**Your house style for acronyms.** For example: "Every acronym must be spelled
out on first use with the acronym in parentheses, e.g. Line Replaceable Unit
(LRU)." The model will then flag acronyms used before they are defined.

**The controlling standard or approved glossary, if you have one.** If a customer
spec or your own glossary is authoritative, name it or paste the relevant
entries: "Approved expansions: LRU = Line Replaceable Unit; BIT = Built-In Test.
Flag anything that deviates." The model will defer to what you give it.

**Known false alarms.** "Ignore part numbers like A1-204 and the project codename
GRYPHON." This trims noise from the spelling results.

---

## Example context, good vs vague

**Vague (don't):**

> Please check my document.

**Specific (do):**

> This is a depot-level maintenance manual for the F-15C radar system, written for
> the U.S. Air Force. House style: spell out every acronym on first use with the
> acronym in parentheses. Approved terms include LRU (Line Replaceable Unit) and
> WRA (Weapon Replaceable Assembly). The only customer is the USAF — flag any
> other organization name. Ignore part numbers (e.g. 622R-1A) and the codename
> SILVERFOX.

The second version lets the model resolve acronym conflicts correctly, suppress
domain-term false positives, and catch a leftover customer name with confidence.

---

## What each result section means

**Acronym table.** The model's proposed master list — acronyms it found defined
in the document plus ones it suggests for phrases that recur often. "Existing"
means it was already defined; "proposed" means you repeat the phrase enough that
an acronym would help. Review the proposed ones — not every repeated phrase needs
an acronym.

**Acronym issues.** The important one is *conflicting expansions*: the same
acronym used to mean two different things (a real and common error). Also flags
acronyms used but never spelled out, and acronyms whose letters don't match their
expansion.

**Possible misspellings.** The weakest signal for technical writing, because
manuals are full of valid terms a dictionary doesn't know. The model is told to
discard domain terms, part numbers, and proper nouns and keep only genuine typos
— but skim these, and add anything it shouldn't have flagged to your context box
for next time.

**Page-number issues.** Catches duplicated page numbers, gaps in the sequence,
out-of-order pages, and cross-references that point outside the document (e.g.
"see page 80" in a 60-page manual). Note: because Word/XML files have no fixed
pages until they're rendered, this verifies the page *markers and references in
the text* — it cannot confirm that "see page 42" lands on the exactly correct
printed page. For that, proofread the final PDF.

**Customer name.** Flags the same company written inconsistently (e.g. with and
without a comma before "Inc.") and any organization that looks like it doesn't
belong — usually a name left over from a reused template.

---

## Tips that consistently help

- **Set the Expected customer field.** It turns the leftover-customer check from a
  guess into a precise comparison.
- **Start strict, then relax.** If you get too many spelling false positives, add
  the offending terms to your context box and re-run; the list shrinks quickly.
- **Shorter, cleaner inputs are judged better.** If a manual is enormous, running
  a chapter at a time gives tighter, more reliable results than the whole book at
  once.
- **Trust the page and acronym-conflict findings most**; treat spelling as a
  prompt to look, not a verdict.
- **The model is deterministic here** (it runs at temperature 0), so the same
  document and context produce the same report — good for re-checking after edits.

---

## What this tool does *not* do

It does not edit your document or rewrite text — it reports. It does not send your
document anywhere; everything runs on the local machine. And it judges only what
the scan surfaces plus your context, so it will not catch a factual error,
a wrong torque value, or a missing safety warning. It is a consistency and
proofreading aid, not a technical reviewer.
