"""``canonical_key`` — the ONE normalisation the whole catalogue keys on.

Two implementations exist on purpose, one per repo: this one (Python, the
worker) and ``src/utils/catalogue/key.ts`` (TypeScript, the app and console).
They must agree on every input, because ``topics.canonical_key`` and
``topic_aliases.normalized`` are written by both sides and matched by equality
in SQL: a harvest that normalises "Cells" one way and a console that
normalises it another way is a candidate that never finds its topic. Both
suites therefore run the SAME truth table (``tests/fixtures/
catalogue_key_cases.json``, sha256-pinned, mirroring the premium-voice table).

The rule, in order — stdlib only, no locale, no dictionary:

  1. NFKD, drop every Unicode mark (categories Mn, Mc, Me — the app's \p{M}), lower-case
     ("Réfraction" → "refraction", full-width digits → ASCII);
  2. ``&`` → " and " ("Acids, Bases & Salts" reads as its spoken form);
  3. every run of characters outside [a-z0-9] → ONE "_";
  4. strip leading/trailing "_";
  5. drop ONE leading article token ("the", "a", "an") — "The Cell" and
     "Cell" are the same topic;
  6. per "_"-separated token, fold a simple plural: drop a final "s" when the
     token is purely alphabetic, at least 4 letters long, and the letter
     before that "s" is not "s" and not one of a/i/o/u — i.e. a consonant or
     an "e". "cells" → "cell", "atoms" → "atom", "laws" → "law", "bases" →
     "base", "forces" → "force"; "glass" (ss), "gas" (a), "bus" (u), "this"
     (i), "its" (3 letters) and a lone "s" ("newton_s_law") stay; "7bs" in
     the Cambridge code "7Bs.01" has a digit and stays — codes are never
     folded.

     Why "e" is foldable when the other vowels are not: "bases"/"forces"/
     "waves" are the plurals a science syllabus is full of, and the one
     casualty ("gases" → "gase", not "gas") still maps the same way from both
     repos, which is all a key has to do. The rule is the app's
     (src/utils/catalogue/key.ts, singularToken) — mirrored, not re-derived.

Deliberately naive. It is not a stemmer and must not become one: the point is
that two people typing the same title get the same key, not that "mice" meets
"mouse". Aliases exist for the rest.
"""

from __future__ import annotations

import re
import unicodedata

_ARTICLES = frozenset({"the", "a", "an"})
# The letters before a final "s" that block the fold: "ss" is not a plural
# ("glass"), and a/i/o/u + s is a singular far more often than not ("gas",
# "this", "chaos", "bus"). "e" is deliberately NOT here — see rule 6.
_NO_FOLD_BEFORE_S = frozenset("saiou")
_NON_KEY = re.compile(r"[^a-z0-9]+")
_ALPHA = re.compile(r"^[a-z]+$")
_MIN_FOLD_LEN = 4


def singular_token(token: str) -> str:
    """Rule 6 for one token. Pure; see the module docstring for the cases.
    Mirrors the app's ``singularToken`` and is exported for the tests."""
    if len(token) < _MIN_FOLD_LEN or not _ALPHA.match(token) or not token.endswith("s"):
        return token
    if token[-2] in _NO_FOLD_BEFORE_S:
        return token
    return token[:-1]


def canonical_key(text: object) -> str:
    """Normalise a topic title / heading / alias to its catalogue key.

    Accepts anything (None, numbers, pydantic strings) and always returns a
    ``str`` — possibly "" for input that carries no key material at all
    (whitespace, punctuation, a bare article). Callers treat "" as "no key".
    """
    if text is None:
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    # Every mark category, not only Mn: a spacing or enclosing mark between
    # two Latin letters must vanish (app: \p{M}), not become a separator.
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("M"))
    s = s.lower()
    s = s.replace("&", " and ")
    s = _NON_KEY.sub("_", s).strip("_")
    if not s:
        return ""
    tokens = s.split("_")
    if tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]
    return "_".join(singular_token(t) for t in tokens)


__all__ = ["canonical_key", "singular_token"]
