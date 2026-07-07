"""Tests for VNCManager — allocation logic and get_ws_port."""

from __future__ import annotations

import asyncio

import pytest

import backend.vnc_manager as vnc_manager_module
from backend.vnc_manager import VNCInstance, VNCManager


@pytest.fixture()
def vnc() -> VNCManager:
    return VNCManager()


# ── allocate ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allocate_first(vnc: VNCManager):
    display, ws_port = await vnc.allocate()
    assert display == 100
    assert ws_port == 6100


@pytest.mark.asyncio
async def test_allocate_sequential(vnc: VNCManager):
    d1, p1 = await vnc.allocate()
    d2, p2 = await vnc.allocate()
    d3, p3 = await vnc.allocate()
    assert (d1, d2, d3) == (100, 101, 102)
    assert (p1, p2, p3) == (6100, 6101, 6102)


@pytest.mark.asyncio
async def test_allocate_fills_gap(vnc: VNCManager):
    """After freeing display 100, next allocate should reuse it."""
    await vnc.allocate()  # 100
    await vnc.allocate()  # 101
    # Simulate freeing display 100 (like stop_vnc would)
    vnc._allocated.pop(100)
    d, p = await vnc.allocate()
    assert d == 100  # gap filled
    assert p == 6100


@pytest.mark.asyncio
async def test_allocate_tracks_instances(vnc: VNCManager):
    await vnc.allocate()
    await vnc.allocate()
    assert len(vnc._allocated) == 2
    assert 100 in vnc._allocated
    assert 101 in vnc._allocated


@pytest.mark.asyncio
async def test_allocate_instance_fields(vnc: VNCManager):
    await vnc.allocate()
    instance = vnc._allocated[100]
    assert isinstance(instance, VNCInstance)
    assert instance.display == 100
    assert instance.ws_port == 6100
    assert instance.process is None  # not started yet


# ── get_ws_port ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ws_port_allocated(vnc: VNCManager):
    await vnc.allocate()
    assert vnc.get_ws_port(100) == 6100


def test_get_ws_port_not_allocated(vnc: VNCManager):
    assert vnc.get_ws_port(999) is None


# ── active_displays ──────────────────────────────────────────────────────────


def test_active_displays_empty(vnc: VNCManager):
    assert vnc.active_displays == []


@pytest.mark.asyncio
async def test_active_displays_after_allocate(vnc: VNCManager):
    await vnc.allocate()
    await vnc.allocate()
    assert sorted(vnc.active_displays) == [100, 101]


# ── start_vnc ────────────────────────────────────────────────────────────────


def test_start_vnc_disables_kasmvnc_clipboard(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, list[str]] = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None):
        captured["cmd"] = cmd
        return FakeProcess()

    async def fake_sleep(_seconds: float):
        return None

    monkeypatch.setattr(vnc_manager_module.shutil, "which", lambda _name: "/usr/bin/Xvnc")
    monkeypatch.setattr(vnc_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(vnc_manager_module.asyncio, "sleep", fake_sleep)

    manager = VNCManager()
    asyncio.run(manager.start_vnc(display=100, ws_port=6100))

    assert "-SendCutText=0" in captured["cmd"]
    assert "-AcceptCutText=0" in captured["cmd"]


# ── BrowserManager.get_status ────────────────────────────────────────────────


def test_get_status_stopped():
    from backend.browser_manager import BrowserManager
    mgr = BrowserManager()
    status = mgr.get_status("nonexistent")
    assert status == {"status": "stopped", "vnc_ws_port": None, "display": None, "cdp_url": None}


def test_get_status_running():
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock
    mgr = BrowserManager()
    mgr.running["abc"] = RunningProfile(
        profile_id="abc",
        context=MagicMock(),
        display=100,
        ws_port=6100,
        cdp_port=5100,
    )
    status = mgr.get_status("abc")
    assert status == {
        "status": "running",
        "vnc_ws_port": 6100,
        "display": ":100",
        "cdp_url": "/api/profiles/abc/cdp",
    }
