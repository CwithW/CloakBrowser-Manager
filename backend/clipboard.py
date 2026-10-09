"""Bounded X11 clipboard access with foreground selection-owner processes."""

from __future__ import annotations

import asyncio
import os
from typing import Any

MAX_TEXT = 1_048_576
TIMEOUT = 3.0


class ClipboardManager:
    def __init__(self):
        self._owners: dict[str, asyncio.subprocess.Process] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def cleanup(self, profile_id: str) -> None:
        process = self._owners.pop(profile_id, None)
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), TIMEOUT)

    async def set(self, running: Any, text: str) -> None:
        lock = self._locks.setdefault(running.profile_id, asyncio.Lock())
        async with lock:
            await self.cleanup(running.profile_id)
            # -quiet keeps xclip in the foreground. Its default -silent forks,
            # leaving an untracked child alive after the parent exits.
            process = await asyncio.create_subprocess_exec(
                "xclip", "-selection", "clipboard", "-in", "-quiet",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "DISPLAY": f":{running.display}"},
            )
            self._owners[running.profile_id] = process
            try:
                process.stdin.write(text.encode())
                await asyncio.wait_for(process.stdin.drain(), TIMEOUT)
                process.stdin.close()
                await asyncio.sleep(0.05)
                if process.returncode is not None and process.returncode != 0:
                    raise RuntimeError("The VNC clipboard owner could not start")
            except BaseException:
                await self.cleanup(running.profile_id)
                raise

    async def read(self, running: Any) -> str:
        process = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard", "-out",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "DISPLAY": f":{running.display}"},
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), TIMEOUT)
            if process.returncode == 0:
                return output[:MAX_TEXT].decode(errors="replace")
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()

        # A frozen tab must not leave API/UI requests permanently busy.
        async def read_pages() -> str:
            for page in reversed(running.context.pages):
                try:
                    text = await asyncio.wait_for(page.evaluate("window.__clipboardText || window.getSelection()?.toString() || ''"), 0.75)
                    if isinstance(text, str) and text:
                        return text[:MAX_TEXT]
                except Exception:
                    continue
            return ""
        try:
            return await asyncio.wait_for(read_pages(), TIMEOUT)
        except asyncio.TimeoutError:
            return ""
