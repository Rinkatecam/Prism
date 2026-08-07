# Engineering learnings — the design-token migration

Written for whoever picks this repository up cold. It is not a history of the
design round; it is the set of mistakes that round kept making, stated so they
transfer to code that has nothing to do with colour.

All of it is traceable. The evidence is the commit bodies on
`design/tokens-phase-1` — covered here through `820a1d3` — which are unusually
long on purpose:

```bash
git log --reverse --format='=== %h %s%n%b' origin/master..HEAD
```

The tools referred to throughout are `tools/design_tokens.py`,
`tools/migrate_tokens.py`, `tools/migrate_css_tokens.py`,
`tools/migrate_brand_roles.py`, `tools/migrate_focus_visible.py`,
`tools/verify_token_equivalence.py` and `tools/verify_css_tokens.py`; the
tests are `tests/test_design_*.py`. Most of the reasoning below is also
written into those files at the point where it matters, which is where it
belongs. This document exists so that the pattern is visible in one place
rather than scattered across twenty docstrings.

---

## 1. The thesis

> Something that looks installed, reports success, and is not doing the work.

`docs/plans/HANDOFF.md` §4 named that pattern and counted eleven instances
across two earlier sessions. This branch produced roughly thirty more in a
single work stream, which is the point worth absorbing: it is not a run of bad
luck, it is the default outcome of any check whose own correctness is never
checked. A guard, a test, a verifier and a measurement all share the property
that they are believed when they say nothing is wrong — so a broken one is
indistinguishable from a healthy one until something else independently
contradicts it.

Two of the handoff's eleven (the deleted dark backgrounds, the colliding token
test) were in fact found by this branch's first commit, so treat the catalogue
below as continuing the same count rather than starting a new one.

---

## 2. The catalogue

Grouped by the shape of the failure, not by the order it was found. Each entry
ends with the general rule, which is the part meant to survive.

### 2.1 Guards that stopped guarding

**1. Three colour abstractions, zero users.** `app.css` custom properties,
`tailwind.config.extend.colors` and the status classes all existed and all
three were referenced by nothing. Measured across the templates:
`var(--token)` appeared **0** times, hardcoded hex literals **5,184** times,
across **113** distinct colours. Nothing was broken — typing a hex was simply
easier and nothing ever failed for not using the abstraction. Caught by
counting call sites rather than by noticing the abstraction was there.
*Rule: an abstraction with no enforcement decays to zero users. Count the
references, not the definitions; if the count is zero the abstraction does not
exist regardless of how good it looks.*

**2. A file that documented a guarantee nobody was enforcing.** `app.css`
stated in its own header that tests asserted it matched the Python token
table. No such test existed. A token defined in Python and missing from the
CSS renders as nothing at all, and would have done so silently.
*Rule: when a comment claims "there is a test for this", go and find the test.
The claim is the cheapest thing in the file to write and the least likely to
be maintained.*

**3. A generated-region marker that matched shape rather than saying what it
protected.** The CSS converter located generated output with a regex for
`--c-name: <digits and spaces>`. That works exactly as long as the generator
emits only colour triplets. It began emitting durations, easings and box
shadows — and a shadow is full of `rgb(...)` for the converter to rewrite,
after which the next regeneration would have wiped the result. Replaced with
explicit `GENERATED_OPEN` / `GENERATED_CLOSE` sentinel comments.
*Rule: delimit a generated region with sentinels that say "this is generated",
never with a pattern that infers it from the current content. Shape-matching
protects the content you had in mind when you wrote the pattern.*

**4. A config override that governed 12 of 750 elements.** Phase 1a collapsed
the radius scale by overriding Tailwind's `sm` and `md` to land on the two
values already carrying 734 of 746 uses, and the commit promised that
softening the corners later would then cost one config line. It would not
have: 738 of 750 uses spell `rounded` and `rounded-lg`, which read Tailwind's
`DEFAULT` and `lg` keys — different keys, untouched. The rule was real,
correct, and applied to almost nothing. Found when the softening was actually
attempted.
*Rule: after installing a central control, measure how many of the existing
call sites it actually reaches. "The scale is now defined" and "the scale now
governs" are different claims.*

**5. `prefers-reduced-motion` honoured by five rules out of 156.** Three
separate media blocks covered five declarations, against 123 transitions and
33 animations. Every remaining one kept running for a user who had explicitly
asked it not to, and every new transition opted out again by default, because
honouring the preference meant remembering a line. Replaced with one block on
`*, *::before, *::after`.
*Rule: an opt-in accessibility measure is a measure that is not in place. If
compliance depends on the author remembering, assume roughly the coverage
measured here.*

**6. Three status colours with no dark override at all.** `--color-healthy`,
`--color-offline` and `--color-resolved` had no `.dark` rule, so each leaked
its light value into dark mode. Nobody had noticed because nothing compares
the two themes. Surfaced as a side effect of tokenising the sheet.
*Rule: a mode, locale or theme that no test and no tool ever enumerates will
accumulate holes proportional to how rarely anyone opens it.*

**7. A duplicate rule 600 lines away, silently overriding the real one.**
`::-webkit-scrollbar-thumb:hover { background: #B4A0E8 }` sat far below the
scrollbar block it contradicted, and a second copy dropped `background-clip`,
so the thumb lost its pill shape at the exact moment you touched it. Somebody
had already tried to do this work and their attempt was quietly losing to
itself.
*Rule: in a cascade-ordered file, a later duplicate is not a duplicate, it is
an override. When you find a rule that seems not to apply, search the whole
file for the property before assuming you understand the rule you are
reading.*

**8. An exemption that never fired, in a tool that reported success anyway.**
`migrate_brand_roles.py` turned page-title icons violet in pass 1 and every
other decorative icon turquoise in pass 2, with pass 2 exempting the titles.
The exemption compared an `<h1>` match position against an `<i>` match
position; those start in different places, so it never matched. Pass 1 turned
nine page titles violet, pass 2 turned all nine back, and the tool printed
nine successful changes either way. Caught by opening the page — not by any
count the tool produced. Now keyed on the class attribute's own span, the
title rule accepts `text-accent` as an input so a wrongly-converted title is
corrected rather than frozen, and `test_the_assignment_is_idempotent` asserts
the exact property that was violated.
*Rule: a tool's change count measures work done, not work that stuck. Where
two passes can undo each other, assert idempotence over the real tree — it is
the cheapest possible detector for "the rule ran and achieved nothing".*

**9. Focus affordances that were installed and then partially removed.**
Measured: 87 `focus:` utilities and 0 `focus-visible:`. `:focus` matches a
mouse click as well as keyboard navigation, so clicking any button left a 2px
ring stuck on it — which is why focus rings get deleted from codebases, and
four `focus:outline-none` utilities were already a partial version of exactly
that. Separately, a test asserting that anything suppressing its native
outline draws its own ring found two controls that draw nothing (a run-order
number input in `operations.html`, a dependency-browser search field in
`servers.html`). Both are invisible to a keyboard user and perfectly fine to
anyone with a mouse, which is everyone who had ever reviewed them.
*Rule: for any affordance only one class of user can perceive, the reviewer
population is not the affected population. Write the test that enumerates the
sites; do not rely on someone noticing.*

### 2.2 Tests that asserted nothing

**10. Nine passing tests over a converter that deleted 275 dark backgrounds.**
`bg-white dark:bg-[#1E293B]` appears on 275 elements. The light half is
Tailwind's built-in class, so the converter saw an unpaired `dark:` utility,
judged it redundant and deleted it — every card would have gone white in dark
mode. All nine unit tests passed, because all nine used arbitrary values on
both sides. The bug was found by reading converted real markup.
*Rule: a test suite written from one mental model tests one shape. Before
trusting it, run the transformation over the real input and read the diff.
`verify_token_equivalence.py` exists because of this.*

**11. A test that passed because two tokens collided.** `page` and `raised`
share the dark value `#0F172A`, so a plain hex→token dict resolved to
whichever was declared last, and one test passed for that reason alone. Later
the same collision widened: `#0F172A` became the dark half of `page`, `raised`
**and** `field`, and `#FFFFFF` the light half of both `card` and `field`.
Guessing would have turned 157 form inputs into cards. `_by_hex()` now returns
every candidate and the caller disambiguates with the other half of the pair,
which is the only thing that can.
*Rule: a lookup that silently picks a winner among equals produces tests that
pass by coincidence. Make ambiguity an explicit value — a list, or `None` —
and force the caller to resolve it or decline.*

**12. A test that asserted on a constant declared in the test file.** One of
the four radius tests read its expectation from a literal defined a few lines
above rather than from `base.html`, and passed unchanged while a mutation put
a third value into the real config. It reads the config now.
*Rule: if a test can pass with the production artefact deleted, it is testing
the test. Every assertion must terminate at a file the application actually
loads.*

**13. A substring assertion over generated JavaScript.** `critical-tint: '…'`
is a JavaScript syntax error, and the Tailwind config is an inline `<script>`
— so the consequence is not one broken colour, it is `tailwind.config` never
being assigned and the application losing its entire theme. The map is now
emitted with every key quoted, and the test *parses* it rather than
substring-matching, because a substring assertion is precisely what would have
missed it.
*Rule: when you generate code, assert that it parses in the target grammar. A
substring check confirms the characters you thought about and nothing else.*

**14. A contrast test built on an unverified contrast helper.** Four tests
enforce WCAG AA across the palette; a fifth asserts the helper itself is right
(black on white is exactly 21:1, any colour on itself exactly 1:1). Without
it, a broken luminance formula passes everything and the whole suite is
decorative.
*Rule: a test whose verdict is computed by your own helper inherits that
helper's bugs. Pin the helper against a value you know independently.*

**15. A guardrail whose detector was never shown to match anything.**
`test_design_scroll.py` fails any element that is both rounded and its own
scroll container. A second test asserts the detector actually matches the
exact markup that was in `activity_feed.html` before the fix.
*Rule: a guardrail that finds nothing is indistinguishable from a guardrail
whose pattern is wrong. Pair every "no offenders" assertion with a positive
control built from a real historical offender.*

**16. A ratchet nobody tightens.** `LITERAL_BASELINE` records per-file hex
counts and fails on any increase. Two supporting tests do the rest of the
work: one fails when a brand-new template contains a literal at all (the
baseline records history, it is not a licence to start again elsewhere), and
one fails when the real count drops below the baseline without the baseline
coming down with it — otherwise every cleanup silently buys back the headroom
it just won.
*Rule: a ratchet has three tests, not one: no growth, no new entrants, no
slack left behind.*

### 2.3 Verifiers that verified less than they claimed

**17. The verifier reported 0 unexpected differences and could not see the
bug.** The converter collapsed `bg-[#2563EB]/10 dark:bg-[#3B82F6]/20` into
`bg-info/10` — same colours, different opacities, and one class cannot carry
two alphas, so dark mode was quietly restated at half the intended opacity. 43
paired utilities have mismatched halves, 30 of them a solid light surface
against a translucent dark one. `effective()` compared `(utility, theme, hex)`
and never looked at the modifier, so the whole run was green. Found by reading
the first batch's diff before committing it. The fix carries alpha through the
comparison, rewrites the dark half as its own token utility keeping its own
alpha, and was mutation-checked: restoring the unconditional collapse fails
two unit tests and makes the verifier report exactly 43 unexpected
differences, matching an independent count of the mismatched pairs.
*Rule: enumerate what your comparison key omits and write the omissions down.
Anything not in the key is invisible to every green run the verifier will ever
produce.*

**18. The verifier inspected a narrower region than the converter rewrote.**
The converter was widened to rewrite class strings built in JavaScript — 629
literals outside any `class="…"` attribute, applied by the browser exactly
like the ones in the markup. The verifier still walked `class="…"` only.
Widening one without the other would have meant reporting success over 264
changes it never examined. Both now call the same exported
`dt.class_scopes()`.
*Rule: a verifier's scope must be the converter's scope, by construction —
share the function, do not re-implement the boundary. Any region the tool can
change and the check cannot see is a blind spot that reports green.*

**19. What a verifier should and should not share with the thing it
verifies.** `verify_css_tokens.py` imports the converter's *parser*
(`declarations()`, `colour_slots()`) and deliberately re-implements its two
*decisions*: which theme a rule renders in, and which token a literal is
allowed to become. Sharing the parser is necessary — the first version of
`verify_token_equivalence.py` parsed variants differently from the converter
and produced 128 false positives. Sharing the decisions would be fatal:
mutation-checked, a converter that reads every `.dark` rule as light gets
*caught* rather than agreed with, because the grey in `.dark .badge-offline`
resolved against the light table yields `faint` — a real token holding a real
colour, and the wrong one. Two tests watch the deliberate duplication for
drift.
*Rule: share the mechanics, duplicate the judgement. A verifier that calls the
converter's decision function can only ever confirm it.*

### 2.4 Measurements that lied

This section carries the same weight as the three above it. Over-trusting a
measurement costs exactly what under-measuring costs, and it costs it in a
nastier currency: a false alarm sends you to rewrite working code, and a
sufficiently noisy check gets ignored, which disables it more completely than
deleting it would.

**20. A probe that read the DOM in the same tick it wrote it.** The entire
token scheme rests on the Tailwind Play build accepting `rgb(var(--x) /
<alpha-value>)`. The first probe reported all three test classes unresolved.
That would have sent the approach back to the drawing board. The Play CDN
generates classes from a `MutationObserver`, and the probe read computed
styles in the same tick it injected the element; re-reading after a tick
showed it working perfectly.
*Rule: any runtime that builds lazily needs a tick between the write and the
read. Before believing a negative browser measurement, prove the instrument
can produce a positive.*

**21. Transitions never advance in a non-compositing pane.** The body
background read as light-mode half a second after switching to dark, while
`--c-page` provably resolved to the dark triplet and `.bg-page` was provably
the only matching rule. It was not a bug. In a tab that is not compositing
frames — the normal state for an automated browser pane — a CSS transition
never advances, so `getComputedStyle` returns its START value indefinitely,
and `<body>` carries `transition-colors`. Two hypotheses were wrong before
that one was right: first "the tokens do not work", then "it is
mid-transition, wait longer". Recorded in `verify_token_equivalence.py` and in
HANDOFF §2; inject
`*,*::before,*::after{transition:none!important;animation:none!important}`
before reading anything.
*Rule: when a measurement contradicts two independent pieces of evidence, the
measurement is the thing to doubt. Waiting longer is the reflex, and it is
wrong whenever time is not what is broken.*

**22. 1,407 phantom differences from a scope test that was not stable across
the conversion.** `class_scopes()` decided whether a JavaScript string was a
class list by looking for a colour utility in *arbitrary* form. A string
therefore stops being a class list the instant it is converted, so
`class_scopes(before)` and `class_scopes(after)` returned different counts,
the verifier zipped them and compared body 7 against body 8, and the output
looked exactly like a catastrophic regression. Two fixes, both needed: the
predicate now matches a colour utility spelled either way, and the verifier
fails loudly on a count mismatch instead of silently zipping mismatched pairs.
*Rule: any predicate that defines the comparison scope must be invariant under
the transformation being compared. And never zip two sequences whose lengths
you have not asserted equal — misalignment produces a large, confident, wholly
fictional diff.*

**23. 128 false positives from a verifier blind to something the converter
saw.** The first `verify_token_equivalence.py` did not model variant chains;
the converter did. Fixed by importing `dt._UTIL` and `dt._split_variants`
rather than writing a second parser.
*Rule: same as #19 from the other direction. A parsing difference between tool
and check does not fail safe — it reports phantom differences and misses real
ones at the same time.*

**24. A report that said green gains red.** The first `--by-file` report
answered "some light value on this key", and one class attribute routinely
holds several mutually-exclusive Jinja branches, so `operations.html` read `2x
bg: dark was #10B981 (leaked from light) -> #EF4444`. A green light half
gaining a red dark is nonsense, and it looks exactly like a conversion bug.
The conversion was correct; the reporting was wrong. The leak is now matched
through the token that owns the gained value, and nothing reports as
ambiguous.
*Rule: the code that explains a result is as capable of being wrong as the
code that produces it, and it is usually written faster and tested less. A
nonsense finding is evidence about the reporter first.*

**25. Eleven correct conversions reported as breakages.** Modelling "the"
colour of a class attribute as a single value is wrong whenever the attribute
carries mutually-exclusive branches. `effective()` returns the full **set** of
`(utility, theme, hex@alpha)` an attribute can produce, and compares sets.
*Rule: when a source can branch, the honest model of its output is a set of
reachable values. Collapsing it to one produces both false positives and false
negatives, and the false positives arrive first.*

**26. A check that cried wolf twice, and the noise budget.** A fold onto a
value the class list could **already** reach by another route — `text-muted
dark:text-[#94A3B8]` collapsing onto `#CBD5E1`, which `text-muted` was
producing all along — showed up as a pure loss with nothing gained, and was
reported as a defect. It is not one. The check now compares against the whole
AFTER set. The reasoning recorded in the source is worth quoting in spirit:
two false positives is enough to start reading the output as noise, which is
worse than the check being slightly looser.
*Rule: a check's value is its signal-to-noise ratio, not its strictness. Past
a very small number of false positives the check is off, whatever its exit
code says.*

**27. Hundreds of phantom colour changes from an over-strict invariant.** The
CSS verifier compared the working tree against `convert(HEAD)` and called any
difference a hand-edit. That forbids authoring CSS at all, and the moment the
new scrollbar rules were written the declaration lists stopped lining up and
every comparison after the first insertion was off by one. Replaced: the
property worth asserting across an arbitrary edit is **idempotence** — a
hand-added literal a token could express makes `convert(disk)` differ from
`disk`, which is the mistake actually worth catching.
*Rule: pick the weakest invariant that still catches the failure you care
about. An invariant that forbids legitimate work will be disabled, and it will
be disabled at the worst moment.*

**28. A synthetic click that reached nothing and "proved" a widget was
broken.** The open question was whether `appearance: none` on a number input
removes click-to-increment. An attempt to settle it by clicking a styled
spinner in the browser was inconclusive — the synthetic click did not reach
the **native** spinner either, so it measured nothing at all. Reported as a
result, it would have been a confident answer derived from an instrument that
was not working.
*Rule: run every negative measurement against a known-positive control first.
"The styled one did not respond" is only a finding if the unstyled one did.*

### 2.5 Codemod mechanics that corrupt quietly

**29. `str.replace` edits land on the wrong occurrence.** `#9CA3AF` is both a
light token value (`faint`) and an aliased dark (`muted`), so both spellings
can sit in one class list — and `text-[#9CA3AF]` is a substring of
`dark:text-[#9CA3AF]`. Substituting by content turned `dark:text-[#9CA3AF]
text-[#9CA3AF]` into `dark:text-faint text-[#9CA3AF]`: the wrong theme
tokenised, the other half left a literal. Which utility gets corrupted depends
on the order the matches happen to be in.
*Rule: a codemod edits by `(start, end, replacement)` spans applied
right-to-left. `str.replace` on matched text is correct only when no match is
a substring of another, which is a property you almost never actually have.*

**30. Python's `|` is first-match, not longest-match.** `critical-tint` starts
with `critical`, so `"|".join(TOKENS)` made both the converter and the
verifier read `text-critical-strong` as `text-critical` followed by three
stray characters — silently, and it makes the verifier report a difference
that does not exist. `token_alternation()` sorts longest-first and both
callers use it.
*Rule: any alternation over a name set where one name prefixes another must be
sorted by descending length, and the ordering must live in one helper that
every consumer calls.*

**31. Regex quote pairing desynchronises over mixed HTML and JavaScript —
twice.** Run over a whole template, quote pairing breaks the first time it
meets an HTML attribute or an apostrophe in prose, and *every string after
that point is mispaired*. The first pass converted 286 class strings and
walked past the rest without saying so. Confining the scan to `<script>`
blocks and Jinja expressions removed the HTML from the input and recovered 102
more. Then `// don't let this…` in a comment paired its apostrophe with the
next quote further down the file and hid 158 more; one template's class
strings were invisible while its neighbours converted cleanly, and nothing
reported a problem. The regex was replaced with a small left-to-right scanner
that tracks strings and comments together, because each can contain what looks
like the start of the other — an apostrophe inside a comment, `//` inside a
URL — plus template-literal `${…}` interpolation. Two regression tests, one
per direction. The literal count moved 847 → 583 → 481 → 323 across those
three commits.
*Rule: a partial-coverage scanner fails silently by construction, because the
part it missed produces no output to look at. Report coverage, not just hits —
"453 of the remaining 481 literals are inside a recognised class scope, up
from 263" is the number that exposes the gap.*

**32. Decline rather than guess, and say why in the output.** Where a value
was genuinely ambiguous the tools stop. `#0F172A` is the dark half of `page`,
`raised` and `field`; with no light half there is nothing to resolve it, so
135 dark-only literals were re-spelled and the ambiguous ones left alone — a
guess would look fine today and only surface once the palette pulled the three
apart. The `--brand-*` ramp is declined **whole**: converting only what
resolved made `--brand-violet-lite` render darker than the colour it is a
lighter step of, and collapsed both steps onto one value in dark mode. A
gradient is likewise converted all-or-nothing — `--brand-grad` would otherwise
have run violet → *darker* violet → turquoise, reversing direction halfway.
The CSS conversion landed 133 of 210, and the tool reports a reason for each
of the 77 it left.
*Rule: a partial conversion is fine and a wrong conversion is not, because
nothing downstream catches the wrong one. Make "declined, and here is why" a
first-class output; the residue list is what lets the next person finish the
job or ratchet the count.*

---

## 3. The techniques that worked

**Mutation-check every non-trivial assertion.** Break the code deliberately,
confirm the named test goes red, revert. This is the single highest-yield
habit in the branch, and the recorded deltas are what make the tests credible
six months later:

| Mutation | Result |
|---|---|
| Restore the unconditional alpha collapse | 2 tests; verifier reports 43 |
| Emit the wrong token for a dark-only literal | 107 differences |
| Drop `dark:` so the colour leaks into light | 105 differences |
| Make the pairing key variant-blind | paired-pass scoping test |
| Force every CSS rule to resolve as light | 12 tests, 13 verifier sites |
| Resolve ambiguity to the first candidate | 9 tests, 15 verifier sites |
| Drop alpha in the CSS converter | 3 tests, 7 verifier sites |
| Prune a now-redundant `.dark` rule | declaration-count check |
| Off-scale duration; bare `ease`; ad-hoc shadow | 3 motion tests |
| Drop the spinner's reduced-motion exemption | 1 motion test |
| A third value in the radius scale | caught, once the test read the config |

Note the last row. That mutation is also how finding #12 was found: it passed,
which is the only reason anyone looked at what the test was reading.

**Verify by independent re-derivation, not by re-using the converter's own
answer.** Share the parser (otherwise you get phantom differences, #23);
duplicate the decisions (otherwise the check agrees with the bug, #19). State
in the verifier's docstring which is which — `verify_css_tokens.py` does,
under a heading that says exactly what is deliberately not shared.

**Ratchets instead of unachievable zero-targets.** The spec asked for a test
failing on any new `[#hex]`, with a short allowlist. That shape assumed the
conversion would reach zero. It reached 188 of 5,184 — 96.4% — and every
survivor has a measured reason: 100 light utilities whose colour is not a
light token value (cross-theme oddities plus deliberately theme-invariant
chrome), 60 dark utilities whose colour is not a dark token value, and 28
outside any class scope a regex can identify. An allowlist of forty-odd
colours would assert nothing. A per-file ratchet asserts the property the spec
actually wanted — "without this it regrows" — and unlike zero-plus-allowlist
it is true today.

**Sentinel-delimited generated regions.** See #3. The convention is two
constants in the generator, exactly one occurrence of each in the target, and
a test asserting both the count and the ordering — plus, usefully, that a
recently-added kind of generated content (`--shadow-lg`) is *inside* the
region rather than beside it.

**Separate the mechanical change from the semantic one, in different
commits.** Phase 1a tokenised 5,184 literals holding today's values, so
nothing rendered differently; Phase 1b changed twenty-two token values and
touched no template. If something looks wrong afterwards, the commit
responsible is unambiguous. The same split was applied one level down:
`convert(paired_only=True)` skips utilities with no dark half, so the 561
elements that genuinely gain a dark value land in their own commit with the
list printed. Crucially, a test asserts that the paired pass followed by the
full pass lands exactly where a single full pass lands, over every real
template — the split is a review device, and if it were also a semantic change
the two-commit sequence would ship something the verifier never looked at.

**Measure the running system, with the instrument calibrated first.** Every
phase ends with values read out of the real app in both themes — 22 tokens in
the config, `bg-field` `#FFFFFF`/`#080E1A`, `bg-info/20` composing alpha,
30.4px of right padding on every visible select, `duration-base` resolving
*through* `var(--dur-base)` rather than to a parallel number. Transitions
disabled before every reading.

**Compare sets when the source branches; assert lengths before zipping.** #22
and #25.

**Idempotence as the general-purpose invariant.** It caught the exemption that
never fired (#8), it is the property the CSS verifier fell back to when exact
comparison proved too strict (#27), and it costs one test per codemod. Run it
over the real tree, not over fixtures.

---

## 4. Traps specific to this stack

These are the ones that cost time and are not obvious from the documentation.

**Browser measurement in a non-compositing pane.** Transitions never advance;
`getComputedStyle` returns start values forever. Disable transitions and
animations before reading anything. This is trap number one and it will happen
again.

**`requestAnimationFrame` never fires in that pane either, and the reason is
worse than it sounds.** `document.visibilityState` is `"hidden"`, so the
browser suspends frame callbacks entirely — zero in 600ms, and a chained rAF
never resolves. Any code path that schedules its real work in a rAF simply
does not run, and the page looks *broken* rather than unmeasured.

This cost a wrong conclusion. Verifying a panel fix, the panel appeared not
to open at all: content populated, row displayed, `grid-template-rows` stuck
at `0fr`. That is indistinguishable from a fix that does not work. The open
path sets `1fr` inside a rAF. Shimming rAF to a macrotask — and verifying the
shim itself fired before trusting anything downstream — showed the fix was
correct all along.

The general rule, which is the third instance of it in this document: **when a
measurement says something is broken, test the instrument before you believe
it.** Under-measuring costs a bug. Over-trusting a broken measurement costs a
correct implementation, thrown away and rewritten worse.

**Tailwind Play builds at runtime, from a `MutationObserver`.** Classes do not
exist in the same tick you inject the element.

**Tailwind's sheet is injected after the authored one, so at equal specificity
it wins — and it wins one declaration at a time.** This has now happened three
times on this branch and it is the single most productive trap in the stack.
`padding-right` on `select` lost to `px-2`; measured, every select in the app
carries a `px-*` utility, so the rule applied to none of them and a long
option label would have run under the chevron. Later, `[disabled] { opacity:
.72; cursor: not-allowed }` applied its opacity and silently lost its cursor,
because preflight ships `:disabled { cursor: default }` at equal specificity —
the control looked unavailable and still reported itself as ordinary to the
pointer. Note the shape: a rule that partially applies is far harder to spot
than one that does not apply at all, because the visible half tells you it is
working. The fix in both cases is an element-qualified selector —
`select[class]` and `button[disabled]` are (0,1,1) against a utility's (0,1,0)
— and it wins without `!important`. Both were caught by reading the computed
value, not the rule.

**Tailwind's built-in colour classes are the light half of many pairs.** 275
elements are `bg-white dark:bg-[#…]`; the light side never spells the hex. Any
tool reasoning about light/dark pairs must know the built-ins exist or it will
see orphans everywhere.

**Tailwind's radius keys are `DEFAULT`, `sm`, `md`, `lg`, `xl` … and `rounded`
means `DEFAULT`.** Overriding `sm` and `md` leaves `rounded` and `rounded-lg`
untouched. See #4.

**CSS custom properties and the alpha modifier.** Tokens must be stored as
channel triplets (`R G B`) so Tailwind can compose `rgb(var(--c-x) /
<alpha-value>)`. A bare `var()` holding a hex breaks every opacity modifier
silently, and 329 sites use one. This is a requirement, not a style
preference.

**`currentColor` does not resolve inside an SVG data URI.** The select chevron
therefore hardcodes the hex once per theme, and a test asserts both still
match the brand token — nothing else in the sheet would notice them going
stale.

**`appearance: none` removes behaviour along with appearance, and how much
depends on the widget.** On `<select>` it costs nothing, because the popup
opens from a click anywhere in the control and the arrow is purely decorative.
On `<input type="number">` the spin buttons *are* the increment behaviour, so
removing the appearance removes the feature. `accent-color` does not reach the
spinner at all, and a `filter` chain on `::-webkit-inner-spin-button` tints
the whole spin-button box rather than the arrows — an opaque rectangle beside
the value. The number fields signal focus through their ring instead.

**`color-scheme` at the root beats per-widget patches.** It was set only on
`select`, with spinners separately faked via `filter: invert(1)` (which
inverts the whole box). Declaring the scheme once lets the browser draw dark
spinners, dropdown popups, autofill chrome and text-selection handles itself —
all of which the per-widget patches missed.

**Chromium paints `::-webkit-scrollbar` in the padding box and does not clip
it to `border-radius`.** A card that scrolls has a straight bar running past
its own rounded corners, so it reads as square down that edge. Invisible at
4px, obvious at 16px. The fix is structural: a clipping shell (radius, border,
background, `overflow-hidden`) around an inner element that scrolls. 11
containers needed it. Chromium also draws stepper arrow buttons at each end of
the *page* scrollbar as soon as `::-webkit-scrollbar` is styled at all.

**`:focus` versus `:focus-visible`.** `:focus` matches mouse clicks. Convert
`focus:` to `focus-visible:` — but **not** `peer-focus:` or `group-focus:`,
which style a different element from the one receiving focus (a floating label
reacting to its input); `peer-focus-visible` stops matching in precisely the
case they exist for, and a label that no longer lifts does not read as a focus
bug when you go looking for one.

**Regex over mixed HTML/JS desynchronises on quotes.** See #31. Confine the
scan to `<script>` blocks and Jinja expressions, and track strings and
comments in one pass.

**A Jinja class attribute holds mutually-exclusive branches.** `{% if %}` /
`{% elif %}` inside `class="…"` means the attribute has several possible
colours and only one ever renders. Model it as a set.

**Dark-mode shadows are not light shadows at higher opacity.** A shadow works
by darkening what is behind it, and on a near-black page there is nothing left
to darken. The dark elevation values add a light hairline at the edge to
separate the plane at all, and a test asserts that rather than trusting it.

**A filled turquoise button inverts its label between themes.** White on the
light fill `#0F766E` is 5.47:1; white on the dark fill `#2DD4BF` is 1.86:1,
unreadable. Dark mode takes the near-black `page` at 10.84:1, and a test fails
any filled accent button missing the override.

**`opacity: 0.5` on a disabled control is a contrast bug.** Measured, it drags
a 7.6:1 label to roughly 3.4:1 — under AA, so the state where the reader most
needs to read the label is the state where they cannot. 0.72 keeps `muted` on
`card` above 4.5:1 and still reads as unavailable. The general form: any
global opacity applied for emphasis is a contrast change, and nothing in the
toolchain will tell you so unless a test measures the composited result.

**A palette can be a pre-existing defect that the conversion faithfully
reproduces.** Measured on the pre-flip twelve tokens, text on card, six of
eight failed AA — `faint` at 1.93:1 in dark mode was effectively invisible,
and the 62 sites that already spelled the pair out had been that way all
along. Tokenising 77 more elements onto it made the defect *visible* without
creating it. The C-Ink flip then took the worst pair to 4.76:1 with a test
that fails the build below 4.5.

---

## 5. Candidate skills and automation

Honest assessment of what is worth mechanising.

**Worth building — mutation-check as a workflow.** The habit is already
universal in this branch but entirely manual. A small harness that takes
`(patch, expected-failing-tests)`, applies the patch, runs the target tests,
asserts red, and reverts would let the mutations in §3's table live as
executable fixtures rather than as prose in commit messages. It also solves
the decay problem: today, nothing notices when a mutation stops being caught.
Start with the eleven mutations already recorded in §3; do not attempt general
mutation testing, which is a much larger and much lower-yield tool.

**Worth building — the ratchet-test template.** It is three tests and a dict,
and the third one (baseline must come down when the count does) is the one
people forget. Copy `tests/test_design_tokens.py` §guardrail verbatim for any
"this must not regrow" property: TODO counts, `# type: ignore`, bare `except`,
inline styles, untranslated strings.

**Worth building — the generated-block convention.** Two constants, a
`render_*()` function, and a three-line test asserting exactly one sentinel
pair in the right order containing a known recent addition. Cheap, and it
converts a whole class of "the generator silently ate my edit" bugs into a
test failure. Already proven here; just needs writing down as a pattern rather
than as one instance.

**Worth building, and tiny — the scope-invariance assertion.** Three lines:
`assert len(scopes(before)) == len(scopes(after))`, fail loudly, never zip
unequal sequences. It would have turned 1,407 phantom differences into one
clear message. Applies to any before/after comparison that segments its input.

**Worth building, and tiny — the browser-measurement preamble.** Inject the
transition/animation kill switch and force a tick before reading anything. It
is currently prose in two docstrings and HANDOFF §2, which means it will be
rediscovered. It should be a function.

**Worth building — the codemod checklist.** Span edits right-to-left;
longest-first alternation; idempotence test over the real tree; report the
residue with reasons; coverage as a first-class number. Five items, all of
them learned the hard way above, none of them specific to colour.

**Not worth building — a generic converter framework.** Five migrations ran on
this branch (colours, focus variants, radii, CSS tokens, brand roles) and
their pairing rules had almost nothing in common. The shared value is the
checklist, not the code.

**Not worth building — a general "independent re-derivation" harness.** Which
parts to share and which to duplicate is a judgement about where the converter
can be wrong, and that judgement is the whole content. What can be mechanised
is the *documentation* requirement: a verifier that does not state what it
deliberately does not share with the thing it verifies should not be trusted.

---

## Appendix — figures that look contradictory and are not

Four sets of numbers in the history will make you think you have found a
mistake in it. You have not.

**870 / ~570 / 561 unpaired utilities.** 870 is the count of light utilities
with no `dark:` counterpart. ~570 was the number of dark-mode values the full
pass would add, measured across all 29 templates at the first commit. 561 is
the same measurement over the 27 templates still unconverted when the split
landed. The gap between 870 and 561 is the utilities whose colour maps to no
token, which are left alone.

**135 / 200 dark-only literals.** 200 is roughly how many dark-only utilities
exist; 135 is how many were re-spelled, the remainder being ambiguous values
the tool declines. The 200 in the test docstring is an approximation and the
135 in the commit is the measured conversion.

**286 of 629, and 337 missed.** The JavaScript scan commits count class
*strings* in one place and colour *literals* in another, and the two do not
sum cleanly (629 literals, 286 strings converted, 337 quoted as missed). Trust
the literal trail, which is exact and self-consistent: 847 → 583 → 481 → 323 →
188.

**746 / 750 radius uses.** Two counts of the same population taken several
commits apart. Nothing in between obviously adds four rounded elements, so
treat the discrepancy as unexplained rather than as one of the numbers being
wrong — the conclusion each supports (12 of them governed by the override)
does not depend on it.
