"""Modal deployment of the headless-browser tier: ``modal deploy modal_browser.py``.

The API (``modal_app.py``) calls ``Browser.render`` only when plain HTTP meets a
JavaScript challenge or an empty JavaScript shell.
"""

import modal

app = modal.App("webfetch-browser")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("patchright==1.64.4")
    .run_commands("patchright install --with-deps chromium")
    .add_local_python_source("webfetch")
)


@app.cls(image=image, cpu=2.0, memory=4096, timeout=180, scaledown_window=600, max_containers=2)
@modal.concurrent(max_inputs=4)
class Browser:
    @modal.enter()
    async def start(self):
        from webfetch.browser import BrowserPool

        self.pool = BrowserPool()
        await self.pool.start()

    @modal.method()
    async def render(self, url: str, cookies: list, headers: dict | None, timeout: float) -> dict:
        return await self.pool.render(url, cookies=cookies, headers=headers, timeout=timeout)

    @modal.exit()
    async def stop(self):
        await self.pool.stop()
