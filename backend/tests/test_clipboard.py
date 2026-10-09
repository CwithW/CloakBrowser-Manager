"""Regression coverage for stalled tabs and persistent X11 clipboard owners."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import clipboard


@pytest.mark.asyncio
async def test_replace_foreground_owner_and_cleanup(monkeypatch):
    owner = MagicMock(returncode=None)
    owner.stdin.drain = AsyncMock()
    owner.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=owner)
    monkeypatch.setattr(clipboard.asyncio, "create_subprocess_exec", spawn)
    manager = clipboard.ClipboardManager()
    running = SimpleNamespace(profile_id="one", display=100)
    await manager.set(running, "first")
    await manager.set(running, "second")
    assert "-quiet" in spawn.call_args.args
    owner.kill.assert_called_once()
    assert owner.stdin.write.call_args.args == (b"second",)
    await manager.cleanup("one")
    assert "one" not in manager._owners


@pytest.mark.asyncio
async def test_frozen_tab_does_not_block_reads_or_next_request(monkeypatch):
    process = MagicMock(returncode=1)
    process.communicate = AsyncMock(return_value=(b"", b""))
    monkeypatch.setattr(clipboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    async def frozen(*_):
        await asyncio.Event().wait()
    page = SimpleNamespace(evaluate=frozen)
    manager = clipboard.ClipboardManager()
    running = SimpleNamespace(profile_id="one", display=100, context=SimpleNamespace(pages=[page]))
    assert await asyncio.wait_for(manager.read(running), 1.5) == ""
    page.evaluate = AsyncMock(return_value="recovered")
    assert await manager.read(running) == "recovered"


@pytest.mark.asyncio
async def test_native_x11_value_wins_over_stale_browser_cache(monkeypatch):
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=("当前剪贴板".encode(), b""))
    monkeypatch.setattr(clipboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    page = SimpleNamespace(evaluate=AsyncMock(return_value="old cached text"))
    running = SimpleNamespace(profile_id="one", display=100, context=SimpleNamespace(pages=[page]))
    assert await clipboard.ClipboardManager().read(running) == "当前剪贴板"
    page.evaluate.assert_not_awaited()
