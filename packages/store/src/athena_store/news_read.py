"""V2-P3 N3: the single canonical news read model (§6.3 / §26).

**DARK LAUNCH.** ``SOURCE`` ships as ``LEGACY``. A *persisted* backfill (§26.2
item 1) and observed runtime N2 dual-write convergence (item 2) are hard
preconditions for the cutover; §25's six-item gate was a transaction-internal
dry-run that left the live canonical tables empty, and §26.4 forbids starting
the engine. Cutting over against empty tables would silently blank every news
reader. The read model and all 17 wrappers are complete and verified here;
activation is one line (``SOURCE = CANONICAL``) plus a persisted backfill and
requires an explicit operator go/no-go.

One switch (``SOURCE``) controls all 17 readers. ``render()`` prefixes a CTE
named ``news_events`` that projects the three canonical tables into the **exact
legacy column shape**, so every reader keeps its own WHERE / ORDER BY / LIMIT /
``(ts AT TIME ZONE 'America/New_York')`` bucketing byte-identical. Setting
``SOURCE = "legacy"`` -- or reverting this module -- returns every reader to
the legacy table in one step.

Rulings recorded in §26.1:

* **R1 (ET day bucketing)**: the CTE exposes ``ts = association.effective_ts``,
  a real ``timestamptz``. ``guardian_regime`` keeps bucketing that column in
  America/New_York. ``published_day`` (UTC) is deliberately **not** exposed to
  readers: it is identity-only.
* **R2 (news_score determinism)**: only the canonical ``news_score`` reader gets
  a deterministic tie-break (``ORDER BY ts, sentiment``), requested explicitly
  via ``stable_order=True``. The legacy path returned by ``render`` stays
  byte-identical (``ORDER BY ts``), and ``indicators.py``'s Decimal loop is
  untouched.

Column provenance matters. Identity/anchor fields come from the immutable
legacy map, mutable business fields from the live association:

* ``fingerprint`` <- ``news_legacy_event_map.legacy_fingerprint`` (deep links,
  dismiss, draft reasons must not lose their referent)
* ``title``       <- ``news_legacy_event_map.title`` (per-legacy-row title)
* ``ts``          <- ``news_article_symbols.effective_ts``
* ``symbol`` / ``source`` / ``event_type`` / ``sentiment`` / ``url`` /
  ``dismissed``   <- ``news_article_symbols`` (DSH enrich and dismiss write here)

The join produces one row per legacy map row, i.e. the same cardinality as the
legacy table, so counts, ``avg()``, ``max()`` and the paginated news list remain
reconcilable site by site. No migration is added.
"""

from __future__ import annotations

import re

CANONICAL = "canonical"
LEGACY = "legacy"

# §26.2 makes a *persisted* backfill + runtime N2 convergence hard
# preconditions for the N3 cutover. Both were still unmet at delivery time:
# §25's six-item gate was a transaction-internal dry-run (ROLLBACK), so the
# live canonical tables were empty, and §26.4 forbids starting the engine to
# observe dual-write convergence. Flipping this switch against empty tables
# would silently blank every news reader. Therefore V2-P3 batch 3 ships as a
# DARK LAUNCH: the read model and all 17 wrappers are complete and verified,
# but the process-wide default stays LEGACY (zero behavior change, zero
# persistent DB writes). Activation is one line (SOURCE = CANONICAL) plus the
# persisted backfill, and must be preceded by six_item_gate() == blocks_n3
# False on the live, persisted state. One-line cutback / git revert both work.
SOURCE = LEGACY

# A CTE named news_events shadows the legacy table for the duration of one
# statement. Readers keep writing "FROM news_events" literally.
CANONICAL_CTE = """WITH news_events AS (
    SELECT m.legacy_fingerprint AS fingerprint,
           s.effective_ts       AS ts,
           s.symbol             AS symbol,
           s.source             AS source,
           s.event_type         AS event_type,
           s.sentiment          AS sentiment,
           m.title              AS title,
           s.url                AS url,
           s.dismissed          AS dismissed
    FROM news_legacy_event_map AS m
    JOIN news_article_symbols AS s ON s.association_id = m.association_id
)
"""

LEGACY_COLUMNS = (
    "fingerprint", "ts", "symbol", "source", "event_type",
    "sentiment", "title", "url", "dismissed",
)

# R2: add a deterministic tie-break only where explicitly requested, and never
# turn "ORDER BY ts DESC" into "ORDER BY ts, sentiment DESC".
_ORDER_BY_TS = re.compile(r"ORDER BY ts\b(?!\s+DESC)")
_STABLE_ORDER = "ORDER BY ts, sentiment"


def is_canonical(source: str | None = None) -> bool:
    return (source if source is not None else SOURCE) == CANONICAL


def render(sql: str, *, stable_order: bool = False, source: str | None = None) -> str:
    """Return the SQL to execute for the currently selected news read source.

    ``source=`` gives each reader its own one-line cutback (§26.2 item 4);
    omitting it uses the global ``SOURCE`` switch.
    """
    src = source if source is not None else SOURCE
    if src == LEGACY:
        return sql
    if src != CANONICAL:
        raise ValueError(f"unknown news read source: {src!r}")
    body = _ORDER_BY_TS.sub(_STABLE_ORDER, sql, count=1) if stable_order else sql
    return CANONICAL_CTE + body


def score_sql(sql: str, *, source: str | None = None) -> str:
    """R2 helper: ``news_score`` and only ``news_score`` gets the tie-break."""
    return render(sql, stable_order=True, source=source)
