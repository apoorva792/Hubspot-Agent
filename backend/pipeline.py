"""Deterministic post-processing over the agent's full result set.

The LLM only ever sees a 3-row sample, so anything that must reason over the WHOLE
set lives here, in plain Python, so it is repeatable and auditable:

  join deals -> drop junk leads -> dedup -> deal filter -> split missing email
             -> owner/stage breakdown

This is the spec's "results are deduplicated, classified, and filtered" stage.
"""
from __future__ import annotations

from typing import Any

import config
from hubspot import HubSpot

# Deal fields we try to pull when joining (intersected with what the portal actually
# has, so a missing custom field never breaks the batch read).
_WISHLIST_DEAL_PROPS = [
    "dealname", "dealstage", "amount", "hubspot_owner_id", "closedate",
    "pipeline", "closed_lost_reason", "closedlost_reason", "hs_is_closed_won",
    "hs_is_closed_lost", "hs_lastmodifieddate", "notes_last_contacted",
]


def _available_deal_props(hs: HubSpot) -> list[str]:
    """The subset of our wishlist that exists on this portal's Deal object.

    Returns [] if the deals schema can't be read (scope missing) — callers then skip
    the join entirely rather than crash.
    """
    try:
        names = {p["name"] for p in hs.all_properties("deals")}
    except Exception:
        return []
    return [p for p in _WISHLIST_DEAL_PROPS if p in names]


def _classify_deal(props: dict[str, str], stage_label: str) -> tuple[str, str]:
    """Return (deal_state, loss_class) from a deal's stage + close-lost reason.

    deal_state: 'closed_won' | 'closed_lost' | 'open'
    loss_class: 'recoverable' | 'hard_dead' | 'unknown' | '' (only meaningful if lost)

    Prefers HubSpot's own `hs_is_closed_won`/`hs_is_closed_lost` flags (reliable across
    pipelines) and falls back to the stage label.
    """
    won = str(props.get("hs_is_closed_won", "")).lower() == "true"
    lost = str(props.get("hs_is_closed_lost", "")).lower() == "true"
    s = (stage_label or "").lower()
    if won or "won" in s:
        return "closed_won", ""
    if lost or "lost" in s:
        reason = (
            props.get("closed_lost_reason") or props.get("closedlost_reason") or ""
        ).lower()
        if any(k in reason for k in config.HARD_DEAD_LOSS_KEYWORDS):
            loss = "hard_dead"
        elif any(k in reason for k in config.RECOVERABLE_LOSS_KEYWORDS):
            loss = "recoverable"
        else:
            loss = "unknown"
        return "closed_lost", loss
    return "open", ""


def join_deals(
    contact_records: list[dict], hs: HubSpot
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Fetch each contact's associated deals. Returns (deal_id->props, contact_id->deal_ids).

    Degrades to ({}, {}) when the deals/association scope is missing."""
    deal_props = _available_deal_props(hs)
    if not deal_props:
        return {}, {}
    contact_ids = [str(r["id"]) for r in contact_records if r.get("id")]
    cid_to_deals = hs.associations("contacts", "deals", contact_ids)
    all_deal_ids = sorted({d for ds in cid_to_deals.values() for d in ds})
    deals = hs.batch_read("deals", all_deal_ids, deal_props)
    return deals, cid_to_deals


def build_rows(
    records: list[dict],
    hs: HubSpot,
    object_type: str,
    include_deals: bool,
) -> list[dict]:
    """One enriched row per record (or per contact-deal pair when joining deals).

    Enrichment is deterministic: owner names, enum labels, record link, explicit
    `hs_contact_id`/`hs_deal_id`, and deal classification columns.
    """
    enum_maps = _safe_enum_maps(hs, object_type)
    owners = hs.owner_map()

    deals: dict[str, dict] = {}
    cid_to_deals: dict[str, list[str]] = {}
    stage_labels: dict[str, str] = {}
    if include_deals and object_type == "contacts":
        deals, cid_to_deals = join_deals(records, hs)
        stage_labels = _safe_stage_labels(hs)

    rows: list[dict] = []
    for r in records:
        rid = str(r.get("id", ""))
        base = _enrich_props(dict(r.get("properties", {})), enum_maps, owners)
        base["hs_contact_id" if object_type == "contacts" else f"hs_{object_type[:-1]}_id"] = rid
        link = hs.record_url(object_type, rid)
        if link:
            base["hubspot_link"] = link

        deal_ids = cid_to_deals.get(rid, []) if include_deals else []
        if not deal_ids:
            base.setdefault("hs_deal_id", "")
            rows.append(base)
            continue

        # One row per associated deal (stated dedup rule: unique contact x deal pair).
        for did in deal_ids:
            dprops = deals.get(did, {})
            row = dict(base)
            row["hs_deal_id"] = did
            stage_label = stage_labels.get(
                str(dprops.get("dealstage", "")), dprops.get("dealstage", "")
            )
            row["deal_name"] = dprops.get("dealname", "")
            row["deal_stage"] = stage_label
            row["deal_amount"] = dprops.get("amount", "")
            row["deal_closedate"] = dprops.get("closedate", "")
            doid = dprops.get("hubspot_owner_id")
            row["deal_owner"] = owners.get(str(doid), "") if doid else ""
            row["deal_closelost_reason"] = (
                dprops.get("closed_lost_reason") or dprops.get("closedlost_reason") or ""
            )
            state, loss = _classify_deal(dprops, stage_label)
            row["deal_state"] = state
            row["loss_class"] = loss
            rows.append(row)
    return rows


def drop_junk_leads(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Standing rule: junk/unqualified/disqualified/discarded leads never appear.

    Checks `hs_lead_status` and `lifecyclestage` against config.JUNK_LEAD_KEYWORDS
    (case-insensitive substring). Returns (kept, dropped). Overrides the prompt."""
    kept, dropped = [], []
    for row in rows:
        hay = " ".join(
            str(row.get(k, "")) for k in ("hs_lead_status", "lifecyclestage")
        ).lower()
        if any(bad in hay for bad in config.JUNK_LEAD_KEYWORDS):
            dropped.append(row)
        else:
            kept.append(row)
    return kept, dropped


def dedupe(rows: list[dict]) -> list[dict]:
    """Unique on (contact id, deal id). Stable order. No person+deal pair appears twice."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in rows:
        key = (str(row.get("hs_contact_id", "")), str(row.get("hs_deal_id", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def apply_deal_filter(
    rows: list[dict], spec: dict | None
) -> tuple[list[dict], list[dict]]:
    """Split rows by the deal state/loss-class already computed in build_rows.

    `spec` may carry 'deal_state' (open|closed_won|closed_lost) and/or 'loss_class'
    (recoverable|hard_dead|unknown). A row matches only if every key present in `spec`
    equals the row's computed value. Rows that don't match — including contacts with no
    associated deal — are returned separately (off-target) rather than dropped, so
    nothing silently disappears. No-op (empty spec) returns (rows, [])."""
    want_state = (spec or {}).get("deal_state", "").strip().lower()
    want_loss = (spec or {}).get("loss_class", "").strip().lower()
    if not want_state and not want_loss:
        return rows, []
    matching, off_target = [], []
    for row in rows:
        ok = (not want_state or str(row.get("deal_state", "")).lower() == want_state) and (
            not want_loss or str(row.get("loss_class", "")).lower() == want_loss
        )
        (matching if ok else off_target).append(row)
    return matching, off_target


def split_missing_email(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate rows with no email rather than dropping or placeholdering them (test F4)."""
    with_email, no_email = [], []
    for row in rows:
        if str(row.get("email", "")).strip():
            with_email.append(row)
        else:
            no_email.append(row)
    return with_email, no_email


def breakdown(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Counts by owner and by stage so headline totals can be reconciled (test E2)."""
    by_owner: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    for row in rows:
        owner = row.get("deal_owner") or row.get("owner_name") or "(unassigned)"
        stage = row.get("deal_stage") or row.get("hs_lead_status") or row.get(
            "lifecyclestage"
        ) or "(none)"
        by_owner[owner] = by_owner.get(owner, 0) + 1
        by_stage[stage] = by_stage.get(stage, 0) + 1
    return {
        "by_owner": dict(sorted(by_owner.items(), key=lambda kv: -kv[1])),
        "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1])),
    }


# ----- internal helpers ----------------------------------------------------
def _safe_enum_maps(hs: HubSpot, object_type: str) -> dict[str, dict[str, str]]:
    try:
        return hs.enum_label_maps(object_type)
    except Exception:
        return {}


def _safe_stage_labels(hs: HubSpot) -> dict[str, str]:
    try:
        return hs.deal_stage_labels()
    except Exception:
        return {}


def _enrich_props(
    props: dict[str, Any], enum_maps: dict[str, dict[str, str]], owners: dict[str, str]
) -> dict[str, Any]:
    oid = props.get("hubspot_owner_id")
    if oid and str(oid) in owners:
        props["owner_name"] = owners[str(oid)]
    for key, val in list(props.items()):
        if key in enum_maps and val in enum_maps[key]:
            props[key] = enum_maps[key][val]
    return props
