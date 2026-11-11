# Dependencies

Every runtime dependency is a recorded decision. Pins are exact, live in `requirements-runtime.txt` (image) and `requirements.txt` (image closure plus the test toolchain the platform installs), and resolve with wheels on Python 3.11 and 3.12 for manylinux_2_28. `pip-audit` is clean at every pin change.

| Package | Version | Why it is needed | Alternatives rejected |
|---|---|---|---|
| fastapi | 0.162.0 | HTTP routes, request validation through pydantic, the `create_app` factory | Starlette alone: loses typed validation at the boundary |
| uvicorn | 0.54.0 | ASGI server the container runs, one worker | gunicorn: a second process manager adds nothing at one worker |
| httpx2 | 2.16.0 | The read-only UDL client, and the client Starlette's TestClient now requires | httpx 0.28: deprecated by Starlette 1.10 for the test client, so one library serves both |
| numpy | 2.4.6 | Binning, medians and statistics over up to about 1.2 million observations | pandas: far larger image and CVE surface for what is array arithmetic |
| sgp4 | 2.27 | Vectorised SGP4 propagation (`SatrecArray`) for the neighbourhood screen | Skyfield: larger, and only SGP4 is needed |

Vendored browser library (served by the app, not installed by pip): Plotly.js 2.35.3 (`plotly.js-dist-min`, MIT licence, copy in `src/lightbee/web/static/vendor/plotly-LICENSE.txt`), SHA-256 `fc84e3cfa55d23d3654c562ea5deb3c274eb6972a6adf66941d1a821e34825e4`, fetched with npm's integrity check (`sha512-sz2HLP8gkysLx/BanM2PtJTtZ1PLPwdHwMWNri2YxLBy3IOeuDsVQtlmWa4hoK3j/fi4naaD3uZJqH5ozM3zGg==`). It draws every chart and exports the PNGs in the browser, so the server carries no imaging stack. Excluded from SonarQube analysis as third-party code. Only SVG traces are used: its WebGL traces need `eval`, which the content security policy refuses.

Test-only (in `requirements.txt`, never in the image): pytest 9.2.0, pytest-cov 7.1.0. Local JavaScript tests use Node's built-in `node --test`; the browser smoke test uses the Playwright already on the build machine. Neither is a project dependency.

Local quality tooling (`requirements-dev.txt`, never installed by the platform): ruff 0.16.9, mypy 2.4.1, pip-audit 2.10.0.

Deliberately absent: matplotlib, Pillow, pandas, pyarrow. PNG export happens in the browser through Plotly, so the server carries no imaging stack. The predecessor's first container scan returned 91 findings, 33 of them from Pillow, urllib3 and certifi.
