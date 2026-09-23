from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest
from xrpl.wallet import Wallet

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_and_set_env.py"
spec = importlib.util.spec_from_file_location("verify_and_set_env", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_fixtures(path: Path) -> dict[str, Wallet]:
    wallets = {name: Wallet.create() for name in set(module.ENV_ADDRESSES.values())}
    path.write_text(
        json.dumps(
            {
                "network": "testnet",
                "wallets": {
                    name: {"address": wallet.address, "seed": wallet.seed}
                    for name, wallet in wallets.items()
                },
            }
        )
    )
    return wallets


def test_writes_addresses_and_never_seeds(tmp_path: Path) -> None:
    fixtures = tmp_path / ".testnet-fixtures.json"
    wallets = write_fixtures(fixtures)
    env_file = tmp_path / ".env"
    env_file.write_text("# local\nALLOW_DEMO_AUTH=true\nXRPL_EXECUTION_ADDRESS=\n")

    module.upsert_env(env_file, module.fixture_env_values(fixtures))

    text = env_file.read_text()
    assert text.startswith("# local\nALLOW_DEMO_AUTH=true\n")
    assert f"XRPL_EXECUTION_ADDRESS={wallets['execution'].address}\n" in text
    assert text.count("XRPL_EXECUTION_ADDRESS=") == 1
    assert f"XRPL_MXN_ISSUER_ADDRESS={wallets['mxn_issuer'].address}" in text
    assert f"NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS={wallets['recipient'].address}" in text
    assert all(wallet.seed not in text for wallet in wallets.values())
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_creates_env_from_template(tmp_path: Path) -> None:
    fixtures = tmp_path / ".testnet-fixtures.json"
    write_fixtures(fixtures)
    template = tmp_path / ".env.example"
    template.write_text("XRPL_NETWORK=testnet\nXRPL_PAYOUT_ADDRESS=\n")
    env_file = tmp_path / ".env"

    module.upsert_env(env_file, module.fixture_env_values(fixtures), template=template)

    lines = env_file.read_text().splitlines()
    assert lines[0] == "XRPL_NETWORK=testnet"
    assert lines[1].startswith("XRPL_PAYOUT_ADDRESS=r")


def test_rejects_invalid_fixture_address(tmp_path: Path) -> None:
    fixtures = tmp_path / ".testnet-fixtures.json"
    write_fixtures(fixtures)
    data = json.loads(fixtures.read_text())
    data["wallets"]["payout"]["address"] = "not-an-address"
    fixtures.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="XRPL_PAYOUT_ADDRESS"):
        module.fixture_env_values(fixtures)
