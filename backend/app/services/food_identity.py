"""What the user says a food actually is, kept for next time.

WHY THIS EXISTS

The vision model now says whether it RECOGNISED a dish or only described what
it could see. Photographed twice a minute apart, one plate of rajas came back
as "creamy chicken" and then "creamy mushroom sauce" -- 315 g against 186 g,
energy 40% apart, on geometry that was within 12% both times. The name is not
a label: it picks the density, the height prior and the nutrition lookup.

So when the person corrects what a food is, their answer is kept HERE,
against whatever the model had called it. The next time the model calls that
food the same thing for that person, their name is used instead.

WHEN IT OVERRIDES THE MODEL -- and why confidence is no longer a gate

  It used to run only on dishes the model had admitted it could not name
  ("described" / "unsure"), and never on one it NAMED. That bounded a bad
  alias to zero confident identifications -- and it also meant a confident
  WRONG identification could never be fixed. Gil corrected teriyaki beef that
  the model kept calling scallops, and it came back as scallops every time,
  because the model was sure (25 Sep 2026).

  So the gate is now the person, not the model. A name they have given ONCE
  is only offered, whatever the model said; a name they have given the same
  way twice (MIN_SAMPLES_TO_APPLY) is applied, including over a confident
  identification. The model's confidence is one look at one photo. The same
  person, looking at their own plate, correcting the same mistake the same
  way twice, is repeated first-hand evidence about food they cooked or
  bought -- stronger than the model's self-report, and exactly the case the
  old gate threw away. The cost, stated: a wrong alias entered twice now
  renames a food the model had right. That is bounded to this person's own
  scans, and `remember` resets the count to 1 the moment they answer
  differently, so one further correction turns it back into an offer.

WHAT IT DELIBERATELY DOES NOT DO

  It is not a food catalogue. There is no curated list of dishes per cuisine,
  because a fixed list works for the foods on it and quietly makes the app
  worse for everyone whose cooking is not on it. What is stored is what THIS
  person eats and has named, which is unbounded and steers nobody.

  It never overrides the person. A stored alias is what they said last time,
  not a rule about the world.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog

from ..db import service

log = structlog.get_logger()

# Words that carry no identity. These are exactly the words a model reaches for
# when it is describing rather than recognising -- "creamy", "dish", "sauce",
# "with" -- so leaving them in would match a creamy soup to a creamy curry.
STOPWORDS = frozenset({
    "and", "with", "the", "cooked", "fresh", "homemade", "style", "served",
    "plate", "plated", "dish", "food", "meal", "portion", "side", "piece",
    "pieces", "chunk", "chunks", "slice", "slices", "mixed", "assorted",
    "creamy", "cream", "saucy", "sauce", "sauced", "topped", "covered",
    "unidentified", "unknown", "possibly", "probably", "some", "type",
})

# How many identity-carrying words two names must share before one is taken to
# mean the other. One is not enough: "creamy mushroom sauce" and "mushroom
# soup" share a word and are different meals.
MIN_SHARED_WORDS = 2

# Below this an alias is a single unconfirmed opinion. It is still offered to
# the person; it is not applied for them.
MIN_SAMPLES_TO_APPLY = 2


def tokens(name: str) -> frozenset[str]:
    """The identity-carrying words in a food name.

    Plurals are folded because a model writes "mushrooms" one run and
    "mushroom" the next, and those must not be two different foods.
    """
    words = re.findall(r"[a-z]+", str(name or "").lower())
    out = set()
    for w in words:
        if len(w) < 3 or w in STOPWORDS:
            continue
        # Fold plurals, carefully. Stripping "es" from everything turns
        # "chiles" into "chil" while "chile" stays "chile", and then the same
        # dish written two ways stops matching itself. "es" only comes off
        # where English actually put it there: after s, x, z, o, ch or sh.
        if len(w) > 3 and w.endswith("es") and (
                w[:-2].endswith(("s", "x", "z", "o", "ch", "sh"))):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in STOPWORDS:
            out.add(w)
    return frozenset(out)


def looks_like(a: str, b: str) -> bool:
    """Do these two names describe the same food?

    Deliberately conservative. A false match puts someone else's dinner in
    this meal; a miss just asks the question again.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    if len(ta & tb) >= MIN_SHARED_WORDS:
        return True
    # One shared word is only enough when it is ALL either name has to say --
    # "creamy mushroom sauce" and "creamy dish with mushrooms" both reduce to
    # {mushroom}, which is the same food described twice. "mushroom soup" also
    # shares that word and carries another, so it is a different food and must
    # not match.
    #
    # An earlier version accepted any shared word of eight letters or more, on
    # the theory that long words are distinctive. "mushroom" is eight letters,
    # and it matched mushroom soup to a plate of rajas. Length is not
    # distinctiveness.
    return ta == tb


def suggest(described_as: str, learned: list[dict]) -> dict | None:
    """The person's own name for this food, if they have given one.

    `learned` is their stored aliases: {"described_as", "actual_name",
    "samples"}. The best match wins, and best means most-confirmed, so a name
    they have given twice beats one they gave once.
    """
    if not described_as or not learned:
        return None
    hits = [row for row in learned
            if isinstance(row, dict) and looks_like(described_as, row.get("described_as", ""))]
    if not hits:
        return None
    return max(hits, key=lambda r: (_int(r.get("samples")), len(tokens(r.get("described_as", "")))))


def apply_to(det: dict, learned: list[dict]) -> str | None:
    """Rename an item to what the person has called it before.

    Returns a note for the user, or None when nothing changed. Mutates `det`.

    Runs whatever the model's `identification` says -- a confidently named
    item included (see the module docstring for why). The guard that matters
    is the sample count further down: one answer is surfaced as an offer and
    changes nothing; only the same answer given twice renames the food.
    """
    if not isinstance(det, dict):
        return None
    described = str(det.get("name") or "")
    hit = suggest(described, learned)
    if not hit:
        return None
    actual = str(hit.get("actual_name") or "").strip()
    if not actual or actual.lower() == described.strip().lower():
        return None

    if _int(hit.get("samples")) < MIN_SAMPLES_TO_APPLY:
        # One unconfirmed answer is an offer, not a fact.
        return (f"you told us once that this is {actual} — we have left it as "
                f"{described} for now. Correct it again and we will apply it "
                f"from then on.")

    det["name"] = actual
    det["identification"] = "named"
    det["identified_by"] = "user"
    log.info("food_identity_applied", described=described[:40], actual=actual[:40])
    return (f"logged as {actual}, which is what you called this the last "
            f"{_int(hit.get('samples'))} times. It looked like {described} "
            f"to the camera.")


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Storing it
# ---------------------------------------------------------------------------
#
# Both halves live here on purpose. portion_learning wrote heights for weeks
# that nothing read, and the reason it went unnoticed is that the write and the
# read lived far enough apart that neither looked incomplete. See
# test_wiring.test_a_table_we_write_is_a_table_something_reads.

TABLE = "food_aliases"


def aliases_for(user_id: str) -> list[dict]:
    """This person's own names for the foods the camera cannot place."""
    if not user_id:
        return []
    try:
        resp = (service().table(TABLE)
                .select("described_as,actual_name,samples")
                .eq("user_id", user_id).limit(500).execute())
        rows = getattr(resp, "data", None) or []
        return [r for r in rows if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001
        log.warning("food_aliases_read_failed", error=str(exc)[:200])
        return []


def remember(user_id: str, described_as: str, actual_name: str,
             source: str = "typed") -> None:
    """Keep what the person said this food is. Never raises.

    Saving the meal is the user's action; learning from it is ours. A failure
    here must not cost them the correction they just made.
    """
    described = str(described_as or "").strip().lower()
    actual = str(actual_name or "").strip()
    if not user_id or not described or not actual:
        return
    if described == actual.lower():
        return                      # they kept the name; there is nothing to learn
    try:
        sb = service()
        existing = (sb.table(TABLE).select("id,actual_name,samples")
                    .eq("user_id", user_id).eq("described_as", described)
                    .limit(1).execute())
        rows = getattr(existing, "data", None) or []
        if rows:
            row = rows[0]
            # Same answer again: they are more sure. A DIFFERENT answer resets
            # the count -- the new one is what they mean now, and it has to
            # earn its way back up to being applied automatically.
            same = str(row.get("actual_name") or "").strip().lower() == actual.lower()
            sb.table(TABLE).update({
                "actual_name": actual,
                "samples": (_int(row.get("samples")) + 1) if same else 1,
                # An explicit instant rather than the string "now()".
                # Postgres does parse 'now()' -- checked against 16.13, it
                # ignores the parens and returns the current time -- but that is
                # leniency in the datetime parser, not a contract. A stricter
                # driver or a future release would store nothing useful and
                # nothing here would notice.
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row.get("id")).execute()
            log.info("food_identity_learned", described=described[:40],
                     actual=actual[:40], agreed=same, source=source)
            return
        sb.table(TABLE).insert({
            "user_id": user_id, "described_as": described,
            "actual_name": actual, "samples": 1,
        }).execute()
        log.info("food_identity_learned", described=described[:40],
                 actual=actual[:40], agreed=False, source=source)
    except Exception as exc:  # noqa: BLE001
        log.warning("food_identity_write_failed", error=str(exc)[:200])


def learn_from_correction(user_id: str, original_items: list[dict],
                          corrected_items: list, source: str = "typed") -> None:
    """Record every rename in this correction. Never raises.

    Keyed on `source_index` -- which detected item this correction edits -- for
    the same reason portion_learning is: the name is exactly what changed, so it
    cannot also be the key, and list position cannot tell a rename from a food
    the user ADDED. An item without one is an addition and teaches nothing.

    `source` ("typed" by default, "weighed" from a tester) is recorded in the
    food_identity_learned log line only; it changes nothing that is learned.
    """
    try:
        originals = [o for o in (original_items or []) if isinstance(o, dict)]
        for item in corrected_items or []:
            idx = getattr(item, "source_index", None)
            if not isinstance(idx, int) or not (0 <= idx < len(originals)):
                continue
            was = str(originals[idx].get("name") or "")
            now = str(getattr(item, "name", "") or "")
            if was and now and was.strip().lower() != now.strip().lower():
                remember(user_id, was, now, source=source)
    except Exception as exc:  # noqa: BLE001
        log.warning("food_identity_learning_failed", error=str(exc)[:200])
