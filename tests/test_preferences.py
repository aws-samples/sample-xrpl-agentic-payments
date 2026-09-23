from __future__ import annotations

from datetime import datetime

from xrpl_agentcore.preferences import PreferenceMemory, PreferenceSettings


class FakeMemoryClient:
    def __init__(self) -> None:
        self.events = []

    def create_event(self, **kwargs):
        self.events.append(kwargs)

    def retrieve_memory_records(self, **_kwargs):
        return {
            "memoryRecordSummaries": [
                {"content": {"text": "Prefers direct wallet payout"}},
                {"content": {"text": "x" * 700}},
            ]
        }


def test_preference_memory_is_opt_in_filtered_and_uses_sdk_timestamp(
    monkeypatch,
) -> None:
    fake = FakeMemoryClient()
    monkeypatch.setattr(PreferenceMemory, "_client", lambda _self: fake)
    memory = PreferenceMemory("memory-id", region="us-west-2")

    assert (
        memory.remember(
            actor_id="owner",
            session_id="session-123",
            settings=PreferenceSettings(memory_opt_in=False),
        )
        is False
    )
    assert fake.events == []

    assert (
        memory.remember(
            actor_id="owner",
            session_id="session-123",
            settings=PreferenceSettings(
                memory_opt_in=True,
                default_corridor_id="usd-mxn-testnet",
                default_payout_mode="XRPL_WALLET",
                default_slippage_bps=100,
            ),
        )
        is True
    )
    event = fake.events[0]
    assert isinstance(event["eventTimestamp"], datetime)
    text = event["payload"][0]["conversational"]["content"]["text"]
    assert "usd-mxn-testnet" in text
    assert "memory_opt_in" not in text
    assert "recipient" not in text

    recalled = memory.retrieve(actor_id="owner")
    assert recalled[0] == "Prefers direct wallet payout"
    assert len(recalled[1]) == 500
