"""An in-memory stand-in for the UDL read endpoints, shaped to the supplied OpenAPI specs.

It honours the parts of the query contract Light Bee relies on (bounded
`start..end` time filters, `satNo`, `source`, `dataMode`, `columns`,
`maxResults`) and records every request, so tests can assert on exactly what
went over the wire.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

UNCLASSIFIED = "U"


def iso(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    )


def observation(sat_no: int, when: datetime, index: int, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": f"obs-{sat_no}-{index}",
        "classificationMarking": UNCLASSIFIED,
        "obTime": iso(when),
        "satNo": sat_no,
        "mag": 11.5,
        "magNormRange": 11.2,
        "solarEqPhaseAngle": (index % 360) - 180.0 + 0.5,
        "geolon": 101.4,
        "idSensor": f"EX0{index % 4}",
        "dataMode": "REAL",
        "source": "PROVIDER",
        "range": 37000.0,
    }
    record.update(overrides)
    return record


def elset_record(
    sat_no: int, when: datetime, source: str = "KBR", **overrides: Any
) -> dict[str, Any]:
    """An elset as the tuple endpoint returns it: mean elements, no TLE lines."""
    record: dict[str, Any] = {
        "classificationMarking": UNCLASSIFIED,
        "satNo": sat_no,
        "idOnOrbit": str(sat_no),
        "epoch": iso(when),
        "source": source,
        "dataMode": "REAL",
        "ephemType": 0,
        "meanMotion": 1.0027,
        "eccentricity": 0.0001,
        "inclination": 0.05,
        "raan": 80.0,
        "argOfPerigee": 90.0,
        "meanAnomaly": 270.0,
        "bStar": 0.0,
        "meanMotionDot": 0.0,
        "meanMotionDDot": 0.0,
        "period": 1436.1,
    }
    record.update(overrides)
    return record


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _window(value: str) -> tuple[datetime, datetime]:
    start, _, end = value.partition("..")
    return _parse(start), _parse(end)


@dataclass
class FakeUdl:
    """Serves observations and elsets; records every request it receives."""

    history: list[dict[str, Any]] = field(default_factory=list)
    live: list[dict[str, Any]] = field(default_factory=list)
    elsets: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx2.Request] = field(default_factory=list)
    fail_once: dict[str, int] = field(default_factory=dict)
    # A path mapped to a query parameter the fake refuses there with HTTP 400,
    # explaining itself in the body as the UDL does.
    refuse_param: dict[str, str] = field(default_factory=dict)
    # A path mapped to a column the fake will not let be selected there.
    refuse_column: dict[str, str] = field(default_factory=dict)
    # Fields the fake filters on but leaves out of its replies, as the UDL does
    # with satNo on KBR elsets.
    withhold: set[str] = field(default_factory=set)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status = self.fail_once.pop(request.url.path, None)
        if status is not None:
            return httpx2.Response(status)
        params = dict(request.url.params)
        refused = self.refuse_param.get(request.url.path)
        if refused in params:
            return httpx2.Response(400, content=f"Invalid query parameter: {refused}".encode())
        column = self.refuse_column.get(request.url.path)
        if column is not None and column in params.get("columns", "").split(","):
            message = f"Column {column} is not available to select"
            return httpx2.Response(400, content=message.encode())
        source = {
            "/udl/eoobservation/history": (self.history, "obTime"),
            "/udl/eoobservation/tuple": (self.live, "obTime"),
            "/udl/elset/tuple": (self.elsets, "epoch"),
        }.get(request.url.path)
        if source is None:
            return httpx2.Response(404)
        records, time_field = source
        return httpx2.Response(200, content=json.dumps(self._select(records, time_field, params)))

    @staticmethod
    def _ordered(records: list[dict[str, Any]], time_field: str) -> tuple[list[str], list[int]]:
        """Record times in order with their positions, rebuilt per request so edits are seen."""
        pairs = sorted((str(r[time_field]), n) for n, r in enumerate(records))
        return [p[0] for p in pairs], [p[1] for p in pairs]

    def _select(
        self, records: list[dict[str, Any]], time_field: str, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        start, end = _window(params[time_field])
        cap = int(params.get("maxResults", "10000"))
        wanted = params.get("columns", "").split(",") if params.get("columns") else None
        times, positions = self._ordered(records, time_field)
        # The fake's times are all in one ISO form, so they sort as strings.
        low = bisect.bisect_left(times, iso(start))
        high = bisect.bisect_left(times, iso(end))
        chosen = []
        for position in positions[low:high]:
            record = records[position]
            if "satNo" in params and str(record.get("satNo")) != params["satNo"]:
                continue
            if "source" in params and record.get("source") != params["source"]:
                continue
            if "dataMode" in params and record.get("dataMode") != params["dataMode"]:
                continue
            # Only the requested columns come back, as the tuple and history
            # endpoints document; nothing is added on Light Bee's behalf.
            reply = record if wanted is None else {k: record[k] for k in wanted if k in record}
            chosen.append({k: v for k, v in reply.items() if k not in self.withhold})
        return chosen[:cap]


def dense_history(sat_no: int, start: datetime, days: int, per_day: int) -> list[dict[str, Any]]:
    step = timedelta(days=1) / per_day
    return [observation(sat_no, start + step * index, index) for index in range(days * per_day)]
