"""Dispatch approved-transfer outbox records to Step Functions."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

_deserializer = TypeDeserializer()


def _decode(image: dict[str, Any]) -> dict[str, Any]:
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, int]:
    state_machine_arn = os.environ["TRANSFER_STATE_MACHINE_ARN"]
    client = boto3.client(
        "stepfunctions",
        region_name=os.environ["AWS_DEFAULT_REGION"],
    )
    started = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        image = _decode(record.get("dynamodb", {}).get("NewImage", {}))
        if image.get("event_type") != "EXECUTE_APPROVED_TRANSFER":
            continue
        transfer_id = str(image["transfer_id"])
        try:
            client.start_execution(
                stateMachineArn=state_machine_arn,
                name=transfer_id,
                input=json.dumps(
                    {
                        "transfer_id": transfer_id,
                        "approval_hash": image["approval_hash"],
                    }
                ),
            )
            started += 1
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                raise
    return {"started": started}
