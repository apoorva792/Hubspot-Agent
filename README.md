# HubSpot Lead Agent

Describe the leads you want in plain English → the agent translates it into a real
HubSpot CRM search, runs it, enriches the results, and gives you a downloadable CSV.

It's the loop, end to end:

```
prompt → discover schema → translate to HubSpot filterGroups → search CRM
       → enrich (owner names + enum labels + record links) → CSV
```

## What's inside

```
hubspot-lead-agent/
├── backend/
│   ├── main.py          # FastAPI: /api/search, /api/download, serves the UI
│   ├── agent.py         # Claude tool-calling loop + tool definitions
│   ├── hubspot.py       # HubSpot CRM v3 client (schema, search, owners)
│   └── requirements.txt
├── frontend/
│   ├── index.html       # prompt box, results table, CSV download
│   ├── app.js
│   └── styles.css
├── .env.example
├── run.sh
└── README.md
```

## Setup

1. **Get a HubSpot private-app token.** In HubSpot: *Settings → Integrations →
   Private Apps → Create*. Grant read scopes: `crm.objects.contacts.read`,
   `crm.objects.companies.read`, `crm.objects.deals.read`,
   `crm.schemas.contacts.read`. Copy the token.

2. **Get an Anthropic API key** from https://console.anthropic.com.

3. **Configure:**
   ```bash
   cp .env.example .env
   # edit .env and paste both keys
   ```

4. **Run:**
   ```bash
   ./run.sh
   ```
   Open http://localhost:8000

   (Manual alternative: `python3 -m venv .venv && source .venv/bin/activate &&
   pip install -r backend/requirements.txt && cd backend && uvicorn main:app --reload`)

## Try these prompts

- "Warm leads being actively worked (status Working or Demo Booked)"
- "US-based MQLs we haven't contacted in the last 30 days"
- "Decision-makers (Head/VP/Chief) at banks in the Middle East"
- "SQLs created in the last 14 days, newest first"

## How it works

The agent (`agent.py`) is Claude with four tools:

| Tool | Purpose |
|------|---------|
| `discover_schema` | Fetch real property names + enum values **before** querying (no hallucinated fields). |
| `search_crm`      | Run a HubSpot `filterGroups` search; backend paginates up to `MAX_RECORDS`. |
| `resolve_owners`  | Map a rep's name → `hubspot_owner_id`. |
| `date_to_millis`  | Turn "last N days" into an epoch-ms boundary for date filters. |

The model only sees a small sample of search results (to save tokens); the backend
keeps the full set, enriches it (owner names, enum labels, clickable record links),
and builds the CSV.

## Porting to the Lyzr platform

This maps 1:1 onto a Lyzr agent: register the four functions in `agent.py` as tools,
move `SYSTEM_PROMPT` into the agent's instructions, and seed a knowledge base with
your portal's stable enums for an extra accuracy boost.

## Notes / limits

- HubSpot's search API caps at 10,000 results per query; `MAX_RECORDS` (default 1000)
  bounds what's pulled for CSV. Raise it in `.env` if needed.
- CSVs are stored in memory and cleared on restart — fine for a local test tool.
- Read-only by design: the agent never writes to your CRM.
```
