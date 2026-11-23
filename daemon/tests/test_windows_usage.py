#!/usr/bin/env python3
"""The limits-endpoint poll: weekly (all models) + a model-scoped week.

Claude dropped the 5-hour window. /api/oauth/usage now carries a `limits`
list: one `weekly_all`, and `weekly_scoped` entries naming a model (Fable).
The body below is trimmed from a real response (2026-11-23).

Run: python -m pytest daemon/tests/test_windows_usage.py -x -q
"""
import asyncio
import datetime
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon import claude_usage_daemon_windows as mod

NOW = time.time()


def _iso(offset_s: float) -> str:
    return datetime.datetime.fromtimestamp(NOW + offset_s, datetime.timezone.utc).isoformat()


def _body(scoped=True):
    limits = [{
        "kind": "weekly_all", "group": "weekly", "percent": 16, "severity": "normal",
        "resets_at": _iso(6360 * 60), "scope": None, "is_active": True,
    }]
    if scoped:
        limits.append({
            "kind": "weekly_scoped", "group": "weekly", "percent": 8, "severity": "normal",
            "resets_at": _iso(6359 * 60),
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        })
    return {"five_hour": None, "seven_day": {"utilization": 16.0}, "limits": limits}


def test_weekly_and_fable_fill_the_two_slots():
    p = mod.usage_payload_from_limits(_body(), now=NOW)
    assert (p["s"], p["w"]) == (16, 8)
    assert abs(p["sr"] - 6360) <= 1 and abs(p["wr"] - 6359) <= 1
    assert (p["l1"], p["t1"], p["l2"], p["t2"]) == ("Weekly", "7d", "Fable", "F")
    assert p["sw"] == 10080
    assert p["acct"] == "pro" and p["ok"] is True


def test_no_scoped_limit_leaves_slot_two_out():
    """The firmware hides slot 2 when "w" is absent -- not a 0% under no name."""
    p = mod.usage_payload_from_limits(_body(scoped=False), now=NOW)
    assert p["s"] == 16
    assert not {"w", "wr", "l2", "t2"} & p.keys()


@pytest.mark.parametrize("body", [{}, {"limits": None}, {"limits": []}, [], None,
                                  {"limits": [{"kind": "weekly_scoped", "percent": 5}]}])
def test_no_weekly_limit_means_fall_back(body):
    assert mod.usage_payload_from_limits(body, now=NOW) is None


def test_garbage_fields_do_not_raise():
    body = {"limits": [{"kind": "weekly_all", "percent": "x", "resets_at": "not a date"}]}
    p = mod.usage_payload_from_limits(body, now=NOW)
    assert p["s"] == 0 and p["sr"] == 0


def _client(get_resp=None, post_resp=None):
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.get = AsyncMock(return_value=get_resp)
    c.post = AsyncMock(return_value=post_resp)
    return c


def _resp(status, json_body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.text = "mocked"
    r.json = MagicMock(return_value=json_body)
    h = {k.lower(): v for k, v in (headers or {}).items()}
    r.headers = MagicMock()
    r.headers.get = lambda name, default=None: h.get(name.lower(), default)
    return r


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_poll_api_prefers_the_limits_endpoint(monkeypatch):
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    c = _client(get_resp=_resp(200, _body()))
    with patch("httpx.AsyncClient", return_value=c):
        p = _run(mod.poll_api("tok"))
    assert (p["s"], p["w"], p["l2"]) == (16, 8, "Fable")
    c.post.assert_not_called()   # no quota spent


def test_poll_api_falls_back_to_headers_without_5h(monkeypatch):
    """Endpoint refuses -> header poll. With no 5h header the week is slot 1;
    it must NOT be mistaken for an Enterprise account (the old else-branch)."""
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    post = _resp(200, headers={
        "anthropic-ratelimit-unified-7d-utilization": "0.16",
        "anthropic-ratelimit-unified-7d-reset": str(NOW + 3600),
        "anthropic-ratelimit-unified-7d-status": "allowed",
    })
    c = _client(get_resp=_resp(429), post_resp=post)
    with patch("httpx.AsyncClient", return_value=c):
        p = _run(mod.poll_api("tok"))
    assert p["acct"] == "pro"
    assert (p["s"], p["l1"], p["sw"]) == (16, "Weekly", 10080)
    assert "w" not in p


def test_auth_failure_on_the_endpoint_raises(monkeypatch):
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    c = _client(get_resp=_resp(401))
    with patch("httpx.AsyncClient", return_value=c):
        with pytest.raises(mod.AuthError):
            _run(mod.poll_api("tok"))
