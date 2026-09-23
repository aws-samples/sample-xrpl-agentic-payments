"""Lambda targets exposed through AgentCore Gateway.

Only quotes, intents, and status are exposed. There is no signing or execution tool.
"""

from __future__ import annotations

from typing import Any

from .api import configured_services
from .domain import AgentTransferProjection
from .services import QuoteRequest


def _owner(event: dict[str, Any]) -> str:
    owner = event.get("owner_sub")
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("trusted owner_sub is required")
    return owner.strip()


def list_supported_corridors(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    _owner(event)
    return {
        "corridors": [
            corridor.model_dump(mode="json") for corridor in configured_services().corridors.list()
        ]
    }


def get_transfer_quote(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    owner = _owner(event)
    request = QuoteRequest.model_validate(event.get("request"))
    quote = configured_services().quotes.create(owner, request)
    return quote.model_dump(mode="json")


def create_transfer_intent(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    owner = _owner(event)
    quote_id = event.get("quote_id")
    if not isinstance(quote_id, str):
        raise ValueError("quote_id is required")
    transfer = configured_services().transfers.create(owner, quote_id)
    return AgentTransferProjection.from_transfer(transfer).model_dump(mode="json")


def get_transfer_status(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    owner = _owner(event)
    transfer_id = event.get("transfer_id")
    if not isinstance(transfer_id, str):
        raise ValueError("transfer_id is required")
    transfer = configured_services().repository.get_transfer(transfer_id, owner)
    return AgentTransferProjection.from_transfer(transfer).model_dump(mode="json")
