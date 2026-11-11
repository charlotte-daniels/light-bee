# Light Bee

Bluestaq Limited. UNCLASSIFIED. Under test and evaluation.

Light Bee tells a space event analyst, within 3 minutes, whether a satellite's brightness change matches its own seasonal history or needs investigating as a manoeuvre or a neighbour interaction.

Two modes:

* **Seasonal Behaviour?** The recent light curve against SEPA (Solar Equatorial Phase Angle), above two years of the same object seen the same way, with an advisory seasonal flag.
* **Other Causes** The light curve, VisMag against time, period, inclination and RAAN, and close approaches, with a drag-to-select neighbourhood scan of the GEO belt.

Light Bee reads the Unified Data Library (UDL) with the analyst's own credentials and never writes to it. Credentials and fetched data live in server memory for the session only.

## Run locally

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/python -m lightbee        # http://127.0.0.1:8080 (PYTHONPATH=src)
```

## Deploy

See `docs/RUNBOOK.md` (written in Phase 4). The brief is `docs/BRIEF.md` and the build plan is `docs/BUILD-PLAN.md`.
