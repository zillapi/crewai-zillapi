"""CrewAI tools for Zillapi (zillapi.com).

Seven tools following the crewai-tools BaseTool conventions:

- ZillapiPropertyByAddressTool: resolve a street address to a property record
- ZillapiPropertyByZpidTool:    fetch a full property record by zpid
- ZillapiPropertyDetailTool:    one detail section of a property (zestimate, schools, ...)
- ZillapiPropertyByUrlTool:     resolve a Zillow URL to a property record
- ZillapiListingSearchTool:     search listings inside a bounding box
- ZillapiJobTool:               poll an async job and read its results
- ZillapiAccountTool:           account, plan, credit balance and recent usage

Every tool returns a stable JSON string envelope and never raises from execution:
  {"success": true, "data": {...}}
  {"success": false, "error": {"code": "...", "message": "..."}}

Auth: ZILLAPI_KEY environment variable (HTTP Bearer, key format zk_...).
"""

import json
import os
import re
from typing import Any, List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field

try:  # crewai is the runtime host; keep the import lazy-friendly for tests
    from crewai.tools import BaseTool, EnvVar
except ImportError:  # pragma: no cover - exercised only without crewai installed
    BaseTool = None  # type: ignore[assignment,misc]
    EnvVar = None  # type: ignore[assignment,misc]

BASE_URL = "https://api.zillapi.com"
USER_AGENT = "crewai-zillapi/0.1.0 (+https://github.com/ZeroPointRepo/crewai-zillapi)"
TIMEOUT_SECONDS = 90

_ENV_KEY = "ZILLAPI_KEY"

_SIGNUP_LINE = (
    "Create a key at https://zillapi.com. 100 credits at signup, no card "
    "(one time). Pricing: https://zillapi.com/pricing"
)

_ENV_VARS = (
    [
        EnvVar(
            name=_ENV_KEY,
            description=f"Zillapi API key (starts with zk_). {_SIGNUP_LINE}",
            required=True,
        )
    ]
    if EnvVar is not None
    else []
)


# ---------------------------------------------------------------------------
# Error handling.
#
# The library emits ONLY messages from the fixed tables below. No text from an
# API response body is ever copied into a message, so a user facing error can
# never carry service internals, however the API itself phrases a failure.
# Error codes are passed through only for client errors (4xx), which come from
# the Zillapi gateway's own validation vocabulary, and only when they look like
# a plain machine token. Server errors always get our own code.
# ---------------------------------------------------------------------------

_SERVICE_UNAVAILABLE = (
    "The property data provider is temporarily unavailable. Retry with backoff."
)

_GENERIC_CLIENT_ERROR = "The request was rejected. Check the tool arguments and retry."

_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_MESSAGE_BY_CODE = {
    "invalid_filters": (
        "The search filters were rejected. A search needs a bounding box "
        "(west, south, east, north). A free text location alone is not accepted."
    ),
    "invalid_search_url": (
        "The Zillow search URL was rejected. It must contain a searchQueryState= "
        "query parameter. Short readable Zillow URLs are not accepted."
    ),
    "missing_input": "A required input was missing. Check the tool arguments and retry.",
    "out_of_credits": (
        "The account is out of credits. Top up at https://zillapi.com/pricing."
    ),
    "insufficient_credits": (
        "The account does not have enough credits for this request. "
        "Top up at https://zillapi.com/pricing."
    ),
    "job_not_ready": "The job has not finished yet. Wait and poll it again.",
}

_MESSAGE_BY_STATUS = {
    400: "The request was rejected as invalid. Check the tool arguments and retry.",
    401: (
        f"The API key was rejected. Check that {_ENV_KEY} holds a valid key "
        f"(it starts with zk_). {_SIGNUP_LINE}"
    ),
    402: "The account is out of credits. Top up at https://zillapi.com/pricing.",
    404: "No matching record was found. Check the address, zpid, or Zillow URL.",
    409: "The job has not finished yet. Wait and poll it again.",
    429: "Rate limited. Wait and retry, respecting the Retry-After header.",
    502: _SERVICE_UNAVAILABLE,
    504: _SERVICE_UNAVAILABLE,
}


def _envelope_error(code: str, message: str) -> str:
    return json.dumps({"success": False, "error": {"code": code, "message": message}})


def _safe_code(raw: Any, status: int) -> str:
    """Return a machine matchable error code that cannot carry service internals."""
    if status >= 500:
        return "service_unavailable"
    if isinstance(raw, str) and _CODE_PATTERN.match(raw):
        return raw
    return f"http_{status}"


def _safe_message(code: str, status: int) -> str:
    """Return one of our own messages. Never text taken from a response body."""
    if code in _MESSAGE_BY_CODE:
        return _MESSAGE_BY_CODE[code]
    if status in _MESSAGE_BY_STATUS:
        return _MESSAGE_BY_STATUS[status]
    return _GENERIC_CLIENT_ERROR if status < 500 else _SERVICE_UNAVAILABLE


def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> str:
    """Call a Zillapi endpoint and return the JSON string envelope."""
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        return _envelope_error(
            "missing_api_key", f"{_ENV_KEY} is not set. {_SIGNUP_LINE}"
        )
    import requests  # lazy import so tests can stub it

    try:
        response = requests.request(
            method,
            BASE_URL + path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json_body,
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        return _envelope_error(
            "timeout", "The request to Zillapi timed out. Retry with backoff."
        )
    except requests.exceptions.RequestException:
        return _envelope_error(
            "network",
            "Could not reach the Zillapi API. Check network connectivity and retry.",
        )

    if response.status_code >= 400:
        raw_code = None
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                raw_code = body["error"].get("code")
        except ValueError:
            pass
        code = _safe_code(raw_code, response.status_code)
        return _envelope_error(code, _safe_message(code, response.status_code))

    if response.status_code == 204:
        return json.dumps({"success": True, "data": {}})

    try:
        data = response.json()
    except ValueError:
        return _envelope_error(
            "bad_response", "The API returned a response that was not valid JSON."
        )

    # Every successful Zillapi response wraps the payload as {"data": ...} with
    # request_id, and sometimes meta. Lift the payload so an agent reads
    # result["data"] directly instead of result["data"]["data"], and keep the
    # useful siblings at the top level. Any sibling the API does not send is
    # simply absent from the envelope.
    envelope: dict = {"success": True}
    if isinstance(data, dict) and "data" in data:
        envelope["data"] = data["data"]
        for extra in ("meta", "cached", "fetched_at", "request_id"):
            if extra in data:
                envelope[extra] = data[extra]
    else:
        envelope["data"] = data
    return json.dumps(envelope)


# ---------------------------------------------------------------------------
# Argument schemas
# ---------------------------------------------------------------------------

_DETAIL_SECTIONS = (
    "zestimate|price-history|tax-history|schools|photos|agent|nearby|"
    "open-houses|facts"
)


class ZillapiPropertyByAddressToolSchema(BaseModel):
    """Input for ZillapiPropertyByAddressTool."""

    address: str = Field(
        ...,
        min_length=6,
        description=(
            "Full street address including city, state and ZIP, for example "
            "'350 5th Ave, New York NY 10118'"
        ),
    )
    status: str = Field(
        default="FOR_SALE",
        pattern="^(FOR_SALE|RECENTLY_SOLD|FOR_RENT)$",
        description='Listing status to resolve: "FOR_SALE", "RECENTLY_SOLD" or "FOR_RENT"',
    )
    fields: Optional[str] = Field(
        default=None,
        description=(
            "Optional comma separated dotted paths to return only selected fields, "
            "for example 'zpid,price,bedrooms'"
        ),
    )


class ZillapiPropertyByZpidToolSchema(BaseModel):
    """Input for ZillapiPropertyByZpidTool."""

    zpid: str = Field(
        ...,
        min_length=1,
        pattern=r"^\d+$",
        description="Zillow property id, digits only, for example '1234567'",
    )
    fields: Optional[str] = Field(
        default=None,
        description=(
            "Optional comma separated dotted paths to return only selected fields, "
            "for example 'zpid,price,livingArea'"
        ),
    )


class ZillapiPropertyDetailToolSchema(BaseModel):
    """Input for ZillapiPropertyDetailTool."""

    zpid: str = Field(
        ...,
        min_length=1,
        pattern=r"^\d+$",
        description="Zillow property id, digits only, for example '1234567'",
    )
    section: str = Field(
        ...,
        pattern=f"^({_DETAIL_SECTIONS})$",
        description=(
            "Which detail section to fetch: 'zestimate' (value and rent estimate), "
            "'price-history', 'tax-history', 'schools', 'photos', 'agent' (listing "
            "agent and broker), 'nearby' (comparable homes), 'open-houses', or "
            "'facts' (full MLS attribute set)"
        ),
    )


class ZillapiPropertyByUrlToolSchema(BaseModel):
    """Input for ZillapiPropertyByUrlTool."""

    url: str = Field(
        ...,
        min_length=12,
        description=(
            "A zillow.com property URL: a /homedetails/ page, or a /b/, "
            "/apartments/ or /community/ building page"
        ),
    )
    status: str = Field(
        default="FOR_SALE",
        pattern="^(FOR_SALE|RECENTLY_SOLD|FOR_RENT)$",
        description='Listing status to resolve: "FOR_SALE", "RECENTLY_SOLD" or "FOR_RENT"',
    )
    extract_units: Optional[str] = Field(
        default=None,
        pattern="^(disabled|all|for_sale|recently_sold|for_rent|off_market)$",
        description=(
            "For a multi unit building URL, which units to extract: 'all', "
            "'for_sale', 'recently_sold', 'for_rent', 'off_market', or 'disabled' "
            "to return the building record only"
        ),
    )
    fields: Optional[str] = Field(
        default=None,
        description="Optional comma separated dotted paths to return only selected fields",
    )


class ZillapiListingSearchToolSchema(BaseModel):
    """Input for ZillapiListingSearchTool."""

    west: float = Field(..., description="Bounding box west longitude in decimal degrees")
    south: float = Field(..., description="Bounding box south latitude in decimal degrees")
    east: float = Field(..., description="Bounding box east longitude in decimal degrees")
    north: float = Field(..., description="Bounding box north latitude in decimal degrees")
    status: str = Field(
        default="for_sale",
        pattern="^(for_sale|for_rent|sold)$",
        description='Listing status: "for_sale", "for_rent" or "sold"',
    )
    price_min: Optional[int] = Field(default=None, description="Minimum price in dollars")
    price_max: Optional[int] = Field(default=None, description="Maximum price in dollars")
    beds_min: Optional[int] = Field(default=None, description="Minimum bedroom count")
    beds_max: Optional[int] = Field(default=None, description="Maximum bedroom count")
    baths_min: Optional[int] = Field(default=None, description="Minimum bathroom count")
    baths_max: Optional[int] = Field(default=None, description="Maximum bathroom count")
    sqft_min: Optional[int] = Field(default=None, description="Minimum living area in square feet")
    sqft_max: Optional[int] = Field(default=None, description="Maximum living area in square feet")
    home_types: Optional[List[str]] = Field(
        default=None,
        description=(
            "Home types to include, any of: house, condo, townhouse, multi_family, "
            "manufactured, lot, apartment"
        ),
    )
    max_items: int = Field(
        default=20,
        ge=1,
        le=500,
        description=(
            "Maximum listings to return, 1 to 500. Values of 50 or less return "
            "results inline. Above 50 the search runs asynchronously and returns "
            "a job_id to poll with the Zillapi Job tool"
        ),
    )


class ZillapiJobToolSchema(BaseModel):
    """Input for ZillapiJobTool."""

    job_id: str = Field(..., min_length=1, description="The job_id returned by an async search")
    action: str = Field(
        default="status",
        pattern="^(status|results)$",
        description=(
            "'status' to check whether the job has finished, or 'results' to fetch "
            "the rows once its status is succeeded"
        ),
    )


class ZillapiAccountToolSchema(BaseModel):
    """Input for ZillapiAccountTool."""

    resource: str = Field(
        default="account",
        pattern="^(account|usage)$",
        description=(
            "'account' for the plan summary and remaining credit balance, or "
            "'usage' for recent metered request rows"
        ),
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

if BaseTool is not None:

    class _ZillapiBaseTool(BaseTool):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        package_dependencies: list = Field(default_factory=lambda: ["requests"])
        env_vars: list = Field(default_factory=lambda: list(_ENV_VARS))

    class ZillapiPropertyByAddressTool(_ZillapiBaseTool):
        """Resolve a street address to a full property record."""

        name: str = "Zillapi Property Lookup by Address"
        description: str = (
            "Resolves a full street address to a Zillow property record: price, "
            "beds, baths, living area, year built, Zestimate, description, photos "
            "and the zpid. Use when the task names a property by address and you "
            "need its data or its zpid. Needs street, city, state and ZIP. "
            "Costs 3 credits per successful call because the address is resolved "
            "upstream. Failed calls are free."
        )
        args_schema: Type[BaseModel] = ZillapiPropertyByAddressToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiPropertyByAddressToolSchema(**kwargs)
            return _request(
                "GET",
                "/v1/properties/by-address",
                params={
                    "address": args.address,
                    "status": args.status,
                    "fields": args.fields,
                },
            )

    class ZillapiPropertyByZpidTool(_ZillapiBaseTool):
        """Fetch a full property record by zpid."""

        name: str = "Zillapi Property by zpid"
        description: str = (
            "Fetches the full Zillow property record for a known zpid: price, "
            "beds, baths, living area, lot size, year built, Zestimate, status, "
            "address and description. Use when you already have a zpid, for "
            "example from a search or an address lookup. Costs 1 credit per "
            "successful call. Failed calls are free."
        )
        args_schema: Type[BaseModel] = ZillapiPropertyByZpidToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiPropertyByZpidToolSchema(**kwargs)
            return _request(
                "GET",
                f"/v1/properties/{args.zpid}",
                params={"fields": args.fields},
            )

    class ZillapiPropertyDetailTool(_ZillapiBaseTool):
        """Fetch one detail section of a property by zpid."""

        name: str = "Zillapi Property Detail Section"
        description: str = (
            "Fetches one focused detail section for a property by zpid, instead "
            "of the whole record. Sections: 'zestimate' (value and rent "
            "estimate), 'price-history', 'tax-history', 'schools' (assigned and "
            "nearby, with ratings), 'photos', 'agent' (listing agent and broker), "
            "'nearby' (comparable homes), 'open-houses', and 'facts' (the full "
            "MLS attribute set). Use when the task needs one dimension of a "
            "property, such as valuation, school quality or sale history. Costs "
            "1 credit per successful call. Failed calls are free."
        )
        args_schema: Type[BaseModel] = ZillapiPropertyDetailToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiPropertyDetailToolSchema(**kwargs)
            return _request("GET", f"/v1/properties/{args.zpid}/{args.section}")

    class ZillapiPropertyByUrlTool(_ZillapiBaseTool):
        """Resolve a Zillow URL to a property or building record."""

        name: str = "Zillapi Property Lookup by URL"
        description: str = (
            "Resolves a zillow.com property URL to a full property record. Works "
            "with /homedetails/ pages and with /b/, /apartments/ and /community/ "
            "building pages, where it can return every unit in the building. Use "
            "when the task supplies a Zillow link rather than an address or a "
            "zpid. Costs 1 credit per record returned, so a building URL that "
            "returns many units costs 1 credit per unit. Failed calls are free."
        )
        args_schema: Type[BaseModel] = ZillapiPropertyByUrlToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiPropertyByUrlToolSchema(**kwargs)
            return _request(
                "GET",
                "/v1/properties/by-url",
                params={
                    "url": args.url,
                    "status": args.status,
                    "extract_units": args.extract_units,
                    "fields": args.fields,
                },
            )

    class ZillapiListingSearchTool(_ZillapiBaseTool):
        """Search for sale, for rent or sold listings inside a bounding box."""

        name: str = "Zillapi Listing Search"
        description: str = (
            "Searches Zillow listings inside a geographic bounding box, with "
            "filters for status (for_sale, for_rent, sold), price, beds, baths, "
            "living area and home type. A bounding box of west, south, east and "
            "north in decimal degrees is REQUIRED: a place name alone is not "
            "accepted, so convert the place to coordinates first. Use when the "
            "task asks what is on the market in an area rather than about one "
            "known property. Costs 1 credit per listing returned, so keep "
            "max_items tight. Failed calls are free. Requests above 50 listings "
            "run asynchronously and return a job_id to poll with the Zillapi Job "
            "tool."
        )
        args_schema: Type[BaseModel] = ZillapiListingSearchToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiListingSearchToolSchema(**kwargs)
            filters: dict = {
                "status": args.status,
                "bbox": {
                    "west": args.west,
                    "south": args.south,
                    "east": args.east,
                    "north": args.north,
                },
            }
            for name, low, high in (
                ("price", args.price_min, args.price_max),
                ("beds", args.beds_min, args.beds_max),
                ("baths", args.baths_min, args.baths_max),
                ("sqft", args.sqft_min, args.sqft_max),
            ):
                span = {k: v for k, v in (("min", low), ("max", high)) if v is not None}
                if span:
                    filters[name] = span
            if args.home_types:
                filters["homeTypes"] = args.home_types
            return _request(
                "POST",
                "/v1/search",
                json_body={"filters": filters, "maxItems": args.max_items},
            )

    class ZillapiJobTool(_ZillapiBaseTool):
        """Poll an async Zillapi job and read its results."""

        name: str = "Zillapi Job"
        description: str = (
            "Checks the status of an asynchronous Zillapi job and fetches its "
            "results once it has succeeded. Use after a listing search that "
            "returned a job_id instead of inline results. Poll with "
            "action='status' until the status is succeeded, then call again with "
            "action='results'. Costs 0 credits: reading a job is free, and the "
            "search itself was already billed when the job completed."
        )
        args_schema: Type[BaseModel] = ZillapiJobToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiJobToolSchema(**kwargs)
            suffix = "/results" if args.action == "results" else ""
            return _request("GET", f"/v1/jobs/{args.job_id}{suffix}")

    class ZillapiAccountTool(_ZillapiBaseTool):
        """Read the Zillapi account, plan, credit balance and recent usage."""

        name: str = "Zillapi Account and Usage"
        description: str = (
            "Returns the authenticated Zillapi account with its plan and "
            "remaining credit balance, or the recent metered usage rows. Use to "
            "check the remaining balance before running an expensive search, or "
            "to report what a run has spent. Costs 0 credits: this is a free "
            "control endpoint."
        )
        args_schema: Type[BaseModel] = ZillapiAccountToolSchema

        def _run(self, **kwargs: Any) -> str:
            args = ZillapiAccountToolSchema(**kwargs)
            path = "/v1/usage" if args.resource == "usage" else "/v1/me"
            return _request("GET", path)
