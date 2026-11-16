"""KAS approve-for-me: the engine switch, the per-session value, the audit row and the owner check."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp import kas_approve_for_me as afm


@pytest.fixture(autouse=True)
def _reset_owner_check():
    afm.reset_owner_check()
    yield
    afm.reset_owner_check()


def _option(value: str) -> list[dict[str, Any]]:
    return [{"id": "model", "currentValue": "auto"}, {"id": "approveForMe", "currentValue": value}]


class _Handle:
    def __init__(self, value: str | None, *, fail: bool = False, key: str = "dashboard:1"):
        self.config_options = [] if value is None else _option(value)
        self.writes: list[tuple[str, str]] = []
        self._fail = fail
        self._session_key = key

    async def set_config_option(self, config_id: str, value: str) -> None:
        if self._fail:
            raise RuntimeError("refused")
        self.writes.append((config_id, value))
        self.config_options = _option(value)


@pytest.mark.parametrize(
    ("current", "allowed", "target"),
    [
        (None, False, None),
        ("off", False, None),
        ("off", True, None),
        ("on", True, None),
        ("on", False, "off"),
        ("files", True, "off"),
        ("files", False, "off"),
        ("", True, "off"),
    ],
)
def test_a_session_is_held_on_or_off_never_files(current, allowed, target):
    assert afm.settle_target(current, allowed=allowed) == target


def test_settle_writes_off_over_files_and_sends_nothing_without_the_option():
    files = _Handle("files")
    asyncio.run(afm.settle(files))
    assert files.writes == [("approveForMe", "off")]
    none = _Handle(None)
    asyncio.run(afm.settle(none))
    assert none.writes == []


def test_settle_keeps_on_only_while_the_owner_allows_it():
    afm.register_owner_check(lambda key: key == "dashboard:1")
    kept = _Handle("on")
    asyncio.run(afm.settle(kept))
    assert kept.writes == []
    other = _Handle("on", key="slack:1")
    asyncio.run(afm.settle(other))
    assert other.writes == [("approveForMe", "off")]


def test_an_owner_check_that_raises_holds_the_session_off():
    def boom(_key: str) -> bool:
        raise ValueError("x")

    afm.register_owner_check(boom)
    handle = _Handle("on")
    asyncio.run(afm.settle(handle))
    assert handle.writes == [("approveForMe", "off")]


def test_an_on_session_that_cannot_be_turned_off_refuses_the_turn():
    with pytest.raises(afm.ApproveForMeNotOff):
        asyncio.run(afm.settle(_Handle("on", fail=True)))
    # A failed write over ``files`` is logged, not raised.
    asyncio.run(afm.settle(_Handle("files", fail=True)))


def test_the_engine_switch_overrides_an_inherited_value():
    env = {afm.CLASSIFIER_MODE_ENV: "enforce"}
    afm.force_off(env)
    assert env[afm.CLASSIFIER_MODE_ENV] == "off"
    afm.enable(env)
    assert env[afm.CLASSIFIER_MODE_ENV] == "enforce"


def test_only_the_kas_harness_turns_the_engine_switch_on(monkeypatch):
    from kiro_crew.acp.client import _resolve_spawn_env
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    inherited = {afm.CLASSIFIER_MODE_ENV: "enforce"}
    kas_env = dict(inherited)
    harness_for(ACP_BACKEND_KAS).apply_spawn_env(kas_env)
    assert kas_env[afm.CLASSIFIER_MODE_ENV] == "enforce"
    kiro_env = dict(inherited)
    harness_for(ACP_BACKEND_KIRO).apply_spawn_env(kiro_env)
    assert kiro_env[afm.CLASSIFIER_MODE_ENV] == "off"
    client_env = _resolve_spawn_env(dict(inherited))
    assert client_env[afm.CLASSIFIER_MODE_ENV] == "off"


def test_kas_is_asked_to_list_the_option():
    from kiro_crew.acp.types import KAS_CLIENT_CAPABILITIES

    assert KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]["approveForMeOption"] is True


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_tool_invocation(self, **kw: Any) -> None:
        self.rows.append(kw)


def _frame(call_id: str, approved: bool) -> dict[str, Any]:
    meta = {"kiro": {"trustClassifierApproved": True}} if approved else {"kiro": {}}
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": "Run: ls",
        "kind": "execute",
        "_meta": meta,
    }


def test_a_kas_approved_call_gets_one_audit_row(monkeypatch):
    sink = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sink)
    handle = SimpleNamespace(_session_key="dashboard:1", _session_id="s1", active_agent="kirocrew")
    afm.audit_classifier_approved(handle, _frame("t1", True))
    afm.audit_classifier_approved(handle, {**_frame("t1", True), "sessionUpdate": "tool_call_update"})
    afm.audit_classifier_approved(handle, _frame("t2", False))
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["outcome"] == "auto_approved"
    assert row["source"] == afm.SEL_SOURCE
    assert row["request_id"] == "t1"
    assert row["session_key"] == "dashboard:1"
    assert row["metadata"]["crew_gate"] == "skipped"


def test_an_approved_call_after_leave_is_withdrawn_cancels_the_turn_once(monkeypatch):
    sink = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sink)
    cancels: list[int] = []

    class _Live:
        _session_key = "dashboard:1"
        _session_id = "s1"
        active_agent = "kirocrew"

        def __init__(self) -> None:
            self._audit_tasks: set[Any] = set()

        async def cancel(self) -> None:
            cancels.append(1)

    async def run(allowed: bool) -> None:
        afm.register_owner_check(lambda _key: allowed)
        handle = _Live()
        afm.audit_classifier_approved(handle, _frame("t1", True))
        afm.audit_classifier_approved(handle, _frame("t2", True))
        while handle._audit_tasks:
            await asyncio.gather(*list(handle._audit_tasks))

    asyncio.run(run(True))
    assert cancels == []
    asyncio.run(run(False))
    assert cancels == [1]
    assert len(sink.rows) == 4


def test_the_policy_permits_approve_for_me_exactly_where_it_permits_trust(monkeypatch):
    from kiro_crew import approval_mode_policy as amp

    monkeypatch.setattr(amp, "_installing", False)
    monkeypatch.setattr(amp, "_resolved", True)
    monkeypatch.setattr(amp, "_denied", frozenset({"trust"}))
    assert amp.session_mode_permitted("approve_for_me") is False
    assert "approve_for_me" in amp.disabled_session_modes()
    monkeypatch.setattr(amp, "_denied", frozenset())
    assert amp.session_mode_permitted("approve_for_me") is True
    assert amp.clamp_session_mode("trust") == "trust"


def _state(slots: list[Any], *, yolo: bool = False, keys: set[str] | None = None) -> Any:
    state = SimpleNamespace(
        sessions=SimpleNamespace(_fold_key=lambda key: key),
        _slots={s.key: s for s in slots},
        is_yolo_active=lambda: yolo,
        yolo_reaches=lambda _slot: True,
    )
    if keys is not None:
        state._approve_for_me_keys = keys
    return state


def _slot(key: str, **flags: Any) -> Any:
    return SimpleNamespace(
        key=key,
        linked_session_key="",
        _trust=flags.get("trust", False),
        _trust_reads=flags.get("reads", False),
        _trust_scope="",
    )


@pytest.fixture
def _permitted(monkeypatch):
    from kiro_crew.dashboard import approve_for_me as dafm

    monkeypatch.setattr(dafm, "approve_for_me_permitted", lambda: True)
    monkeypatch.setattr(dafm, "effective_session_key", lambda slot: slot.key)
    return dafm


def test_the_owner_allows_only_a_session_a_person_turned_on(_permitted):
    dafm = _permitted
    slot = _slot("dashboard:1")
    assert dafm.owner_allows(_state([slot], keys={"dashboard:1"}), "dashboard:1") is True
    assert dafm.owner_allows(_state([slot]), "dashboard:1") is False
    # A key no live slot addresses is held off.
    assert dafm.owner_allows(_state([], keys={"dashboard:1"}), "dashboard:1") is False


@pytest.mark.parametrize("flags", [{"trust": True}, {"reads": True}])
def test_trust_or_reads_on_the_session_holds_it_off(_permitted, flags):
    state = _state([_slot("dashboard:1", **flags)], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


def test_a_reaching_yolo_grant_holds_it_off(_permitted):
    state = _state([_slot("dashboard:1")], yolo=True, keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


def test_a_policy_deny_holds_it_off(_permitted, monkeypatch):
    monkeypatch.setattr(_permitted, "approve_for_me_permitted", lambda: False)
    state = _state([_slot("dashboard:1")], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False
