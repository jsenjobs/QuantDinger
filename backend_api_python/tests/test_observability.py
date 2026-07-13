"""HTTP metrics, request correlation, and structured logging tests."""

import json
import logging

from app.observability.context import request_id_context
from app.utils.logger import JsonFormatter


def test_request_id_is_preserved(client):
    response = client.get("/api/health", headers={"X-Request-ID": "test-request-42"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request-42"


def test_invalid_request_id_is_replaced(client):
    response = client.get("/api/health", headers={"X-Request-ID": "invalid request id"})

    request_id = response.headers["X-Request-ID"]
    assert request_id != "invalid request id"
    assert len(request_id) == 32


def test_prometheus_metrics_endpoint(client):
    client.get("/api/health")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    body = response.get_data(as_text=True)
    assert "quantdinger_build_info" in body
    assert "quantdinger_http_requests_total" in body
    assert "quantdinger_workers_healthy" in body


def test_json_formatter_includes_runtime_context(monkeypatch):
    monkeypatch.setenv("QD_PROCESS_ROLE", "api")
    token = request_id_context.set("request-123")
    try:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_context.reset(token)

    assert payload["message"] == "hello"
    assert payload["request_id"] == "request-123"
    assert payload["process_role"] == "api"
