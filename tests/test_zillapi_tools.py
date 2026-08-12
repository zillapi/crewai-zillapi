"""Tests for crewai-zillapi.

Two properties matter most and are covered here:

1. The tools NEVER raise from execution. Every failure comes back as the
   JSON envelope {"success": false, "error": {...}}.
2. No text from an upstream response body is ever copied into a user facing
   error. The tests prove this with a synthetic marker, so the guarantee is
   asserted without naming any service internals.
"""

import json
import re
import sys
import types

import pytest

from crewai_zillapi import zillapi_tools as zt

# A stand in for whatever an upstream failure body might contain. It carries a
# unique token and a slug shape, so if any part of a response body were copied
# into a message the assertions below would catch it.
UPSTREAM_MARKER = "ZZ_UPSTREAM_MARKER_9f3a"
UPSTREAM_BODY_TEXT = f"{UPSTREAM_MARKER} vendorname/widget-fetcher finished with TIMED-OUT"


class _RequestException(Exception):
    pass


class _Timeout(_RequestException):
    pass


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def fake_requests(monkeypatch):
    """Install a stub requests module and return a setter for the handler."""
    state = {"handler": None}

    def request(*args, **kwargs):
        return state["handler"](*args, **kwargs)

    module = types.ModuleType("requests")
    exceptions = types.ModuleType("requests.exceptions")
    exceptions.RequestException = _RequestException
    exceptions.Timeout = _Timeout
    module.exceptions = exceptions
    module.request = request
    monkeypatch.setitem(sys.modules, "requests", module)
    monkeypatch.setenv(zt._ENV_KEY, "zk_test_key")

    def set_handler(handler):
        state["handler"] = handler

    return set_handler


def _decode(raw):
    """Every tool returns a JSON string. Decoding it must always work."""
    assert isinstance(raw, str)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Envelope never raises
# ---------------------------------------------------------------------------


def test_missing_key_returns_envelope_with_the_fix(monkeypatch):
    monkeypatch.delenv(zt._ENV_KEY, raising=False)
    result = _decode(zt._request("GET", "/v1/me"))
    assert result["success"] is False
    assert result["error"]["code"] == "missing_api_key"
    assert zt._ENV_KEY in result["error"]["message"]
    assert "https://zillapi.com" in result["error"]["message"]


def test_invalid_key_maps_to_actionable_message(fake_requests):
    fake_requests(
        lambda *a, **k: _FakeResponse(
            401, {"error": {"code": "invalid_api_key", "message": "nope"}}
        )
    )
    result = _decode(zt._request("GET", "/v1/me"))
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_api_key"
    assert "zk_" in result["error"]["message"]


def test_network_failure_returns_envelope(fake_requests):
    def boom(*args, **kwargs):
        raise _RequestException("connection refused to 10.0.0.1:8080")

    fake_requests(boom)
    result = _decode(zt._request("GET", "/v1/me"))
    assert result["success"] is False
    assert result["error"]["code"] == "network"
    # The raw exception text is not echoed back to the caller.
    assert "10.0.0.1" not in result["error"]["message"]


def test_timeout_returns_envelope(fake_requests):
    def boom(*args, **kwargs):
        raise _Timeout("timed out")

    fake_requests(boom)
    result = _decode(zt._request("GET", "/v1/me"))
    assert result["success"] is False
    assert result["error"]["code"] == "timeout"


def test_non_json_response_returns_envelope(fake_requests):
    fake_requests(lambda *a, **k: _FakeResponse(200, None, text="<html>oops</html>"))
    result = _decode(zt._request("GET", "/v1/me"))
    assert result["success"] is False
    assert result["error"]["code"] == "bad_response"


def test_success_wraps_data(fake_requests):
    fake_requests(lambda *a, **k: _FakeResponse(200, {"zpid": "1234567"}))
    result = _decode(zt._request("GET", "/v1/properties/1234567"))
    assert result["success"] is True
    assert result["data"]["zpid"] == "1234567"


def test_api_envelope_is_lifted_so_data_is_the_payload(fake_requests):
    """The API wraps payloads as {"data": ..., "request_id": ...}. Agents should
    read result["data"] directly, never result["data"]["data"]."""
    fake_requests(
        lambda *a, **k: _FakeResponse(
            200,
            {
                "data": {"zpid": "1234567", "price": 1250000},
                "request_id": "req_abc",
                "cached": True,
                "fetched_at": "2026-08-12T00:00:00Z",
                "meta": {"count": 1},
            },
        )
    )
    result = _decode(zt._request("GET", "/v1/properties/1234567"))
    assert result["data"] == {"zpid": "1234567", "price": 1250000}
    assert "data" not in result["data"]
    # Useful siblings survive at the top level.
    assert result["request_id"] == "req_abc"
    assert result["cached"] is True
    assert result["fetched_at"] == "2026-08-12T00:00:00Z"
    assert result["meta"] == {"count": 1}


def test_list_payload_is_lifted(fake_requests):
    fake_requests(
        lambda *a, **k: _FakeResponse(
            200, {"data": [{"zpid": "1"}, {"zpid": "2"}], "request_id": "req_x"}
        )
    )
    result = _decode(zt._request("POST", "/v1/search"))
    assert isinstance(result["data"], list)
    assert len(result["data"]) == 2


# ---------------------------------------------------------------------------
# Neutral error shield: no upstream text reaches the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_upstream_failure_body_is_never_echoed(fake_requests, status):
    fake_requests(
        lambda *a, **k: _FakeResponse(
            status,
            {"error": {"code": "upstream_failed", "message": UPSTREAM_BODY_TEXT}},
            text=UPSTREAM_BODY_TEXT,
        )
    )
    raw = zt._request("GET", "/v1/properties/1234567")
    assert UPSTREAM_MARKER not in raw
    assert "vendorname/widget-fetcher" not in raw
    result = _decode(raw)
    assert result["success"] is False
    assert result["error"]["code"] == "service_unavailable"
    assert result["error"]["message"] == zt._SERVICE_UNAVAILABLE


def test_client_error_body_is_never_echoed(fake_requests):
    fake_requests(
        lambda *a, **k: _FakeResponse(
            400, {"error": {"code": "invalid_filters", "message": UPSTREAM_BODY_TEXT}}
        )
    )
    raw = zt._request("POST", "/v1/search")
    assert UPSTREAM_MARKER not in raw
    result = _decode(raw)
    # The stable code is preserved because it is a plain machine token.
    assert result["error"]["code"] == "invalid_filters"
    # The message is ours, not theirs, and it carries the fix.
    assert "bounding box" in result["error"]["message"]


def test_malformed_error_code_is_replaced(fake_requests):
    fake_requests(
        lambda *a, **k: _FakeResponse(
            400, {"error": {"code": "vendorname/widget-fetcher", "message": "x"}}
        )
    )
    raw = zt._request("GET", "/v1/me")
    assert "vendorname/widget-fetcher" not in raw
    assert _decode(raw)["error"]["code"] == "http_400"


_URL_RE = re.compile(r"https://\S+")


def test_every_static_message_is_neutral():
    """No shipped message may carry a path or slug beyond our own links."""
    messages = list(zt._MESSAGE_BY_CODE.values()) + list(zt._MESSAGE_BY_STATUS.values())
    messages += [zt._SERVICE_UNAVAILABLE, zt._GENERIC_CLIENT_ERROR, zt._SIGNUP_LINE]
    assert messages, "the message tables must not be empty"
    for message in messages:
        assert "/" not in _URL_RE.sub("", message), message


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

crewai_installed = getattr(zt, "BaseTool", None) is not None

TOOLS = (
    "ZillapiPropertyByAddressTool",
    "ZillapiPropertyByZpidTool",
    "ZillapiPropertyDetailTool",
    "ZillapiPropertyByUrlTool",
    "ZillapiListingSearchTool",
    "ZillapiJobTool",
    "ZillapiAccountTool",
)


@pytest.mark.skipif(not crewai_installed, reason="crewai not installed")
@pytest.mark.parametrize("tool_name", TOOLS)
def test_every_tool_declares_its_credit_cost(tool_name):
    tool = getattr(zt, tool_name)()
    assert "credit" in tool.description.lower(), tool_name


@pytest.mark.skipif(not crewai_installed, reason="crewai not installed")
@pytest.mark.parametrize("tool_name", TOOLS)
def test_every_tool_surfaces_the_key_env_var(tool_name):
    tool = getattr(zt, tool_name)()
    names = [var.name for var in tool.env_vars]
    assert zt._ENV_KEY in names, tool_name
    onboarding = [v.description for v in tool.env_vars if v.name == zt._ENV_KEY][0]
    assert "100 credits" in onboarding
    assert "https://zillapi.com" in onboarding


@pytest.mark.skipif(not crewai_installed, reason="crewai not installed")
def test_search_builds_a_bounding_box_body(fake_requests):
    captured = {}

    def handler(method, url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _FakeResponse(200, {"results": []})

    fake_requests(handler)
    zt.ZillapiListingSearchTool()._run(
        west=-73.99, south=40.74, east=-73.98, north=40.75, beds_min=2, max_items=5
    )
    assert captured["filters"]["bbox"] == {
        "west": -73.99,
        "south": 40.74,
        "east": -73.98,
        "north": 40.75,
    }
    assert captured["filters"]["beds"] == {"min": 2}
    assert captured["maxItems"] == 5


@pytest.mark.skipif(not crewai_installed, reason="crewai not installed")
def test_tools_do_not_raise_on_failure(fake_requests):
    """The contract that matters: execution failures come back as envelopes."""

    def boom(*args, **kwargs):
        raise _RequestException("down")

    fake_requests(boom)
    calls = {
        "ZillapiPropertyByAddressTool": {"address": "350 5th Ave, New York NY 10118"},
        "ZillapiPropertyByZpidTool": {"zpid": "1234567"},
        "ZillapiPropertyDetailTool": {"zpid": "1234567", "section": "zestimate"},
        "ZillapiPropertyByUrlTool": {
            "url": "https://www.zillow.com/homedetails/1234567_zpid/"
        },
        "ZillapiListingSearchTool": {
            "west": -73.99,
            "south": 40.74,
            "east": -73.98,
            "north": 40.75,
        },
        "ZillapiJobTool": {"job_id": "job_123"},
        "ZillapiAccountTool": {},
    }
    for tool_name, kwargs in calls.items():
        result = _decode(getattr(zt, tool_name)()._run(**kwargs))
        assert result["success"] is False, tool_name
        assert result["error"]["code"] == "network", tool_name
