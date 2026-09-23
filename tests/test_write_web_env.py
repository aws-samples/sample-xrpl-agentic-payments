from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "write_web_env.py"
spec = importlib.util.spec_from_file_location("write_web_env", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

STACK_OUTPUTS = {
    "Stacks": [
        {
            "Outputs": [
                {"OutputKey": "ApiUrl", "OutputValue": "https://api.example/prod"},
                {"OutputKey": "UserPoolId", "OutputValue": "us-west-2_ABC123"},
                {"OutputKey": "UserPoolClientId", "OutputValue": "client-abc"},
                {
                    "OutputKey": "RuntimeArn",
                    "OutputValue": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/r-1",
                },
            ]
        }
    ]
}


def _mock_cfn_client(outputs: dict) -> MagicMock:
    client = MagicMock()
    client.describe_stacks.return_value = outputs
    return client


def test_writes_stack_outputs_and_fixture_recipient(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS=rRecipient123\n"
        "NEXT_PUBLIC_DEMO_PAYOUT_ALIAS=fixture-bank-token\n"
    )
    output = tmp_path / "web" / ".env.local"

    with patch.object(module.boto3, "client", return_value=_mock_cfn_client(STACK_OUTPUTS)):
        values = module.web_env_values("XrplAgentCorePoc", "us-west-2", env_file)
    module.upsert_env(output, values)

    text = output.read_text()
    assert "NEXT_PUBLIC_API_BASE_URL=https://api.example/prod" in text
    assert "NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-west-2_ABC123" in text
    assert "NEXT_PUBLIC_COGNITO_CLIENT_ID=client-abc" in text
    assert "NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS=rRecipient123" in text
    assert (
        "AGENTCORE_RUNTIME_URL=https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3Aruntime%2Fr-1"
        "/invocations?qualifier=default" in text
    )


def test_defaults_payout_alias_when_absent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS=rRecipient123\n")

    with patch.object(module.boto3, "client", return_value=_mock_cfn_client(STACK_OUTPUTS)):
        values = module.web_env_values("XrplAgentCorePoc", "us-west-2", env_file)

    assert values["NEXT_PUBLIC_DEMO_PAYOUT_ALIAS"] == module.DEFAULT_PAYOUT_ALIAS


def test_fails_closed_without_runtime_arn(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS=rRecipient123\n")
    outputs_without_runtime = {
        "Stacks": [
            {
                "Outputs": [
                    o
                    for o in STACK_OUTPUTS["Stacks"][0]["Outputs"]
                    if o["OutputKey"] != "RuntimeArn"
                ]
            }
        ]
    }

    with patch.object(
        module.boto3, "client", return_value=_mock_cfn_client(outputs_without_runtime)
    ):
        with pytest.raises(RuntimeError, match="RuntimeArn"):
            module.web_env_values("XrplAgentCorePoc", "us-west-2", env_file)


def test_fails_closed_without_fixture_recipient(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=local\n")

    with patch.object(module.boto3, "client", return_value=_mock_cfn_client(STACK_OUTPUTS)):
        with pytest.raises(RuntimeError, match="NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS"):
            module.web_env_values("XrplAgentCorePoc", "us-west-2", env_file)
