"""Run Light Bee locally against the fake UDL world, for browser tests and demos.

Never shipped. The clock is fixed at the fake world's "now" (10 November 2026,
18:00 UTC), so use change dates on or before 2026-11-10. Sign in with the
fake world's test username and password.

    .venv/bin/python tools/dev_server.py --port 8770
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lightbee.analysis.series import DAY_MS
from lightbee.config import Settings
from lightbee.logs import LOG_CONFIG
from lightbee.udl.photometry import Photometry
from lightbee.udl.records import Tally
from lightbee.web import runs
from lightbee.web.app import create_app
from tests.fake_world import NOW, TARGET, factory, world


def synthesise(per_night: int) -> None:
    """Serve the target's photometry from arrays at real density, for load checks.

    Reading a million records through the fake UDL's dictionaries would measure
    the fake, not Light Bee, so the photometry read is replaced by arrays of the
    same shape. Every other read still goes through the fake UDL.
    """
    real = getattr(runs, "read_observations")  # noqa: B009 - patched below, not exported

    def dense(client: Any, sat_no: int, window: tuple[datetime, datetime], **kw: Any) -> Any:
        if sat_no != TARGET:
            return real(client, sat_no, window, **kw)
        start, end = (round(moment.timestamp() * 1000) for moment in window)
        nights = np.arange(start // DAY_MS, end // DAY_MS + 1, dtype=np.int64)
        k = np.arange(per_night, dtype=np.int64)
        epoch = (
            nights[:, None] * DAY_MS + 1000 + k[None, :] * (8 * 3_600_000 // per_night)
        ).ravel()
        epoch = epoch[(epoch >= start) & (epoch < end)]
        phase = (epoch % DAY_MS) / (8 * 3_600_000)
        season = np.sin(2 * np.pi * (epoch / (365.25 * DAY_MS)))
        sepa = (-110.0 + 210.0 * phase + 15.0 * season).astype(np.float32)
        mag = (11.0 + 0.03 * np.abs(sepa) + 0.2 * season).astype(np.float32)
        tally = Tally(received=epoch.size, kept=epoch.size, controls={"PR-MRKT"})
        photometry = Photometry(
            epoch_ms=epoch,
            mag=mag,
            mag_norm=mag + 7.0,
            sepa=sepa,
            geolon=np.full(epoch.size, 100.0, dtype=np.float32),
            sensor=(np.arange(epoch.size) % 5).astype(np.int16),
            sensors=tuple(f"EX1{n}" for n in range(5)),
            tally=tally,
        )
        if kw.get("on_progress"):
            kw["on_progress"](epoch.size, 1, 0)
        return photometry

    setattr(runs, "read_observations", dense)  # noqa: B010 - a load-check stand-in


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--dense", action="store_true", help="a month at real density")
    parser.add_argument(
        "--dense-history",
        action="store_true",
        help="the target's photometry synthesised at --per-night across whatever a run reads",
    )
    parser.add_argument("--per-night", type=int, default=1500)
    args = parser.parse_args(argv)
    if args.dense_history:
        synthesise(args.per_night)
    app = create_app(Settings(), client_factory=factory(world(dense=args.dense)), clock=lambda: NOW)
    # The container's logging: every line one JSON object on stdout.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=LOG_CONFIG)


if __name__ == "__main__":
    main()
