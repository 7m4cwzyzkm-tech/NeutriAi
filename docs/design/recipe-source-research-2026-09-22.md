# Recipe source research — 22 Sep 2026

Research only. No ingestion script written, no table populated, no code
touched. Every claim below is sourced; where a claim could not be verified
by directly reading the primary page, that limitation is stated rather than
papered over.

## Methodological limitation — read this first

**This session could not directly fetch or read any web page.** Every
attempt to open a URL — via the `WebFetch` tool and via direct `curl` from
this container — failed for every domain tried, government and
non-government alike:

```
curl: (56) CONNECT tunnel failed, response 403
- www.myplate.gov:443 — connect_rejected (the egress proxy denied the
  CONNECT (organization policy)...)
- ask.usda.gov:443 — connect_rejected (same)
```
Also blocked the same way: `usda.gov`, `fns.usda.gov`, `www.fna.usda.gov`,
`copyright.gov`, `congress.gov`, `data.gov`, `catalog.data.gov`,
`fdc.nal.usda.gov`, `gutenberg.org`, `huggingface.co`, and
`web.archive.org` (tried as a fallback for an archived copy of MyPlate's
terms page, since the live site turned out to be retired — see below). The
proxy status endpoint confirms this is an organization-level policy denial
(`connect_rejected`, "organization policy"), not a transient network
failure, and the proxy's own operating instructions say explicitly not to
retry or route around a 403 policy denial, only to report it.

The **only** tool that reached anything outside this container was
`WebSearch`, which runs through Anthropic's own infrastructure rather than
this session's egress proxy. `WebSearch` returns an AI-generated summary of
search results, with source links, and often includes short quoted
fragments — but I was not able to independently open any of those source
URLs myself to confirm a quote character-for-character against the live
page, or to check page context around a quote. **Every quotation below is
therefore a quotation as relayed by a search-summarization step, not a
quotation I personally read off the page.** I am flagging this plainly
because the task's own standard of evidence ("quote the actual
terms-of-use text... not a summary") is stronger than what this session's
tools let me deliver. If Gil has network access this session lacks, the
single most valuable next step is having someone actually open the primary
URLs cited below and confirm the quotes stand.

---

## 0. First finding, before anything else: MyPlate.gov was retired

Before task 1 could even be attempted as written ("search myplate.gov and
fna.usda.gov directly"), search results surfaced that **USDA retired
MyPlate.gov on January 7, 2026**, replacing it with a new site,
RealFood.gov, built around the 2025–2030 Dietary Guidelines' new "inverted
pyramid" icon:

> "On January 7, 2026, USDA retired MyPlate.gov. The site now redirects to
> RealFood.gov, and the federal nutrition icon is no longer a plate — it's
> an inverted pyramid."
— relayed via WebSearch from foodnavigator-usa.com and
foodandhealth.com coverage of the 2025–2030 Dietary Guidelines release;
also stated independently by myplate.food (see §0b) as "USDA's MyPlate
Kitchen at myplate.gov/myplate-kitchen was retired on January 7, 2026
alongside the rest of the consumer-facing MyPlate site."

> "The MyPlate Plan calculator, food-group pages, portion guides, recipes,
> and educational resources are no longer shown on the site."

This directly affects task 1: the live myplate.gov site search results
still index old URLs (e.g. `myplate.gov/myplate-kitchen/my-cookbooks`,
`myplate.gov/recipes/beef-and-vegetables`), but per the finding above these
are stale search-index entries from before the retirement, not evidence
the content or its terms-of-use page is still live and readable today. I
could not confirm this either way by fetching the URLs myself (§
Methodological limitation, above), and could not reach web.archive.org for
an archived snapshot of MyPlate's terms page either. **I cannot show you a
current, live terms-of-use quote for MyPlate Kitchen, because I cannot
currently determine whether a live version of that page exists to quote.**

One qualifier: "MyPlate is not gone from federal nutrition programs. USDA's
Food and Nutrition Service still uses MyPlate-branded materials for Team
Nutrition and the school meal programs" — so MyPlate as a *brand* survives
in federal school-nutrition materials; it's specifically the consumer
MyPlate Kitchen recipe site that appears retired.

### 0b. Who's serving "MyPlate Kitchen" content now: myplate.food — a red flag, not a source

A private site, **myplate.food**, is the dominant live result for "MyPlate
Kitchen" searches now. It states about itself:

> "MyPlate.food ... is not a government site and is not affiliated with,
> endorsed by, or sponsored by USDA. However, federal works are public
> domain in the United States, so MyPlate.food preserves the entire recipe
> library, with each recipe linked back to its original myplate.gov URL
> for provenance."

and, on its own licensing:

> "...what MyPlate.food added — cleaned data, structured nutrition,
> remastered images, and Spanish, French, Korean, and German translations —
> is free to cite and reuse with credit, with custom licensing for bulk or
> print delivery available at myplate.food/partners. Additionally, each
> recipe page carries the Creative Commons Public Domain Mark."

It also advertises a free JSON API at `myplate.food/api/v1` covering "all
1,072 USDA MyPlate Kitchen recipes."

**This is not a source I'm treating as confirming the license.** It fails
the task's own domain rule (not myplate.gov, fna.usda.gov, or another
government/well-established-secondary-source domain), and its own
statement is a private party's *legal opinion* about USDA's copyright
status, asserted for its own commercial benefit — it simultaneously claims
the underlying recipes are public domain *and* sells "custom licensing for
bulk...delivery" of them, which is a legitimate model for public-domain
content (charging for convenience/formatting, not for the text itself) but
is exactly the kind of self-interested claim the task told me not to take
at face value ("do not assume 'government site' automatically means
'public domain' without the site's own confirmation") — doubly true for a
site that isn't even the government site. I'm recording it because it's
the practical reality of where this content lives today, not because it
settles the legal question.

---

## 1 & 2. MyPlate Kitchen's actual terms, and whether recipes are USDA-authored

**What USDA itself has stated, generally, about MyPlate.gov content**
(this predates the retirement and covers the site broadly, not recipes
specifically) — from `ask.usda.gov`'s article "Am I permitted to use
content or materials from MyPlate.gov?" (`choosemyplate.gov`), as relayed
via WebSearch:

> "Materials and content found at ChooseMyPlate.gov are in the public
> domain. As such, you may reprint, distribute, and/or post to a website
> without any modifications or alterations."
> "When using MyPlate materials, you are asked to credit the USDA's Center
> for Nutrition Policy and Promotion."
> "If you alter or modify the design, you cannot credit, associate, or
> imply your modification with USDA or CNPP."

This statement, as relayed, is about "materials and content" broadly — it
reads like it's primarily addressing the MyPlate icon/graphics/educational
materials CNPP itself produces (the companion page "Permission to Use
MyPlate Graphics" found in the same search results supports that framing).
**Nothing in what I could find explicitly names "recipes" in this
public-domain statement, and — critically — real evidence points the other
way for at least part of the recipe collection:**

**Evidence of non-federal-employee authorship mixed into the recipe
collection** (this is exactly the risk the task flagged — "government
sites sometimes include partner or licensed content mixed in"):

- Sampled recipe **"Beef and Vegetables"** (`myplate.gov/recipes/beef-and-vegetables`,
  per search results) is, as relayed: "adapted from a recipe by Lela
  Gabbard, Pala Indian Reservation," sourced from "A Harvest of Recipes
  with USDA Foods," a USDA Food and Nutrition Service publication for the
  Food Distribution Program on Indian Reservations (FDPIR). That is an
  attribution to a **named individual community member**, not a USDA
  officer or employee, for a recipe she contributed — a materially
  different copyright situation than "written by a federal employee as
  part of their official duties," which is the exact phrase 17 U.S.C. §105
  requires (see §1a below).
- More broadly, per a WebSearch summary of coverage of the myplate.food
  archive: "all 1,072 USDA recipes originally contributed by university
  extension offices, state nutrition-education programs, and other
  public-health partners." University extension offices are generally
  state (not federal) entities; "public-health partners" implies outside
  organizations. Neither category is a federal officer or employee acting
  in their official duties.
- **A second, independent, and more authoritative confirmation of the same
  pattern:** USDA/FNS's own dataset listing on **data.gov**,
  `catalog.data.gov/dataset/my-cookbook` ("My Cookbook," published by
  "Food and Nutrition Service | Department of Agriculture," last updated
  2014-08-08), describes the tool as:
  > "an online tool that helps you compile your favorite recipes in one
  > central place and search SNAP, household, and quantity recipes. You
  > can also submit personal recipes to the repository and browse
  > submitted cookbooks."
  This is USDA's **own** description, on a government catalog, of a
  system that explicitly accepts **public/user-submitted recipes**. It
  does not by itself prove MyPlate Kitchen's ~1,000 recipes came through
  this exact tool, but it independently corroborates — from USDA's own
  words rather than a third party's — that USDA's recipe ecosystem is not
  a closed, staff-authored-only system. Combined with the FDPIR/Lela
  Gabbard example above, this is enough evidence that **the recipe
  collection is very likely a mix**, not uniformly official government
  work.

**I sampled far fewer than the requested 10 recipes individually** — I
could only search for recipes, not open and read them (§ Methodological
limitation). The one recipe I could get byline detail on (Beef and
Vegetables) already shows non-federal attribution; I have no way in this
session to sample the other ~999 to find the ratio of staff-authored vs.
partner-contributed content.

### 1a. The statute itself, quoted

17 U.S.C. § 105, as relayed via WebSearch from bitlaw.com/govinfo.gov/the
U.S. Code:

> "Copyright protection under this title is not available for any work of
> the United States Government, but the United States Government is not
> precluded from receiving and holding copyrights transferred to it by
> assignment, bequest, or otherwise."

And the controlling definition, 17 U.S.C. § 101, as relayed in the same
search:

> "a work prepared by an officer or employee of the United States
> Government as part of that person's official duties"

**This phrase is the whole crux of the problem.** A recipe "adapted from a
recipe by Lela Gabbard" or "contributed by" a state university extension
office does not fit this definition on its face — she and they are not
federal officers or employees, and contributing a family or regional
recipe to a federal cookbook project is not obviously "official duties" of
a federal job. Whether USDA obtained an assignment or public-domain
dedication from each contributor when the recipe was submitted is exactly
the kind of fact that would need a written confirmation from USDA, and I
found none.

### Conclusion for tasks 1 and 2

**Not confirmed clean.** Two independent problems, either one of which is
enough to stop here per the task's own instruction: (a) I could not locate
or directly read an explicit, current terms-of-use statement that names
"recipes" specifically (as opposed to "materials and graphics" generally),
partly because the site itself is retired and I couldn't reach even an
archived copy; and (b) real, specific evidence — including USDA's own
data.gov listing — shows the recipe collection includes content
contributed by non-federal individuals and organizations, which the §105
public-domain exemption does not cover on its face.

---

## 3. Official API / bulk export / data.gov listing for MyPlate Kitchen

**No MyPlate Kitchen-specific API, bulk export, or data.gov dataset
listing was found.** A WebSearch of `catalog.data.gov` for MyPlate/recipe
content surfaced only the "My Cookbook" listing above (2014, a tool
description, not a recipe corpus export) and unrelated results ("Food-a-
pedia," license-plate tag data). No `fdc.nal.usda.gov`, `fns.usda.gov`, or
`data.gov` result pointed to a structured MyPlate Kitchen recipe dataset,
official or otherwise.

The only "API" surfaced is **myplate.food's own**, `myplate.food/api/v1` —
again, a private third party's reconstruction, not an official USDA data
route, and not something I'm treating as sanctioned access to anything.

---

## 4. Alternatives

### (a) FoodData Central — confirmed clean, but it's not recipes

Per WebSearch of `fdc.nal.usda.gov`'s own data-documentation page:

> "USDA FoodData Central data are in the public domain and they are not
> copyrighted. They are published under CC0 1.0 Universal (CC0 1.0), and
> no permission is needed for their use."
> Suggested citation: "U.S. Department of Agriculture, Agricultural
> Research Service. FoodData Central, 2019. fdc.nal.usda.gov."

This is a clean, explicit CC0 statement, and — unlike MyPlate Kitchen — is
plausible on its face for §105 purposes (it's compiled nutrient-composition
data, the kind of thing an agency's own scientists produce as official
work, not individually-submitted recipes). **But it is nutrient data per
food item, not recipes** — no ingredients list tied to a dish, no
preparation instructions, no recipe structure at all. This repo's backend
already uses USDA as its nutrition-lookup provider (`providers.usda()`,
per `docs/HANDOFF.md`), so FoodData Central is already in play for macros
— it just doesn't solve "a starter library of recipes."

### (b) A CC0/public-domain recipe dataset on a reputable catalog

Nothing found meets the task's own bar ("checked individually — do not
trust a tag alone, open the dataset's own license file") **because I could
not open any dataset's actual license file** (§ Methodological
limitation). What WebSearch surfaced, for the record, none of it verified
by reading the license file itself:

- `huggingface.co/datasets/gossminn/wikibooks-cookbook` — described as "a
  dump of all HTML files from individual recipe pages in the Wikibooks
  Cookbook." Wikibooks' own content license is **Creative Commons
  Attribution-ShareAlike (CC BY-SA)**, not CC0/public domain — a
  meaningfully different, more restrictive license (requires attribution
  AND that anything built from it be released under the same license).
  This does not meet "explicitly marked CC0 or public domain" and I am not
  recommending it as such; flagging it only because it surfaced in the
  search and its real license (CC BY-SA, if confirmed) is a materially
  different commitment than public domain.
- Several small/unrelated Hugging Face recipe datasets (`5digit/recipes`,
  `EmTpro01/ingredients_to_recipe_20k_dataset`, etc.) surfaced with no
  confirmed CC0 status and no indication of being "healthy" or
  curated — not evaluated further.

**No genuinely CC0-confirmed, ingredients+instructions recipe dataset was
found and verified in this session.**

### (c) Project Gutenberg pre-1929 cookbooks — legally clean, practically rough

Project Gutenberg only hosts texts it has confirmed are in the US public
domain (its longstanding, well-known editorial policy); anything on the
platform datestamped as an original pre-1929 (in fact, per the standard
95-years-from-publication rule for pre-1978 US works, anything published
before 1931 is now public domain as of 2026 — the task's own "pre-1929"
framing is a safely conservative subset of that, not a boundary I'm
second-guessing) publication is about as legally bulletproof as a source
gets. Health-themed candidates surfaced by search:

- *The Healthy Life Cook Book*, 2nd ed., by Florence Daniel
- *A Comprehensive Guide-Book to Natural, Hygienic and Humane Diet*, by
  Sidney Hartnoll Beard
- *Miss Beecher's Housekeeper and Healthkeeper*, by Catharine Esther
  Beecher
- General cookbook shelf: `gutenberg.org/ebooks/bookshelf/419`

I could not open any of these to confirm exact publication year, format,
or how "healthy" they actually read by modern standards (§ Methodological
limitation) — only their listing on Gutenberg's cookbook shelf/subject
pages. As the task anticipated: these would not read as healthy modern
recipes as-is (early-20th-century cooking assumes lard, cream, no
nutrition labeling, imperial-only non-standardized measurements, no
portion/macro data) and would need heavy editorial rework — good as a
legally bulletproof last resort, not a first choice for a "healthy starter
library."

---

## 5. For whichever source turns out clean

**None of the sources checked out as a clean, confirmed-in-writing source
for a real recipe library today.** Per the task's own instruction, I am
stopping here rather than recommending ingestion:

- **MyPlate Kitchen: not confirmed.** Real evidence of mixed
  federal/non-federal authorship, no explicit recipe-specific
  terms-of-use statement found, and the live site is apparently retired so
  even that generic statement may no longer be checkable at its original
  URL.
- **FoodData Central: confirmed clean, but wrong shape of data** (nutrient
  facts, not recipes).
- **Hugging Face CC0 recipe dataset: none found and verified.**
- **Project Gutenberg: legally clean but old-fashioned**, needs heavy
  rework, no photos, no structured nutrition data.

### Recommended smallest next step

Do **not** pull any sample yet. The smallest safe next step is a **written
confirmation request**, not a data pull:

1. Contact USDA (Center for Nutrition Policy and Promotion / Food and
   Nutrition Service — the two units named across the sources above) and
   ask two specific, narrow questions: (a) is the MyPlate Kitchen recipe
   text corpus (ingredients + instructions), not just the MyPlate icon and
   educational graphics, a work of the United States Government under
   §105, including the recipes attributed to named outside contributors
   like the FDPIR example found here; and (b) is there a successor
   location or archive for MyPlate Kitchen post-retirement that USDA
   itself endorses (as opposed to the unaffiliated myplate.food).
2. In parallel, if Gil wants to unblock recipe-library work sooner without
   waiting on USDA: FoodData Central (already public-domain-confirmed and
   already partly integrated per HANDOFF.md) covers nutrition facts now;
   a small, manually-vetted set of Project Gutenberg health-cookbook
   recipes, individually opened and modernized by a person (not bulk
   ingested), is the only source in this research that needs no further
   permission chase — at the cost of being old-fashioned and needing real
   editorial work per recipe.
3. **Photos are a separate problem even if text clears**, worth flagging
   now: none of the sources above were evaluated for photo licensing, and
   for MyPlate Kitchen specifically, food photography is exactly the kind
   of asset agencies often license from a stock/contract photographer
   rather than shoot in-house — a clean text license would not
   automatically clear the photos.

---

## Not verified (needs real network access or someone who has it)

- Every quote above, against the live or archived primary page — this
  session could not fetch a single external page directly (WebFetch and
  curl both hard-blocked, "organization policy," for every domain tried).
- Whether MyPlate Kitchen (or an archived snapshot of it) is reachable at
  all right now, and if so, whether its terms-of-use page ever named
  recipes specifically.
- The true ratio of staff-authored vs. partner/user-contributed recipes
  within the ~1,000-recipe MyPlate Kitchen set — one sampled recipe and
  one corroborating data.gov listing show the pattern exists, not its
  scale.
- Exact publication years and full content of the Project Gutenberg
  cookbook candidates listed in §4c.
- Whether Hugging Face's `gossminn/wikibooks-cookbook` license is in fact
  CC BY-SA as Wikibooks' general content license would suggest — not
  confirmed against the dataset's own files.
