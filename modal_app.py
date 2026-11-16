"""Modal deployment of the HTTP API: ``modal deploy modal_app.py``.

Serves https://<workspace>--webfetch.modal.run . The optional headless-browser tier is a
separate app (``modal deploy modal_browser.py``) so that the API never depends on the
heavier Chromium image building successfully.
"""

import modal

API_REQUIREMENTS = [
    "fastapi==0.163.1",
    "pydantic==2.14.3",
    "curl_cffi==0.17.0",
    "selectolax==1.0.2",
    "html-to-markdown==3.22.2",
    "trafilatura==2.3.1",
    "charset-normalizer==3.5.2",
]

app = modal.App("webfetch")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(*API_REQUIREMENTS)
    .env({"WEBFETCH_BROWSER": "modal"})
    .add_local_python_source("webfetch")
)


@app.function(
    image=image,
    cpu=1.0,
    memory=2048,
    timeout=300,
    scaledown_window=900,
    max_containers=3,
)
@modal.concurrent(max_inputs=40)
@modal.asgi_app(label="webfetch")
def api():
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from webfetch.api import create_app
    from webfetch.modal_client import ModalBrowserClient

    return create_app(browser=ModalBrowserClient())
