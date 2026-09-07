"""How a model registry reads its environment, and how it complains about it.

Lifted out of shared/image_models.py unchanged when the text and vision ids
got a registry of their own (shared/text_models.py). It is a pure MOVE — same
behaviour, same default, and the variable image_models shipped with is still
honoured — and it exists so that the two registries cannot drift on the one
piece of behaviour that has to be identical in both:

    A MISCONFIGURED VARIABLE IS LOUD AND SURVIVABLE, NEVER AN EXCEPTION.

Nothing a model registry does is worth failing a lesson over. A typo in a
Railway variable must show up in the log and then be worked around, not take
the pipeline down at the moment somebody is editing variables — which is, by
definition, the moment something is already wrong.

The second half of that contract is the REPEAT. A complaint said once per
process is not loud: a Railway worker lives for days, so a single line emitted
at the first call after a deploy has rolled out of the log window long before
anyone asks "what is this worker actually running?" — and the answer the logs
then give is silence, which reads as "nothing is wrong" while a typo quietly
decides every call. So the same complaint about the same value is repeated, at
most once per interval, for as long as it is true.
"""

from __future__ import annotations

import logging
import os
import time

# Keyed by the FORMATTED message, value = the monotonic second it was last
# said. Pre-formatting is what lets "the same complaint about the same value"
# be recognised as the same complaint. Shared by every registry, and cleared by
# tests (tests/test_image_model_migration.py clears it as `im._WARNED`).
_WARNED: dict[str, float] = {}

WARN_INTERVAL_DEFAULT = 900.0
# Checked in order, first non-empty wins. The image-specific name came first
# and is the one that could already be set on Railway, so it keeps working and
# keeps meaning exactly what it meant; the generic name is what to reach for
# now that there is more than one registry. Nothing has the generic one set
# today, so this is behaviour-identical to what image_models shipped.
WARN_INTERVAL_ENV = ("MODEL_WARN_INTERVAL_S", "IMAGE_MODEL_WARN_INTERVAL_S")


def env(name: str) -> str:
    """An environment variable, stripped, empty string when unset.

    Read on EVERY call, never captured at import. A module-level
    ``X = os.getenv(...)`` is read once and can never be changed afterwards —
    not by a test's ``monkeypatch.setenv`` and not by anything else — which is
    precisely why the constants these registries replace could not be moved.
    """
    return str(os.getenv(name, "") or "").strip()


def warn_interval() -> float:
    """Seconds between repeats of one complaint. A junk value is the default,
    never zero: this module's whole contract is that nothing here raises, and
    a zero would turn a misconfiguration into a log flood."""
    raw = ""
    for name in WARN_INTERVAL_ENV:
        raw = env(name)
        if raw:
            break
    if not raw:
        return WARN_INTERVAL_DEFAULT
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return WARN_INTERVAL_DEFAULT
    return v if v > 0 else WARN_INTERVAL_DEFAULT


def shout(logger: logging.Logger, message: str) -> None:
    """Say a misconfiguration on `logger`, at most once per `warn_interval()`.

    The caller's own logger is used rather than this module's so that a test
    (and an operator's log filter) can still name the module the complaint is
    about — ``caplog.set_level(logging.ERROR, logger=im.logger.name)`` in
    tests/test_image_model_migration.py does exactly that.

    `message` is pre-formatted rather than lazy: ``logger.error`` with no args
    does no %-substitution of its own, and the formatted string is the key the
    repeat guard recognises.
    """
    now = time.monotonic()
    said = _WARNED.get(message)
    if said is not None and now - said < warn_interval():
        return
    _WARNED[message] = now
    logger.error(message)


__all__ = ["_WARNED", "WARN_INTERVAL_DEFAULT", "WARN_INTERVAL_ENV",
           "env", "warn_interval", "shout"]
