"""POST /api/mcp/probe/{name}: test one MCP server, read-only for its config.

The route spawns only the named server. A disabled server is answered from
``probe_server``'s consent refusal, so the test asserts that the spawn helper
is never reached for it through this handler.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import mcp_probe


class _Server:
    def __init__(self, name: str, disabled: bool = False) -> None:
        self.name = name
        self.disabled = disabled
        self.status = "unknown"
        self.error = ""
        self.tools: list = []

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "error": self.error}


def _request(name: str) -> SimpleNamespace:
    return SimpleNamespace(match_info={"name": name})


@pytest.fixture
def owner(monkeypatch):
    import kiro_crew.dashboard.handlers._shared as shared

    monkeypatch.setattr(shared, "require_owner_dashboard_request", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_probe, "_read_global_mcps", lambda: {})
    monkeypatch.setattr(mcp_probe, "_kirocrew_mcp_servers", lambda: {})
    monkeypatch.setattr(mcp_probe, "_record_probe_verdicts", lambda rows: None)


@pytest.mark.asyncio
async def test_unknown_name_is_404(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda: [_Server("alpha")])
    resp = await mcp_probe.api_mcp_probe_one(_request("missing"))
    assert resp.status == 404
    assert json.loads(resp.body)["name"] == "missing"


@pytest.mark.asyncio
async def test_named_server_is_probed_and_its_row_returned(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    alpha, beta = _Server("alpha"), _Server("beta")

    async def fake_probe(server):
        server.status = "ok"
        return server

    spawned = AsyncMock(side_effect=fake_probe)
    monkeypatch.setattr(mcp_probe, "_probe_server", spawned)
    monkeypatch.setattr(disc, "list_servers", lambda: [alpha, beta])

    resp = await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert resp.status == 200
    row = json.loads(resp.body)
    assert (row["name"], row["status"], row["error"]) == ("alpha", "ok", "")
    spawned.assert_awaited_once_with(alpha)


@pytest.mark.asyncio
async def test_disabled_server_is_not_spawned(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    off = _Server("off", disabled=True)
    spawned = AsyncMock(side_effect=lambda s: s)
    monkeypatch.setattr(mcp_probe, "_probe_server", spawned)
    monkeypatch.setattr(disc, "list_servers", lambda: [off])

    resp = await mcp_probe.api_mcp_probe_one(_request("off"))

    spawned.assert_not_awaited()
    assert json.loads(resp.body)["status"] == "disabled"


@pytest.mark.asyncio
async def test_a_probed_server_gets_the_denied_env_note(monkeypatch, owner):
    import kiro_crew.mcp_discovery as disc

    alpha = _Server("alpha")
    noted: list[str] = []

    async def fake_probe(server):
        return server

    monkeypatch.setattr(mcp_probe, "_probe_server", fake_probe)
    monkeypatch.setattr(disc, "list_servers", lambda: [alpha])
    monkeypatch.setattr(disc, "_note_denied_env", lambda s: noted.append(s.name))

    await mcp_probe.api_mcp_probe_one(_request("alpha"))

    assert noted == ["alpha"]
