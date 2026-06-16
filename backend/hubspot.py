"""Thin HubSpot CRM v3 client used by the agent's tools.

Wraps property discovery, the CRM search API (with pagination), owners, and
account info. Kept deliberately small so it's easy to read and port.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import httpx

BASE = "https://api.hubapi.com"

# objectType -> the id segment HubSpot uses in record URLs
_OBJECT_URL_IDS = {
    "contacts": "0-1",
    "companies": "0-2",
    "deals": "0-3",
    "tickets": "0-5",
}


def _chunks(seq: list, size: int):
    """Yield successive `size`-length slices of `seq` (HubSpot batch endpoints cap at 100)."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class HubSpotError(Exception):
    """Raised with a human-readable message the agent can read and self-correct from."""


class HubSpot:
    def __init__(self, token: str | None = None, timeout: float = 30.0):
        token = token or os.environ.get("HUBSPOT_TOKEN")
        if not token:
            raise HubSpotError("HUBSPOT_TOKEN is not set. Add it to your .env file.")
        self._client = httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    # ----- low level -------------------------------------------------------
    def _get(self, path: str, **params) -> dict[str, Any]:
        r = self._client.get(path, params=params or None)
        self._raise_for_status(r)
        return r.json()

    def _post(self, path: str, json: dict) -> dict[str, Any]:
        r = self._client.post(path, json=json)
        self._raise_for_status(r)
        return r.json()

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.is_success:
            return
        # Surface HubSpot's own error message so the LLM can correct its query.
        try:
            detail = r.json().get("message", r.text)
        except Exception:
            detail = r.text
        raise HubSpotError(f"HubSpot API {r.status_code}: {detail}")

    # ----- properties / schema --------------------------------------------
    @lru_cache(maxsize=8)
    def all_properties(self, object_type: str) -> tuple:
        """All property definitions for an object type (cached per process)."""
        data = self._get(f"/crm/v3/properties/{object_type}")
        return tuple(data.get("results", []))

    def search_properties(self, object_type: str, keywords: list[str]) -> list[dict]:
        """Keyword (substring) search over property name/label, trimmed for token size."""
        props = self.all_properties(object_type)
        if not keywords:
            matches = list(props)
        else:
            kws = [k.lower() for k in keywords]
            matches = [
                p
                for p in props
                if any(k in p["name"].lower() or k in p.get("label", "").lower() for k in kws)
            ]
        # Trim payload: keep the fields the model needs, cap enum options.
        out = []
        for p in matches[:40]:
            entry = {
                "name": p["name"],
                "label": p.get("label"),
                "type": p.get("type"),
            }
            opts = p.get("options") or []
            if opts:
                entry["options"] = [
                    {"value": o["value"], "label": o["label"]} for o in opts[:40]
                ]
                if len(opts) > 40:
                    entry["options_truncated"] = True
            out.append(entry)
        return out

    @lru_cache(maxsize=2)
    def deal_stage_labels(self) -> dict[str, str]:
        """stageId -> readable stage label, across all deal pipelines.

        Deal stages are defined per-pipeline, not as `dealstage` property options, so
        the only way to label a stage id (e.g. '258084705') is the pipelines API.
        Returns {} if it can't be read."""
        out: dict[str, str] = {}
        try:
            data = self._get("/crm/v3/pipelines/deals")
        except HubSpotError:
            return out
        for pipe in data.get("results", []):
            for stage in pipe.get("stages", []):
                out[str(stage.get("id"))] = stage.get("label", str(stage.get("id")))
        return out

    def enum_label_maps(self, object_type: str) -> dict[str, dict[str, str]]:
        """value->label maps for every enumeration property (for output enrichment)."""
        maps: dict[str, dict[str, str]] = {}
        for p in self.all_properties(object_type):
            if p.get("type") == "enumeration" and p.get("options"):
                maps[p["name"]] = {o["value"]: o["label"] for o in p["options"]}
        return maps

    # ----- search ----------------------------------------------------------
    def search(
        self,
        object_type: str,
        filter_groups: list[dict],
        properties: list[str],
        sorts: list[dict] | None = None,
        max_records: int = 1000,
    ) -> dict[str, Any]:
        """Run a CRM search, paginating via the `after` cursor up to max_records."""
        records: list[dict] = []
        after: str | None = None
        total = 0
        page_size = min(200, max_records)

        while len(records) < max_records:
            body: dict[str, Any] = {
                "filterGroups": filter_groups or [],
                "properties": properties,
                "limit": page_size,
            }
            if sorts:
                body["sorts"] = sorts
            if after:
                body["after"] = after

            data = self._post(f"/crm/v3/objects/{object_type}/search", json=body)
            total = data.get("total", 0)
            page = data.get("results", [])
            records.extend(page)

            after = (
                data.get("paging", {}).get("next", {}).get("after")
                if data.get("paging")
                else None
            )
            if not after or not page:
                break

        return {"total": total, "records": records[:max_records]}

    # ----- associations (cross-object join) --------------------------------
    def associations(
        self, from_object: str, to_object: str, ids: list[str]
    ) -> dict[str, list[str]]:
        """Map each from-record id -> list of associated to-record ids (v4 batch).

        Used to join Contacts to their Deals so a row can carry both IDs. Returns
        {} silently if the app lacks the association/read scope, so callers degrade
        gracefully instead of crashing.
        """
        out: dict[str, list[str]] = {}
        if not ids:
            return out
        path = f"/crm/v4/associations/{from_object}/{to_object}/batch/read"
        for chunk in _chunks(ids, 100):
            try:
                data = self._post(path, {"inputs": [{"id": i} for i in chunk]})
            except HubSpotError:
                return out  # missing scope or object — caller treats as "no deals"
            for row in data.get("results", []):
                frm = str(row.get("from", {}).get("id", ""))
                tos = [str(t.get("toObjectId")) for t in row.get("to", []) if t.get("toObjectId")]
                if frm:
                    out[frm] = tos
        return out

    def batch_read(
        self, object_type: str, ids: list[str], properties: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Batch-read records by id -> {id: properties}. Used to pull deal fields
        for the deal ids discovered via associations. Returns {} if scope is missing."""
        out: dict[str, dict[str, Any]] = {}
        uniq = list(dict.fromkeys(ids))  # de-dupe, preserve order
        if not uniq:
            return out
        path = f"/crm/v3/objects/{object_type}/batch/read"
        for chunk in _chunks(uniq, 100):
            body = {"properties": properties, "inputs": [{"id": i} for i in chunk]}
            try:
                data = self._post(path, body)
            except HubSpotError:
                return out
            for rec in data.get("results", []):
                out[str(rec["id"])] = rec.get("properties", {})
        return out

    # ----- owners ----------------------------------------------------------
    @lru_cache(maxsize=1)
    def owner_map(self) -> dict[str, str]:
        """hubspot_owner_id -> display name, across all (paginated) owners."""
        mapping: dict[str, str] = {}
        after: str | None = None
        while True:
            params = {"limit": 100}
            if after:
                params["after"] = after
            data = self._get("/crm/v3/owners", **params)
            for o in data.get("results", []):
                name = " ".join(filter(None, [o.get("firstName"), o.get("lastName")])).strip()
                mapping[str(o["id"])] = name or o.get("email", str(o["id"]))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return mapping

    def find_owners(self, query: str) -> list[dict]:
        """Resolve a name/email fragment to owner ids."""
        q = query.lower()
        out = []
        for oid, name in self.owner_map().items():
            if q in name.lower():
                out.append({"ownerId": oid, "name": name})
        return out[:25]

    # ----- account ---------------------------------------------------------
    @lru_cache(maxsize=1)
    def portal_id(self) -> str:
        env = os.environ.get("HUBSPOT_PORTAL_ID")
        if env:
            return env
        try:
            return str(self._get("/account-info/v3/details").get("portalId", ""))
        except HubSpotError:
            return ""

    def record_url(self, object_type: str, record_id: str) -> str:
        portal = self.portal_id()
        seg = _OBJECT_URL_IDS.get(object_type, "0-1")
        if not portal:
            return ""
        return f"https://app.hubspot.com/contacts/{portal}/record/{seg}/{record_id}"
