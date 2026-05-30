"""Shared fixtures."""

from __future__ import annotations

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink


@pytest.fixture
def schemas():
    return {
        "send_email": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string", "format": "email"},
                "subject": {"type": "string", "maxLength": 200},
                "body": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["to", "subject", "body"],
        },
        "create_event": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "format": "date-time"},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 480},
            },
            "required": ["title", "start"],
        },
    }


@pytest.fixture
def executors():
    sent: list[dict] = []

    def send_email(**kwargs):
        sent.append({"tool": "send_email", **kwargs})
        return {"ok": True, "id": f"msg_{len(sent)}"}

    def create_event(**kwargs):
        sent.append({"tool": "create_event", **kwargs})
        return {"ok": True, "id": f"evt_{len(sent)}"}

    create_event._sent = sent  # noqa — easy assertion access
    send_email._sent = sent
    return {"send_email": send_email, "create_event": create_event}


@pytest.fixture
def sink():
    return NullSink()


@pytest.fixture
def cruxial(schemas, executors, sink):
    return guard(
        schemas=schemas,
        executors=executors,
        config=GuardConfig(fail_open=True, sinks=("null",)),
        sink=sink,
    )
