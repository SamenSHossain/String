"""Calls the headless-browser tier deployed as the ``webfetch-browser`` Modal app."""

from __future__ import annotations

from typing import Any

BROWSER_APP = "webfetch-browser"
BROWSER_CLS = "Browser"


class ModalBrowserClient:
    def __init__(self, app_name: str = BROWSER_APP, cls_name: str = BROWSER_CLS):
        self.app_name = app_name
        self.cls_name = cls_name
        self._obj = None

    async def render(self, url: str, *, cookies: list[dict], headers: dict[str, str] | None,
                     timeout: float) -> dict[str, Any]:
        import modal

        if self._obj is None:
            self._obj = modal.Cls.from_name(self.app_name, self.cls_name)()
        return await self._obj.render.remote.aio(url, cookies, headers, timeout)
