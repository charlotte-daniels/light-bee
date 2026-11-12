"""A small, believable UDL for driving the whole service in-process."""

from __future__ import annotations

import math
from base64 import b64decode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

from lightbee.config import Settings
from lightbee.udl.client import Credentials, UdlReadClient
from tests.fake_udl import FakeUdl, elset_record, iso, observation

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
TARGET = 43874
NEIGHBOUR = 40001  # 100 km along-track from the target, always
PASSWORD = "not-real-distinctive-pw-7"  # secrets-gate: not-a-secret
USERNAME = "analyst.one"
GEO_RADIUS_KM = 42_164.0


def history(days: int = 740, per_day: int = 12) -> list[dict[str, Any]]:
    """Two years of nightly photometry with a seasonal cycle, sorted by time."""
    records = []
    for day in range(days):
        night = NOW - timedelta(days=day + 1)
        for k in range(per_day):
            when = night + timedelta(minutes=40 * k)
            sepa = -100.0 + 200.0 * k / (per_day - 1)
            season = 0.3 * math.sin(2 * math.pi * when.timetuple().tm_yday / 365.2422)
            mag = 11.0 + 0.03 * abs(sepa) + season
            index = day * per_day + k
            records.append(
                observation(
                    TARGET,
                    when,
                    index,
                    mag=mag,
                    magNormRange=None,
                    georange=37_800.0,
                    solarEqPhaseAngle=-sepa,
                    geolon=100.0,
                    idSensor=f"EX0{k % 3}",
                    classificationMarking="U//PR-MRKT",
                )
            )
    records.sort(key=lambda r: r["obTime"])
    return records


MEAN_MOTION = 1.0027  # revolutions a day, as the fake elsets file


def phased(sat_no: int, epoch: datetime, offset_deg: float, **extra: Any) -> dict[str, Any]:
    """An elset whose mean anomaly is consistent with every other one at any epoch."""
    days = (epoch - NOW).total_seconds() / 86_400.0
    anomaly = (270.0 + offset_deg + 360.0 * MEAN_MOTION * days) % 360.0
    return elset_record(sat_no, epoch, meanAnomaly=anomaly, meanMotion=MEAN_MOTION, **extra)


def elsets() -> list[dict[str, Any]]:
    """KBR elsets for the target every 6 hours over a fortnight, and a GEO snapshot."""
    target = [
        phased(TARGET, NOW - timedelta(hours=h), 0.0, classificationMarking="U//PR-KBR-ELSETS")
        for h in range(0, 24 * 14, 6)
    ]
    snapshot_epoch = NOW - timedelta(days=3, hours=12)
    snapshot = [phased(NEIGHBOUR, snapshot_epoch, math.degrees(100.0 / GEO_RADIUS_KM))]
    snapshot += [phased(41000 + k, snapshot_epoch, 7.0 * (k + 1)) for k in range(40)]
    return target + snapshot


def dense_recent(days: int = 30, per_day: int = 1500) -> list[dict[str, Any]]:
    """A month at real density (about 1,500 observations a night), for load checks."""
    records = []
    for day in range(days):
        night = NOW - timedelta(days=day + 1)
        for k in range(per_day):
            when = night + timedelta(seconds=round(8 * 3600 * k / per_day) + 1)
            sepa = -110.0 + 210.0 * k / (per_day - 1)
            records.append(
                observation(
                    TARGET,
                    when,
                    10_000_000 + day * per_day + k,
                    mag=11.0 + 0.03 * abs(sepa),
                    magNormRange=None,
                    georange=37_800.0,
                    solarEqPhaseAngle=-sepa,
                    geolon=100.0,
                    idSensor=f"EX1{k % 5}",
                    classificationMarking="U//PR-MRKT",
                )
            )
    return records


NEIGHBOUR_NAME = "TEST NEIGHBOUR-1"


def names() -> list[dict[str, Any]]:
    """On-orbit names for the target, the 100 km neighbour and most of the snapshot.

    The last snapshot object has no record, so an unnamed object is always in view.
    """
    records = [
        {"satNo": TARGET, "commonName": "TEST TARGET", "classificationMarking": "U"},
        {"satNo": NEIGHBOUR, "commonName": NEIGHBOUR_NAME, "classificationMarking": "U"},
    ]
    records += [
        {"satNo": 41000 + k, "commonName": f"GEO TEST {k}", "classificationMarking": "U"}
        for k in range(39)
    ]
    return records


def world(*, dense: bool = False) -> FakeUdl:
    observations = history() + (dense_recent() if dense else [])
    observations.sort(key=lambda r: r["obTime"])
    return FakeUdl(history=observations, live=[], elsets=elsets(), onorbit=names())


def factory(fake: FakeUdl) -> Callable[[Credentials], UdlReadClient]:
    """Clients that talk to the fake and refuse any password but the test one."""

    def make(credentials: Credentials) -> UdlReadClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            token = request.headers.get("Authorization", "").removeprefix("Basic ")
            if b64decode(token).decode() != f"{USERNAME}:{PASSWORD}":
                return httpx2.Response(401)
            return fake.handle(request)

        return UdlReadClient(
            Settings(), credentials, transport=httpx2.MockTransport(handle), sleep=lambda _: None
        )

    return make


def as_iso(moment: datetime) -> str:
    return iso(moment)
