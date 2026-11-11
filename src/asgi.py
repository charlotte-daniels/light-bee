"""The ASGI application the App Store runs.

The container starts this with exactly one uvicorn worker. Analyst sessions,
including their UDL credentials, live in this process's memory by design, so a
second worker would sign analysts out at random as the balancer alternated.
"""

from __future__ import annotations

from lightbee.config import load_settings
from lightbee.web.app import create_app

app = create_app(load_settings())
