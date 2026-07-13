"""Contract tests for security-sensitive human API mutations."""

import pytest
from marshmallow import ValidationError

from app.openapi.schemas.high_risk import (
    CredentialCreateRequestSchema,
    QuickTradeOrderRequestSchema,
)


HIGH_RISK_REQUEST_PATHS = (
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/reset-password",
    "/api/auth/change-password",
    "/api/strategies/start",
    "/api/strategies/stop",
    "/api/strategies/delete",
    "/api/credentials/create",
    "/api/credentials/delete",
    "/api/billing/usdt/create",
    "/api/quick-trade/place-order",
    "/api/quick-trade/close-position",
)


def test_high_risk_mutations_have_typed_requests(app):
    from app.openapi import get_openapi_api

    api = get_openapi_api(app)
    with app.app_context():
        paths = api.spec.to_dict()["paths"]

    for path in HIGH_RISK_REQUEST_PATHS:
        operation = next(
            value
            for method, value in paths[path].items()
            if method in {"post", "put", "patch", "delete"}
        )
        assert "requestBody" in operation or operation.get("parameters"), path


def test_login_validation_uses_human_error_envelope(client):
    response = client.post("/api/auth/login", json={"username": "demo"})

    assert response.status_code == 400
    assert response.get_json() == {
        "code": 0,
        "msg": "Invalid request data",
        "data": {"errors": {"json": {"password": ["Missing data for required field."]}}},
    }


def test_quick_trade_contract_normalizes_legacy_values():
    loaded = QuickTradeOrderRequestSchema().load(
        {
            "credential_id": 7,
            "symbol": "BTC/USDT",
            "side": "BUY",
            "order_type": "LIMIT",
            "amount": "50.5",
            "price": "60000",
            "market_type": "PERP",
            "marginMode": "ISOLATED",
        }
    )

    assert loaded["side"] == "buy"
    assert loaded["order_type"] == "limit"
    assert loaded["amount"] == 50.5
    assert loaded["market_type"] == "perp"
    assert loaded["marginMode"] == "isolated"


def test_credential_contract_requires_secrets_except_ibkr():
    with pytest.raises(ValidationError):
        CredentialCreateRequestSchema().load({"exchange_id": "binance"})

    loaded = CredentialCreateRequestSchema().load(
        {"exchange_id": "IBKR", "ibkr_port": 7497}
    )
    assert loaded["exchange_id"] == "ibkr"
