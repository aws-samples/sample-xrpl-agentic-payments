from __future__ import annotations

from fastapi.testclient import TestClient

from xrpl_agentcore.api import create_app


def test_rest_workflow_requires_explicit_authenticated_approval(
    monkeypatch, app_services, wallets
) -> None:
    monkeypatch.setenv("ALLOW_DEMO_AUTH", "true")
    client = TestClient(create_app(app_services))
    auth = {"X-Demo-User": "owner-a"}

    corridors = client.get("/v1/corridors", headers=auth)
    assert corridors.status_code == 200
    quote = client.post(
        "/v1/quotes",
        headers=auth,
        json={
            "corridor_id": "usd-mxn-testnet",
            "destination_amount": "50",
            "payout_mode": "XRPL_WALLET",
            "recipient_address": wallets["recipient"].address,
            "recipient_name": "Demo Recipient",
            "recipient_country": "MX",
            "slippage_bps": 100,
        },
    )
    assert quote.status_code == 201
    intent = client.post(
        "/v1/transfers",
        headers=auth,
        json={"quote_id": quote.json()["quote_id"]},
    )
    assert intent.status_code == 201
    projection = intent.json()
    assert projection["status"] == "AWAITING_APPROVAL"

    transfer_id = projection["transfer_id"]
    assert client.get(f"/v1/transfers/{transfer_id}", headers=auth).status_code == 200
    assert (
        client.get(
            f"/v1/transfers/{transfer_id}",
            headers={"X-Demo-User": "owner-b"},
        ).status_code
        == 404
    )

    approved = client.post(
        f"/v1/transfers/{transfer_id}/approve",
        headers=auth,
        json={
            "approval_hash": projection["approval_hash"],
            "idempotency_key": "browser-confirmation-0001",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    history = client.get("/v1/transfers", headers=auth)
    assert [item["transfer_id"] for item in history.json()] == [transfer_id]


def test_unauthenticated_api_is_rejected(monkeypatch, app_services) -> None:
    monkeypatch.delenv("ALLOW_DEMO_AUTH", raising=False)
    client = TestClient(create_app(app_services))
    assert client.get("/v1/corridors").status_code == 401


def test_blank_env_addresses_fall_back_to_defaults(monkeypatch, tmp_path) -> None:
    # `cp .env.example .env && source .env` exports these as empty strings.
    for name in (
        "XRPL_EXECUTION_ADDRESS",
        "XRPL_PAYOUT_ADDRESS",
        "XRPL_FEE_MERCHANT_ADDRESS",
        "XRPL_USD_ISSUER_ADDRESS",
        "XRPL_MXN_ISSUER_ADDRESS",
        "TRANSFER_TABLE_NAME",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("CORRIDOR_CONFIG_PATH", str(tmp_path / "absent.json"))

    from xrpl_agentcore.api import configured_services

    services = configured_services()

    assert [corridor.id for corridor in services.corridors.list()] == ["usd-mxn-testnet"]
