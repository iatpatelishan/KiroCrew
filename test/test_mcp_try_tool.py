"""``POST /api/mcp/try-tool``, the MCP Servers page's Try a tool panel.

Off (the default) the path is not registered, so it answers as any unknown route.
On it is owner-only and runs the server through the discovery probe, with one
``tools/call`` after the listing: a tool not declared read-only needs a confirm,
every run and refusal is in the security event log, a run is stopped at the time
limit or on a cancel with its whole process tree, and the result is redacted and
then cut to size. Each test starts a real, tiny MCP server written under
``tmp_path``.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest_floor_determinism import _pool_activity
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew import platform_compat
from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

pytestmark = pytest.mark.asyncio

_PATH = "/api/mcp/try-tool"

#: A stdio MCP server driven by a JSON file. It records its pid and every message it
#: reads; ``block`` leaves ``tools/call`` unanswered until stdin closes, ``list_error``
#: answers ``tools/list`` with that RPC error, ``grandchild`` starts a sleeper first.
_SERVER_SOURCE = """\
import json, os, subprocess, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
record = sys.argv[2]

def note(entry):
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")

pgid = os.getpgid(0) if hasattr(os, "getpgid") else None
note({"pid": os.getpid(), "pgid": pgid})
if cfg.get("grandchild"):
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    note({"grandchild": sleeper.pid})
for line in sys.stdin:
    msg = json.loads(line)
    note({"method": msg.get("method"), "params": msg.get("params")})
    if "id" not in msg:
        continue
    method, reply = msg.get("method"), {"jsonrpc": "2.0", "id": msg["id"]}
    if method == "initialize":
        reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {}}
    elif method == "tools/list" and "list_error" in cfg:
        reply["error"] = cfg["list_error"]
    elif method == "tools/list":
        reply["result"] = {"tools": cfg["tools"]}
    elif method == "tools/call" and cfg.get("block"):
        continue
    elif method == "tools/call":
        reply["result"] = cfg["answer"]
    else:
        reply["error"] = {"code": -32601, "message": "no such method"}
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()
"""


class _FakeState:
    owner_id = ""


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for token_auth_middleware, as in the owner-gate invariant test.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agent_config_routes.register(app)
    return app


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class _Server:
    """The fake server's script, its config and what it recorded."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.script = root / "server.py"
        self.script.write_text(_SERVER_SOURCE, encoding="utf-8")
        self.config = root / "config.json"
        self.record = root / "record.jsonl"
        self.set(tools=[], answer=_text("done"))

    def set(self, **cfg: Any) -> None:
        self.config.write_text(json.dumps(cfg), encoding="utf-8")

    @property
    def argv(self) -> list[str]:
        return [str(self.script), str(self.config), str(self.record)]

    def entries(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        lines = self.record.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def methods(self) -> list[str]:
        return [e["method"] for e in self.entries() if "method" in e]

    def pids(self) -> list[int]:
        found = [e.get("pid") for e in self.entries()] + [
            e.get("grandchild") for e in self.entries()
        ]
        return [pid for pid in found if isinstance(pid, int)]


def _gone(pid: int) -> bool:
    # A killed orphan stays a zombie where PID 1 does not reap; it has exited.
    if platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD:
        return True
    return platform_compat.pid_is_zombie(pid) is True


class _Sel:
    """Records what the route writes to the security event log."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_critical = False

    def log_tool_invocation(self, **kw: Any) -> None:
        if kw.get("critical") and self.fail_critical:
            raise OSError("security log unwritable")
        self.events.append(kw)

    def outcomes(self) -> list[tuple[str, str]]:
        return [(e["outcome"], e["error"]) for e in self.events]


@pytest.fixture
def server(_floor_monkeypatch, tmp_path):
    """The route switched on, a fake server named ``s`` configured, nothing switched off."""
    from kiro_crew import cron_script, mcp_cleanup, mcp_discovery, sel

    fake = _Server(tmp_path / "srv")
    _floor_monkeypatch.setattr(agent_config_routes, "tool_tester_enabled", lambda: True)
    _floor_monkeypatch.setattr(
        mcp_discovery,
        "list_servers",
        lambda: [McpServerInfo(name="s", command=sys.executable, args=fake.argv)],
    )
    _floor_monkeypatch.setattr(mcp_cleanup, "mcp_server_muted_in_any_store", lambda *a, **k: False)
    _floor_monkeypatch.setattr(cron_script, "_switched_off_tools", lambda server: frozenset())

    # The same server for a client that reads the cron agent spec, so both launchers see it.
    @functools.lru_cache(maxsize=16)
    def resolve(name: str):
        return ((sys.executable, *fake.argv), {}) if name == "s" else None

    _floor_monkeypatch.setattr(cron_script, "_resolve_mcp_server", resolve)
    _floor_monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, mode: (argv, None))
    _floor_monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: argv)
    log = _Sel()
    _floor_monkeypatch.setattr(sel, "sel", lambda: log)
    fake.sel = log
    try:
        yield fake
    finally:
        for pid in fake.pids():
            if platform_compat.IS_POSIX and not _gone(pid):
                platform_compat.kill_pid(pid, platform_compat.SIGKILL)


async def _post(body: object, **headers: str) -> tuple[int, Any]:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(_PATH, json=body, headers=headers)
        text = await resp.text()
        return resp.status, (json.loads(text) if text.startswith("{") else text)


async def _all_gone(fake: _Server) -> None:
    await async_wait_until(
        lambda: fake.pids() and all(_gone(pid) for pid in fake.pids()),
        describe=lambda: f"still running: {[p for p in fake.pids() if not _gone(p)]}",
        off_loop=True,
    )


async def _probe_pool_idle() -> None:
    await async_wait_until(
        lambda: "_mcp_probe_pool" not in _pool_activity(),
        describe=lambda: f"probe pool still busy: {_pool_activity()}",
    )


async def test_off_the_path_answers_as_an_unknown_route() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        unknown = await client.post("/api/mcp/no-such-route", json={"server": "x"})
        tester = await client.post(_PATH, json={"server": "x"})
        assert (tester.status, await tester.text()) == (unknown.status, await unknown.text())
        assert tester.status == 404


async def test_a_plain_probe_sends_no_tool_call(tmp_path) -> None:
    """Pinned on main: the probe's own handshake is unchanged without a step."""
    fake = _Server(tmp_path / "plain")
    fake.set(tools=[{"name": "t"}], answer=_text("done"))
    probed = await probe_server(McpServerInfo(name="p", command=sys.executable, args=fake.argv))
    assert probed.status == "ok"
    assert probed.tools == ["t"]
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/list"]
    await _all_gone(fake)


async def test_a_probe_step_calls_one_tool_after_the_listing(tmp_path) -> None:
    from kiro_crew.mcp_discovery import ToolCallStep

    fake = _Server(tmp_path / "step")
    fake.set(tools=[{"name": "t"}], answer=_text("done"))
    seen: list[list[Any]] = []

    def choose(tools: list[Any]) -> dict[str, Any]:
        seen.append(tools)
        return {"name": "t", "arguments": {"a": 1}}

    step = ToolCallStep(choose)
    server = McpServerInfo(name="p", command=sys.executable, args=fake.argv)
    probed = await probe_server(server, client_info={"name": "x", "version": "1"}, tool_call=step)
    assert probed.status == "ok"
    assert seen == [[{"name": "t"}]]
    assert step.answer is not None and step.answer["result"] == _text("done")
    assert fake.methods()[-1] == "tools/call"
    assert fake.entries()[-1]["params"] == {"name": "t", "arguments": {"a": 1}}
    await _all_gone(fake)


async def test_on_a_non_owner_is_refused_before_anything_starts(server) -> None:
    status, body = await _post({"server": "s", "tool": "t"}, **{"X-Test-User": "someone-else"})
    assert status in (401, 403)
    assert body["code"] == "owner_only"
    assert server.entries() == []


async def test_without_a_tool_it_lists_tools_with_their_confirm_need(server) -> None:
    server.set(
        tools=[
            {
                "name": "read",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {"type": "object"},
            },
            {"name": "write", "annotations": {"readOnlyHint": False, "destructiveHint": False}},
            {"name": "bare", "description": "no annotations"},
        ]
    )
    status, body = await _post({"server": "s"})
    assert status == 200
    assert [(r["name"], r["needsConfirm"]) for r in body["tools"]] == [
        ("read", False),
        ("write", True),
        ("bare", True),
    ]
    assert body["tools"][0]["inputSchema"] == {"type": "object"}
    assert "tools/call" not in server.methods()
    assert server.sel.events == []
    await _all_gone(server)


@pytest.mark.parametrize(
    "annotations",
    [None, {}, {"readOnlyHint": False}, {"destructiveHint": True}, {"readOnlyHint": "true"}],
)
async def test_a_tool_not_declared_read_only_is_refused_without_the_confirm(
    server, annotations
) -> None:
    tool: dict[str, Any] = {"name": "t"}
    if annotations is not None:
        tool["annotations"] = annotations
    server.set(tools=[tool], answer=_text("done"))
    status, body = await _post({"server": "s", "tool": "t", "arguments": {"a": 1}})
    assert status == 409
    assert body["code"] == "confirm_required"
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "confirm_required")]

    status, body = await _post({"server": "s", "tool": "t", "arguments": {"a": 1}, "confirm": True})
    assert status == 200
    assert server.entries()[-1]["params"] == {"name": "t", "arguments": {"a": 1}}
    assert server.sel.outcomes()[1:] == [("invoked", ""), ("completed", "")]
    await _all_gone(server)


async def test_every_call_is_in_the_security_log_written_ahead(server) -> None:
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    status, _ = await _post({"server": "s", "tool": "t", "confirm": True})
    assert status == 200
    invoked, completed = server.sel.events
    for event in (invoked, completed):
        assert event["session_key"] == "dashboard"
        assert event["source"] == "dashboard"
        assert event["tool_name"] == "s/t"
        assert event["tool_kind"] == "dashboard_mcp_tool"
        assert event["metadata"] == {"confirm": True}
        assert event["critical"] is True
    assert (invoked["outcome"], completed["outcome"]) == ("invoked", "completed")


async def test_a_call_that_cannot_be_recorded_never_runs(server) -> None:
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    server.sel.fail_critical = True
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (503, "audit_unavailable")
    assert "tools/call" not in server.methods()
    await _all_gone(server)


async def test_a_tool_error_is_recorded_as_an_error(server) -> None:
    answer = {"content": [{"type": "text", "text": "no"}], "isError": True}
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=answer)
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["isError"]) == (200, True)
    assert [o for o, _ in server.sel.outcomes()] == ["invoked", "error"]


async def test_a_read_only_tool_runs_without_a_confirm(server) -> None:
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("done"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert body["isError"] is False
    assert json.loads(body["result"]) == _text("done")
    assert server.sel.events[0]["metadata"] == {"confirm": False}


async def test_a_switched_off_tool_is_refused(server, _floor_monkeypatch) -> None:
    from kiro_crew import cron_script

    _floor_monkeypatch.setattr(cron_script, "_switched_off_tools", lambda s: frozenset({"t"}))
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}])
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert status == 403
    assert body["code"] == "tool_switched_off"
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "tool_switched_off")]


@pytest.mark.parametrize("muted_in", ["row", "scope"])
async def test_a_switched_off_server_is_refused_before_it_starts(
    server, _floor_monkeypatch, muted_in
) -> None:
    from kiro_crew import mcp_cleanup, mcp_discovery

    row = McpServerInfo(name="s", command=sys.executable, args=server.argv)
    if muted_in == "row":
        row.disabled = True
    else:
        _floor_monkeypatch.setattr(
            mcp_cleanup, "mcp_server_muted_in_any_store", lambda *a, **k: True
        )
    _floor_monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [row])
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert (status, body["code"]) == (409, "server_disabled")
    assert server.entries() == []


async def test_a_remote_or_unknown_server_is_refused(server, _floor_monkeypatch) -> None:
    from kiro_crew import mcp_discovery

    remote = McpServerInfo(name="r", url="https://mcp.example.test/mcp")
    _floor_monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [remote])
    status, body = await _post({"server": "r"})
    assert (status, body["code"]) == (409, "not_local")
    status, body = await _post({"server": "nope"})
    assert (status, body["code"]) == (404, "unknown_server")


async def test_a_listing_error_is_a_coded_redacted_failure(server) -> None:
    token = github_classic(seed=5)
    server.set(list_error={"code": -32000, "message": f"bad key {token}"})
    status, body = await _post({"server": "s"})
    assert (status, body["code"]) == (502, "server_failed")
    assert body["error"].startswith("tools/list: bad key")
    assert token not in body["error"]
    status, body = await _post({"server": "s", "tool": "t", "confirm": True})
    assert (status, body["code"]) == (502, "server_failed")
    await _all_gone(server)


async def test_a_run_past_the_time_limit_stops_the_server(server, _floor_monkeypatch) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    # The fake call is never answered, so any limit expires.
    _floor_monkeypatch.setattr(module, "_CALL_TIMEOUT_SECS", 0.01)  # flake-ok: never answered
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], block=True)
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 504
    assert body["code"] == "timeout"
    # The cancel reached the probe's private task; its teardown ends the run off the loop.
    await _probe_pool_idle()
    if server.pids():
        await _all_gone(server)


async def test_a_cancelled_request_stops_the_server_and_what_it_started(server) -> None:
    if not platform_compat.IS_POSIX:
        pytest.skip("the grandchild is reaped through the probe's process group")
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], block=True, grandchild=True
    )
    async with TestClient(TestServer(_build_app())) as client:
        pending = asyncio.ensure_future(client.post(_PATH, json={"server": "s", "tool": "t"}))
        await async_wait_until(lambda: "tools/call" in server.methods(), off_loop=True)
        # A gateway shutdown cancels each connection's task, and the handler with it.
        for task in asyncio.all_tasks():
            if "RequestHandler.start" in getattr(task.get_coro(), "__qualname__", ""):
                task.cancel()
        with pytest.raises(Exception):
            await pending
    await _probe_pool_idle()
    assert len(server.pids()) == 2
    await _all_gone(server)


async def test_a_large_result_is_cut_with_a_note(server) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}}],
        answer=_text("x" * (module._MAX_RESULT_CHARS * 2)),
    )
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert len(body["result"]) == module._MAX_RESULT_CHARS
    assert body["truncated"] is True


async def test_a_credential_in_the_result_is_redacted(server) -> None:
    token = github_classic()
    server.set(
        tools=[{"name": "t", "annotations": {"readOnlyHint": True}, "description": token}],
        answer=_text(f"token={token}"),
    )
    status, body = await _post({"server": "s", "tool": "t"})
    assert status == 200
    assert token not in body["result"]
    status, body = await _post({"server": "s"})
    assert token not in body["tools"][0]["description"]


async def test_a_listing_is_redacted_and_bounded(server) -> None:
    secret = github_classic(seed=3)
    schema = {"type": "object", "properties": {"k": {"type": "string", "default": secret}}}
    tools: list[dict[str, Any]] = [{"name": "t", "inputSchema": schema}, {"name": "n" * 5000}]
    tools += [{"name": f"big{i}", "inputSchema": {"description": "x" * 20000}} for i in range(20)]
    server.set(tools=tools)
    status, body = await _post({"server": "s"})
    assert status == 200
    assert secret not in json.dumps(body)
    assert body["tools"][0]["inputSchema"]["properties"]["k"]["type"] == "string"
    assert all(len(row["name"]) <= 1000 for row in body["tools"])
    assert len(json.dumps(body)) < 512 * 1024
    # Every tool the server listed is either a row or counted as left out.
    assert len(body["tools"]) + body["leftOut"] == 22
    assert body["leftOut"] >= 1


async def test_tools_past_the_cap_are_counted_and_named_when_run(server) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    count = module._MAX_TOOLS + 5
    server.set(
        tools=[{"name": f"t{i}", "annotations": {"readOnlyHint": True}} for i in range(count)]
    )
    status, body = await _post({"server": "s"})
    assert status == 200
    assert len(body["tools"]) == module._MAX_TOOLS
    assert body["leftOut"] == 5

    status, body = await _post({"server": "s", "tool": f"t{count - 1}"})
    assert (status, body["code"]) == (404, "tool_not_listed")
    assert body["error"] == "not listed: over the limit"
    assert "tools/call" not in server.methods()

    status, body = await _post({"server": "s", "tool": "never-announced"})
    assert (status, body["code"]) == (404, "unknown_tool")


async def test_a_listing_inside_the_limits_reports_nothing_left_out(server) -> None:
    server.set(tools=[{"name": "t"}])
    status, body = await _post({"server": "s"})
    assert (status, body["leftOut"]) == (200, 0)


async def test_a_tool_name_the_redactor_would_change_is_counted_and_never_listed(server) -> None:
    secret = github_classic(seed=5)
    tools = [
        {"name": f"fetch_{secret}", "annotations": {"readOnlyHint": True}},
        {"name": f"{secret}", "annotations": {"readOnlyHint": True}},
        {"name": "ordinary-name", "annotations": {"readOnlyHint": True}},
    ]
    server.set(tools=tools)
    status, body = await _post({"server": "s"})
    assert status == 200
    assert secret not in json.dumps(body)
    assert [row["name"] for row in body["tools"]] == ["ordinary-name"]
    assert (body["withheld"], body["leftOut"]) == (2, 0)


async def test_a_withheld_tool_name_is_refused_when_it_is_the_tool_asked_for(server) -> None:
    secret = github_classic(seed=6)
    name = f"fetch_{secret}"
    server.set(tools=[{"name": name, "annotations": {"readOnlyHint": True}}])
    status, body = await _post({"server": "s", "tool": name, "confirm": True})
    assert (status, body["code"]) == (403, "tool_name_withheld")
    assert name not in json.dumps(body)
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "tool_name_withheld")]
    # The security log names the refused call without the credential the name carries.
    assert all(secret not in event["tool_name"] for event in server.sel.events)
    await _all_gone(server)


async def test_an_ordinary_listing_withholds_nothing(server) -> None:
    server.set(tools=[{"name": "read_file"}, {"name": "list-things.v2"}])
    status, body = await _post({"server": "s"})
    assert (status, body["withheld"], len(body["tools"])) == (200, 0, 2)


async def test_a_schema_the_gateway_discards_is_null_and_an_empty_one_is_kept(server) -> None:
    server.set(
        tools=[
            {"name": "none", "annotations": {"readOnlyHint": True}},
            {"name": "list", "inputSchema": ["a"]},
            {"name": "huge", "inputSchema": {"description": "x" * (64 * 1024)}},
            {"name": "empty", "inputSchema": {}},
        ]
    )
    status, body = await _post({"server": "s"})
    assert status == 200
    assert {row["name"]: row["inputSchema"] for row in body["tools"]} == {
        "none": None,
        "list": None,
        "huge": None,
        "empty": {},
    }


async def test_a_schema_that_stops_being_json_once_redacted_is_null(
    server, _floor_monkeypatch
) -> None:
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    real = module._redacted
    _floor_monkeypatch.setattr(
        module, "_redacted", lambda text: "{" if text.startswith('{"type"') else real(text)
    )
    server.set(tools=[{"name": "t", "inputSchema": {"type": "object"}}])
    status, body = await _post({"server": "s"})
    assert (status, body["tools"][0]["inputSchema"]) == (200, None)


@pytest.mark.parametrize("body", [{}, {"server": ""}, {"server": "s", "arguments": [1]}])
async def test_a_malformed_request_is_refused(server, body) -> None:
    status, answer = await _post(body)
    assert status == 400
    assert answer["code"] == "bad_args"
    assert server.entries() == []


async def test_the_setting_is_off_unless_it_is_a_real_true() -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.section_builders import _build_mcp_config

    assert KiroCrewConfig().mcp.tool_tester is False
    assert _build_mcp_config({"tool_tester": "true"}).tool_tester is False
    assert _build_mcp_config({"tool_tester": True}).tool_tester is True


def _governed(_floor_monkeypatch: pytest.MonkeyPatch, governance: object) -> None:
    """The dashboard process's installed ceiling, as boot composes it."""
    import types

    from kiro_crew.platform import context as platform_context

    _floor_monkeypatch.setattr(
        platform_context, "current_context", lambda: types.SimpleNamespace(governance=governance)
    )


async def test_a_tool_the_governance_ceiling_denies_never_runs(server, _floor_monkeypatch) -> None:
    """An administrator's deny of ``@server/tool`` binds this run as it binds an agent's call."""
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy({"version": 1, "boot": {}, "mcp": {"mode": "deny", "deny": ["@s/t"]}})
    _governed(_floor_monkeypatch, ceiling)
    read_only = {"annotations": {"readOnlyHint": True}}
    server.set(tools=[{"name": "t", **read_only}, {"name": "u", **read_only}], answer=_text("ok"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (403, "governance_denied")
    assert "tools/call" not in server.methods()
    assert server.sel.outcomes() == [("denied", "governance_denied")]
    status, body = await _post({"server": "s", "tool": "u"})
    assert status == 200 and body["isError"] is False
    await _all_gone(server)


async def test_a_governance_check_that_cannot_run_refuses_the_call(
    server, _floor_monkeypatch
) -> None:
    """Fail closed: a ceiling that cannot be read is never read as no ceiling."""
    from kiro_crew.platform import context as platform_context

    def unreadable() -> object:
        raise RuntimeError("no composed context")

    _floor_monkeypatch.setattr(platform_context, "current_context", unreadable)
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    status, body = await _post({"server": "s", "tool": "t"})
    assert (status, body["code"]) == (503, "governance_unavailable")
    assert "tools/call" not in server.methods()
    await _all_gone(server)


async def test_a_timeout_reported_with_the_servers_output_is_still_a_timeout() -> None:
    """The probe appends the server's last output to an error; a timeout stays a timeout."""
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    probed = McpServerInfo(name="s", command="x")
    probed.status, probed.error = "error", "timeout\nstderr: still working"
    assert module._failure(probed)[:2] == (504, "timeout")
    probed.error = "tools/call: no response\nstderr: timeout"
    assert module._failure(probed)[:2] == (502, "server_failed")


async def test_the_tool_answer_is_read_against_the_run_limit(_floor_monkeypatch) -> None:
    """A tool slower than the handshake timeout still has the run's own limit to answer in."""
    import types

    from kiro_crew import mcp_discovery
    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    waited: list[float] = []
    answer = {"jsonrpc": "2.0", "id": probe._TOOL_CALL_ID, "result": {}}

    async def read(_stdout: object, timeout: float, **_kw: object) -> dict[str, Any]:
        waited.append(timeout)
        return answer

    class _Pipe:
        def write(self, _data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    _floor_monkeypatch.setattr(mcp_discovery, "_get_probe_timeout", lambda: 15.0)
    proc = types.SimpleNamespace(stdin=_Pipe(), stdout=None)
    step = ToolCallStep(  # flake-ok: the reader is faked, so no time passes against this limit
        lambda _tools: {"name": "t", "arguments": {}}, timeout=600.0
    )
    assert await probe._call_one_tool(proc, "s", step, [{"name": "t"}], [])
    assert step.answer == answer
    assert len(waited) == 1 and 500.0 < waited[0] <= 600.0
    # Without a limit of its own the step keeps the probe's timeout.
    waited.clear()
    plain = ToolCallStep(lambda _tools: {"name": "t", "arguments": {}})
    assert await probe._call_one_tool(proc, "s", plain, [{"name": "t"}], [])
    assert waited == [15.0]


async def test_the_profile_bound_to_the_dashboard_surface_governs_the_run(
    server, _floor_monkeypatch, tmp_path
) -> None:
    """The run is asked as the dashboard surface, so a profile bound to it denies the tool."""
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.sel import _infer_source

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    deny = {"mode": "deny", "deny": ["@s/t"]}
    profile = {"name": "dash", "bind": {"type": "surface", "id": "dashboard"}, "mcp": deny}
    (profiles / "dash.json").write_text(json.dumps(profile), encoding="utf-8")
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    _governed(_floor_monkeypatch, None)
    gp.reset_store()
    try:
        read_only = {"annotations": {"readOnlyHint": True}}
        server.set(tools=[{"name": "t", **read_only}], answer=_text("ok"))
        status, body = await _post({"server": "s", "tool": "t"})
    finally:
        gp.reset_store()
    assert status == 403, body
    assert body["code"] == "governance_denied"
    assert "tools/call" not in server.methods()
    await _all_gone(server)
    module = importlib.import_module("kiro_crew.dashboard.handlers.mcp_try_tool")
    assert _infer_source(module._SESSION_KEY) == "dashboard"


_ROUTES_PROBE = """
import sys
from aiohttp import web
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.routes import agent_config

loads = []
KiroCrewConfig.load = classmethod(lambda cls, *a, **k: loads.append(1))
agent_config.register(web.Application())
print(len(loads), "kiro_crew.dashboard.handlers.mcp_try_tool" in sys.modules)
"""


async def test_registering_the_routes_loads_no_config_and_no_tester_module(tmp_path) -> None:
    """Start-up only adds the route: no config read, and none of the tester's code."""
    import os
    import subprocess

    env = {**os.environ, "HOME": str(tmp_path)}

    def boot() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - this interpreter, fixed argv
            [sys.executable, "-c", _ROUTES_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
        )

    proc = await asyncio.to_thread(boot)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "0 False"


async def test_the_setting_is_read_per_request_so_it_needs_no_restart(
    server, _floor_monkeypatch
) -> None:
    """One registered app answers as an unknown route, then runs once the setting is on."""
    flag = [False]
    _floor_monkeypatch.setattr(agent_config_routes, "tool_tester_enabled", lambda: flag[0])
    server.set(tools=[{"name": "t", "annotations": {"readOnlyHint": True}}], answer=_text("ok"))
    async with TestClient(TestServer(_build_app())) as client:
        off = await client.post(_PATH, json={"server": "s"})
        assert off.status == 404
        flag[0] = True
        on = await client.post(_PATH, json={"server": "s"})
        assert on.status == 200
        assert [row["name"] for row in (await on.json())["tools"]] == ["t"]
    await _all_gone(server)


class _RecordingPipe:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


async def test_a_cancel_queued_while_the_call_is_chosen_sends_nothing(
    _floor_monkeypatch,
) -> None:
    """A cancel that arrives while ``choose`` blocks stops the run before the call is written."""
    import types

    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    async def read(_stdout: object, _timeout: float, **_kw: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": probe._TOOL_CALL_ID, "result": {}}

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    pipe = _RecordingPipe()
    proc = types.SimpleNamespace(stdin=pipe, stdout=None)

    def choose(_tools: list[Any]) -> dict[str, Any]:
        # As the probe's private loop gets a request's cancel: queued, not yet run.
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return {"name": "t", "arguments": {}}

    with pytest.raises(asyncio.CancelledError):
        await asyncio.ensure_future(
            probe._call_one_tool(proc, "s", ToolCallStep(choose), [{"name": "t"}], [])
        )
    assert pipe.written == []


async def test_a_run_whose_limit_ran_out_while_choosing_sends_nothing(_floor_monkeypatch) -> None:
    """With no time left once the call is chosen, nothing is written and it reads as a timeout."""
    import types

    from kiro_crew import mcp_discovery_probe as probe
    from kiro_crew.mcp_discovery import ToolCallStep

    async def read(_stdout: object, _timeout: float, **_kw: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": probe._TOOL_CALL_ID, "result": {}}

    _floor_monkeypatch.setattr(probe, "_read_stdio_jsonrpc_response", read)
    pipe = _RecordingPipe()
    proc = types.SimpleNamespace(stdin=pipe, stdout=None)
    step = ToolCallStep(lambda _tools: {"name": "t", "arguments": {}}, timeout=0)
    with pytest.raises(asyncio.TimeoutError):
        await probe._call_one_tool(proc, "s", step, [{"name": "t"}], [])
    assert pipe.written == []
