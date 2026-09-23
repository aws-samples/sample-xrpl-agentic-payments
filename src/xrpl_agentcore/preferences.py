"""Opt-in, structured AgentCore Memory preferences.

No payment details, recipients, wallet addresses, or conversation transcripts are
written by this adapter.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from pydantic import Field

from .domain import PayoutMode, StrictModel


class PreferenceSettings(StrictModel):
    memory_opt_in: bool = False
    default_corridor_id: str | None = Field(default=None, max_length=80)
    default_payout_mode: PayoutMode | None = None
    default_slippage_bps: int | None = Field(default=None, ge=0, le=1_000)

    def memory_payload(self) -> dict[str, Any]:
        if not self.memory_opt_in:
            return {}
        return self.model_dump(
            mode="json",
            exclude={"memory_opt_in"},
            exclude_none=True,
        )


@dataclass(frozen=True, slots=True)
class PreferenceMemory:
    memory_id: str
    region: str

    @classmethod
    def from_environment(cls) -> PreferenceMemory | None:
        memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
        if not memory_id:
            return None
        return cls(
            memory_id=memory_id,
            region=os.environ["AWS_DEFAULT_REGION"],
        )

    def _client(self) -> Any:
        return boto3.client("bedrock-agentcore", region_name=self.region)

    def remember(
        self,
        *,
        actor_id: str,
        session_id: str,
        settings: PreferenceSettings,
    ) -> bool:
        payload = settings.memory_payload()
        if not payload:
            return False
        self._client().create_event(
            memoryId=self.memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(UTC),
            payload=[
                {
                    "conversational": {
                        "content": {
                            "text": "Explicit transfer UI preferences: "
                            + json.dumps(payload, sort_keys=True)
                        },
                        "role": "USER",
                    }
                }
            ],
        )
        return True

    def retrieve(self, *, actor_id: str) -> list[str]:
        response = self._client().retrieve_memory_records(
            memoryId=self.memory_id,
            namespace=f"/preferences/{actor_id}/",
            searchCriteria={
                "searchQuery": "cross-border transfer display and routing preferences",
                "topK": 5,
            },
        )
        values: list[str] = []
        for record in response.get("memoryRecordSummaries", []):
            content = record.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str) and text:
                values.append(text[:500])
        return values
