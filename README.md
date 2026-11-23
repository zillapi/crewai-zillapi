# crewai-zillapi

CrewAI tools for [Zillapi](https://zillapi.com), a REST API for Zillow property data.

Give a CrewAI agent the ability to look up a property by address, zpid or Zillow URL, pull
valuations, price history, tax history, schools, photos and comparable homes, and search
for sale, for rent or sold listings inside a geographic area.

## Install

```bash
pip install crewai-zillapi
```

## Get an API key

Create a key at [zillapi.com](https://zillapi.com). You get 100 credits at signup, no card,
one time. Paid plans start at $5 per month. See [pricing](https://zillapi.com/pricing).

Set the key in your environment:

```bash
export ZILLAPI_KEY="zk_your_key_here"
```

## Quick start

```python
from crewai import Agent
from crewai_zillapi import (
    ZillapiPropertyByAddressTool,
    ZillapiPropertyDetailTool,
    ZillapiListingSearchTool,
)

analyst = Agent(
    role="Real estate analyst",
    goal="Answer questions about properties and local markets with real data",
    backstory="You research homes and neighborhoods using live Zillow data.",
    tools=[
        ZillapiPropertyByAddressTool(),
        ZillapiPropertyDetailTool(),
        ZillapiListingSearchTool(),
    ],
)
```

Use a single tool directly:

```python
import json
from crewai_zillapi import ZillapiPropertyByAddressTool

result = json.loads(
    ZillapiPropertyByAddressTool().run(address="350 5th Ave, New York NY 10118")
)
print(result["data"]["zpid"])
```

## Tools

| Tool | What it does | Credit cost |
|---|---|---|
| `ZillapiPropertyByAddressTool` | Resolve a street address to a full property record | 3 per successful call |
| `ZillapiPropertyByZpidTool` | Full property record for a known zpid | 1 per successful call |
| `ZillapiPropertyDetailTool` | One detail section for a zpid | 1 per successful call |
| `ZillapiPropertyByUrlTool` | Resolve a Zillow URL to a property or building | 1 per record returned |
| `ZillapiListingSearchTool` | Search listings inside a bounding box | 1 per listing returned |
| `ZillapiJobTool` | Poll an async job and read its results | 0, free |
| `ZillapiAccountTool` | Plan, remaining credit balance, recent usage | 0, free |

A lookup or search with no match costs 1 credit; other failed calls are free.

`ZillapiPropertyDetailTool` sections: `zestimate`, `price-history`, `tax-history`, `schools`,
`photos`, `agent`, `nearby`, `open-houses`, `facts`.

## Searching an area

A listing search needs a real bounding box in decimal degrees. A place name on its own is not
accepted, so convert the place to coordinates first.

```python
import json
from crewai_zillapi import ZillapiListingSearchTool

result = json.loads(
    ZillapiListingSearchTool().run(
        west=-73.99, south=40.74, east=-73.98, north=40.75,
        status="for_sale", beds_min=2, price_max=2_000_000, max_items=25,
    )
)
```

Because a search costs 1 credit per listing returned, keep `max_items` tight. Asking for more
than 50 listings runs the search asynchronously and returns a `job_id`, which you poll with
`ZillapiJobTool` until its status is `succeeded`, then read with `action="results"`.

## Response shape

Every tool returns a JSON string with a stable envelope, and the tools never raise from
execution. An agent can always parse the result.

```json
{
  "success": true,
  "data": { "zpid": "1234567", "price": 1250000 },
  "request_id": "req_abc123"
}
```

```json
{ "success": false, "error": { "code": "invalid_filters", "message": "..." } }
```

`data` is the payload itself, so you never have to reach through a second wrapper.
`request_id` is worth logging if you need to ask about a specific call. List endpoints
also return a `meta` object alongside `data`.

Match on `code`, never on `message`.

| Code | Meaning |
|---|---|
| `missing_api_key` | `ZILLAPI_KEY` is not set |
| `invalid_api_key` | The key was rejected |
| `out_of_credits` | No credits left, top up to continue |
| `invalid_filters` | A search was sent without a bounding box |
| `invalid_search_url` | The Zillow search URL was not usable |
| `missing_input` | A required argument was absent |
| `not_found` | Nothing matched the address, zpid or URL |
| `job_not_ready` | The async job has not finished yet |
| `rate_limited` | Too many requests, respect `Retry-After` |
| `service_unavailable` | The data provider is temporarily unavailable |
| `timeout` | The request timed out |
| `network` | The API could not be reached |

## Coverage

These tools cover the synchronous, agent friendly surface of the API. The full REST API also
offers batch property jobs, chained search with details, building unit extraction and outbound
webhooks. See the [API documentation](https://zillapi.com/docs) and the
[OpenAPI spec](https://zillapi.com/openapi.json).

Zillapi is also available as an [MCP server](https://api.zillapi.com/mcp) for MCP compatible
clients.

## Notes

Zillapi is an independent service and is not affiliated with, endorsed by, or sponsored by
Zillow, Inc. Zillow is a trademark of its respective owner. Use of the data is subject to the
[Zillapi terms](https://zillapi.com/legal/terms).

## License

MIT
