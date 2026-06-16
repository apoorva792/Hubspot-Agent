"""The lead-fetching agent: Claude tool-calling loop over the HubSpot client.

Flow (the loop we proved live):
  prompt -> discover_schema -> translate to HubSpot filterGroups -> search_crm
        -> (optional) resolve_owners / date_to_millis -> final answer

The model only ever sees a small SAMPLE of search results (to save tokens);
the backend keeps the full record set for CSV export.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

import anthropic

from hubspot import HubSpot, HubSpotError

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "1000"))
MAX_TURNS = 10

SYSTEM_PROMPT = """You are a HubSpot lead-fetching agent for product and sales teams.
A user gives you a plain-English description of the leads they want. You translate
that into a precise HubSpot CRM search and return the matching records.

Today's date (UTC) is {today}.

Follow these rules strictly:
1. ALWAYS call `discover_schema` BEFORE searching. Never invent property names or
   enumeration values — use the exact `value` strings returned by discovery. This
   portal has custom values (e.g. lead statuses, lifecycle stages) you cannot guess.
2. Build filters as HubSpot filterGroups: filters inside one group are AND-ed;
   separate groups are OR-ed together.
3. To filter by owner/rep name, first call `resolve_owners` to get the numeric id,
   then filter on `hubspot_owner_id`.
4. For time windows like "not contacted in N days" or "created in the last N days",
   call `date_to_millis` to get the epoch-millisecond boundary, then use LT/GT on the
   relevant datetime property (e.g. `notes_last_contacted`, `createdate`).
5. In `search_crm`, request a focused `properties` list — these become the CSV
   columns. For contacts always include at least: firstname, lastname, email,
   jobtitle, company, lifecyclestage, hs_lead_status, country, hubspot_owner_id.
6. Sort by the most useful signal for the request (e.g. most recently contacted,
   or newest first) when it helps.
7. If a search returns an error, read it and fix the query (usually a bad property
   name or enum value) — re-discover the schema if needed.
8. When done, write a short summary: how many leads matched, the segment shape
   (industries/regions/titles you notice), and any caveats. Do NOT list every lead;
   the full set is exported to CSV automatically.

Use-case mapping for THIS portal (verified live — prefer these, don't guess):
- The primary use-case field is `what_is_your_usecase` (checkbox). Older records use
  `what_is_your_usecase_og`; free-text descriptions live in `please_share_your_usecase`.
- A MARKETING use case = values like "AI for Marketing", "AI for Sales", "Agent API",
  "Custom Agents", "AI RFP Scout", or the "Sales & Marketing" tag.
- `what_is_your_usecases` is labelled "Industries" — it is NOT a usecase field; ignore
  it for usecase filtering.
- When a request involves deals (deal stage/owner/value, "associated deal", close-lost
  reason, or per-row Deal IDs), set `include_associated_deals: true` on `search_crm`
  with object_type "contacts". The backend joins each contact's deals, classifies each
  as open/closed_won/closed_lost (and recoverable/hard_dead/unknown for losses), and
  dedups — all deterministically.
- To restrict to a deal state or loss class (e.g. "recoverable closed-lost deals to
  reactivate", "open deals"), ALSO set `deal_filter`, e.g.
  {{"deal_state": "closed_lost", "loss_class": "recoverable"}}. Deal state cannot be
  expressed as a contact filterGroup, so always use `deal_filter` for it. Contacts with
  no matching deal are returned separately (off-target), not dropped.

STANDING RULES — these always apply and OVERRIDE the user's prompt. State them in your
summary when relevant; never break them even if the user explicitly asks you to:
- Junk leads NEVER appear in results: any lead whose hs_lead_status or lifecyclestage is
  Junk Lead, Unqualified, Disqualified, or Discarded is dropped by the backend regardless
  of the prompt. If a user asks for junk leads, flag the conflict — they will be dropped.
- A "Sales & Marketing" TAG alone does not make a lead a marketing usecase; the true
  usecase field governs (operations/support/HR/etc. are excluded even if tagged).
- Never invent leads. If nothing matches, say so and return zero rows.
"""

TOOLS: list[dict] = [
    {
        "name": "discover_schema",
        "description": (
            "Discover available CRM properties and their valid enumeration values. "
            "ALWAYS call this before search_crm. Returns property name, type, and the "
            "exact option values you must use verbatim in filters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {
                    "type": "string",
                    "enum": ["contacts", "companies", "deals", "tickets"],
                    "description": "CRM object to inspect. Use 'contacts' for leads.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Property-name guesses, e.g. ['lifecycle','lead status','country']. Empty lists all.",
                },
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "search_crm",
        "description": (
            "Search HubSpot CRM records using filterGroups (AND within a group, OR "
            "across groups). Returns the total match count and a small sample. The "
            "requested `properties` become the CSV columns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string", "enum": ["contacts", "companies", "deals", "tickets"]},
                "filterGroups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "filters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "propertyName": {"type": "string"},
                                        "operator": {
                                            "type": "string",
                                            "enum": [
                                                "EQ", "NEQ", "LT", "LTE", "GT", "GTE",
                                                "BETWEEN", "IN", "NOT_IN", "HAS_PROPERTY",
                                                "NOT_HAS_PROPERTY", "CONTAINS_TOKEN",
                                                "NOT_CONTAINS_TOKEN",
                                            ],
                                        },
                                        "value": {"type": "string"},
                                        "values": {"type": "array", "items": {"type": "string"}},
                                        "highValue": {"type": "string"},
                                    },
                                    "required": ["propertyName", "operator"],
                                },
                            }
                        },
                        "required": ["filters"],
                    },
                },
                "properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Properties to return; these become CSV columns.",
                },
                "include_associated_deals": {
                    "type": "boolean",
                    "description": (
                        "Set true (contacts only) when the request needs deal data — "
                        "the backend joins each contact's associated deals, adding deal "
                        "id/stage/owner/amount/close-lost reason and one row per deal."
                    ),
                },
                "deal_filter": {
                    "type": "object",
                    "description": (
                        "Optional (contacts + include_associated_deals only). Restrict "
                        "results to deals of a given state/loss-class, applied "
                        "deterministically by the backend over the full set. Contacts "
                        "with no matching deal are returned in a separate off-target "
                        "bucket, not dropped. Do NOT express deal state as a contact "
                        "filterGroup — use this instead."
                    ),
                    "properties": {
                        "deal_state": {
                            "type": "string",
                            "enum": ["open", "closed_won", "closed_lost"],
                        },
                        "loss_class": {
                            "type": "string",
                            "enum": ["recoverable", "hard_dead", "unknown"],
                        },
                    },
                },
                "sorts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "propertyName": {"type": "string"},
                            "direction": {"type": "string", "enum": ["ASCENDING", "DESCENDING"]},
                        },
                        "required": ["propertyName", "direction"],
                    },
                },
            },
            "required": ["object_type", "properties"],
        },
    },
    {
        "name": "resolve_owners",
        "description": "Resolve an owner/rep name or email fragment to hubspot_owner_id values.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "date_to_millis",
        "description": (
            "Convert a relative time to a Unix epoch-millisecond string for date "
            "filters. Returns the boundary for 'days_ago' days before now (UTC)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days_ago": {"type": "integer"}},
            "required": ["days_ago"],
        },
    },
]


def _execute_tool(name: str, args: dict, hs: HubSpot) -> tuple[str, dict | None]:
    """Run a tool. Returns (text_for_model, meta_for_backend_or_None)."""
    try:
        if name == "discover_schema":
            props = hs.search_properties(args["object_type"], args.get("keywords") or [])
            return json.dumps({"properties": props}), None

        if name == "search_crm":
            result = hs.search(
                object_type=args["object_type"],
                filter_groups=args.get("filterGroups") or [],
                properties=args["properties"],
                sorts=args.get("sorts"),
                max_records=MAX_RECORDS,
            )
            records = result["records"]
            # Show the model only a small sample to conserve tokens.
            sample = [r.get("properties", {}) for r in records[:3]]
            model_view = {
                "total_in_hubspot": result["total"],
                "fetched": len(records),
                "note": "Full set kept server-side for CSV export. Sample below.",
                "sample": sample,
            }
            meta = {
                "object_type": args["object_type"],
                "records": records,
                "properties": args["properties"],
                "total": result["total"],
                "include_associated_deals": bool(args.get("include_associated_deals")),
                "deal_filter": args.get("deal_filter") or {},
                "query": {
                    "filterGroups": args.get("filterGroups") or [],
                    "sorts": args.get("sorts"),
                    "properties": args["properties"],
                    "include_associated_deals": bool(args.get("include_associated_deals")),
                    "deal_filter": args.get("deal_filter") or {},
                },
            }
            return json.dumps(model_view), meta

        if name == "resolve_owners":
            return json.dumps({"owners": hs.find_owners(args["query"])}), None

        if name == "date_to_millis":
            now = dt.datetime.now(dt.timezone.utc)
            boundary = now - dt.timedelta(days=int(args["days_ago"]))
            ms = int(boundary.timestamp() * 1000)
            return json.dumps({"epoch_millis": str(ms), "iso": boundary.isoformat()}), None

        return json.dumps({"error": f"unknown tool {name}"}), None

    except HubSpotError as e:
        return json.dumps({"error": str(e)}), None


def run_agent(prompt: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Run the full agent loop and return summary + the last search's full records."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    hs = HubSpot()

    system = SYSTEM_PROMPT.format(today=dt.date.today().isoformat())
    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_search: dict | None = None
    final_text = ""

    for _ in range(MAX_TURNS):
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=0,  # determinism: same prompt -> same translated query (test F3)
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            break

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            text, meta = _execute_tool(block.name, block.input, hs)
            if block.name == "search_crm" and meta:
                last_search = meta
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": text}
            )
        messages.append({"role": "user", "content": tool_results})

    return {
        "summary": final_text or "Done.",
        "last_search": last_search,
        "hubspot": hs,
    }
