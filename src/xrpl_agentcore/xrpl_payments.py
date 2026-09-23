"""XRPL transaction construction, persist-before-broadcast, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Protocol

import boto3
from xrpl.clients import JsonRpcClient
from xrpl.models import IssuedCurrencyAmount, Memo, Payment
from xrpl.models.requests import LedgerCurrent, SubmitOnly, Tx
from xrpl.transaction import autofill, sign, simulate
from xrpl.wallet import Wallet

from .domain import Amount, Transfer
from .repository import ExecutionArtifactRepository

MEMO_TYPE = "6170706C69636174696F6E2F6A736F6E"  # application/json
SOURCE_TAG = 20260530


class XrplError(RuntimeError):
    pass


class TransactionRejected(XrplError):
    pass


class SettlementMismatch(XrplError):
    pass


def amount_to_xrpl(amount: Amount) -> str | IssuedCurrencyAmount:
    if amount.asset.currency == "XRP":
        drops = amount.value * 1_000_000
        if drops != drops.to_integral_value():
            raise ValueError("XRP value is not representable as drops")
        return str(int(drops))
    return IssuedCurrencyAmount(
        currency=amount.asset.currency,
        issuer=amount.asset.issuer or "",
        value=amount.canonical_value(),
    )


def amount_from_xrpl(value: Any) -> tuple[str, str | None, str]:
    if isinstance(value, str) and value.isdigit():
        xrp = Decimal(value) / Decimal(1_000_000)
        return "XRP", None, format(xrp, "f")
    if isinstance(value, dict):
        return str(value.get("currency")), str(value.get("issuer")), str(value.get("value"))
    raise SettlementMismatch("delivered amount has an unsupported shape")


def amount_matches_xrpl(value: Any, expected: Amount) -> bool:
    try:
        currency, issuer, amount_value = amount_from_xrpl(value)
        return (
            currency == expected.asset.currency
            and issuer == expected.asset.issuer
            and Decimal(amount_value) == expected.value
        )
    except (InvalidOperation, SettlementMismatch, ValueError):
        return False


def workflow_memo(transfer: Transfer, kind: str) -> Memo:
    payload = json.dumps(
        {
            "contract_version": 1,
            "transfer_id": transfer.transfer_id,
            "approval_hash": transfer.approval_hash,
            "kind": kind,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return Memo(
        memo_type=MEMO_TYPE,
        memo_data=payload.encode().hex().upper(),
    )


def invoice_id_hash(invoice_id: str) -> str:
    return hashlib.sha256(invoice_id.encode()).hexdigest().upper()


def invoice_memo(invoice_id: str) -> Memo:
    return Memo(memo_data=invoice_id.encode().hex().upper())


def has_invoice_memo(raw: Any, invoice_id: str) -> bool:
    if not isinstance(raw, list):
        return False
    expected = invoice_id.encode().hex().upper()
    for wrapper in raw:
        if not isinstance(wrapper, dict):
            continue
        memo = wrapper.get("Memo", wrapper)
        if str(memo.get("MemoData", memo.get("memo_data", ""))).upper() == expected:
            return True
    return False


def decode_workflow_memo(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, list):
        return None
    for wrapper in raw:
        if not isinstance(wrapper, dict):
            continue
        memo = wrapper.get("Memo", wrapper)
        memo_type = memo.get("MemoType", memo.get("memo_type"))
        memo_data = memo.get("MemoData", memo.get("memo_data"))
        if str(memo_type).upper() != MEMO_TYPE.upper() or not memo_data:
            continue
        try:
            value = json.loads(bytes.fromhex(str(memo_data)).decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


class XrplClient(Protocol):
    def autofill(self, payment: Payment) -> Payment: ...

    def simulate(self, payment: Payment) -> dict[str, Any]: ...

    def submit_blob(self, blob: str) -> dict[str, Any]: ...

    def transaction(self, tx_hash: str) -> dict[str, Any] | None: ...

    def ledger_index(self) -> int: ...


class JsonRpcXrplClient:
    def __init__(self, url: str) -> None:
        self.client = JsonRpcClient(url)

    def autofill(self, payment: Payment) -> Payment:
        return autofill(payment, self.client)

    def simulate(self, payment: Payment) -> dict[str, Any]:
        response = simulate(payment, self.client)
        result = dict(response.result)
        engine = str(result.get("engine_result") or result.get("meta", {}).get("TransactionResult"))
        if engine and engine != "tesSUCCESS":
            raise TransactionRejected(f"simulation rejected transaction: {engine}")
        return result

    def submit_blob(self, blob: str) -> dict[str, Any]:
        response = self.client.request(SubmitOnly(tx_blob=blob, fail_hard=True))
        return dict(response.result)

    def transaction(self, tx_hash: str) -> dict[str, Any] | None:
        response = self.client.request(Tx(transaction=tx_hash))
        return dict(response.result) if response.is_successful() else None

    def ledger_index(self) -> int:
        response = self.client.request(LedgerCurrent())
        if not response.is_successful():
            raise XrplError("could not read the current ledger")
        return int(response.result["ledger_current_index"])


@dataclass(frozen=True, slots=True)
class PreparedTransaction:
    transfer_id: str
    kind: str
    tx_hash: str
    signed_blob: str
    sequence: int
    last_ledger_sequence: int


@dataclass(frozen=True, slots=True)
class WalletIdentity:
    """Public wallet identity used by reconciliation-only workers."""

    address: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    state: str
    tx_hash: str
    ledger_index: int | None = None
    delivered_amount: Amount | None = None
    result: str | None = None


class XrplTransactionEngine:
    def __init__(
        self,
        client: XrplClient,
        wallet: Wallet | WalletIdentity,
        artifacts: ExecutionArtifactRepository,
    ) -> None:
        self.client = client
        self.wallet = wallet
        self.artifacts = artifacts

    def _payment(
        self,
        transfer: Transfer,
        *,
        kind: str,
        destination: str,
        destination_tag: int | None,
        destination_amount: Amount,
        send_max: Amount | None,
        paths: list[list[dict[str, Any]]] | None,
        source_tag: int,
        invoice_id: str | None,
    ) -> Payment:
        memos = [workflow_memo(transfer, kind)]
        if invoice_id:
            memos.append(invoice_memo(invoice_id))
        return Payment(
            account=self.wallet.address,
            destination=destination,
            destination_tag=destination_tag,
            amount=amount_to_xrpl(destination_amount),
            send_max=amount_to_xrpl(send_max) if send_max else None,
            paths=paths or None,
            source_tag=source_tag,
            invoice_id=invoice_id_hash(invoice_id) if invoice_id else None,
            memos=memos,
        )

    def prepare(
        self,
        transfer: Transfer,
        *,
        kind: str,
        destination: str,
        destination_tag: int | None,
        destination_amount: Amount,
        send_max: Amount | None = None,
        paths: list[list[dict[str, Any]]] | None = None,
        source_tag: int = SOURCE_TAG,
        invoice_id: str | None = None,
    ) -> PreparedTransaction:
        existing = self.artifacts.get(transfer.transfer_id, kind)
        if existing:
            return PreparedTransaction(**existing)
        if kind == "TRANSFER" and self.wallet.address != transfer.quote.source_account:
            raise XrplError("execution wallet does not match the approved source account")
        payment = self._payment(
            transfer,
            kind=kind,
            destination=destination,
            destination_tag=destination_tag,
            destination_amount=destination_amount,
            send_max=send_max,
            paths=paths,
            source_tag=source_tag,
            invoice_id=invoice_id,
        )
        filled = self.client.autofill(payment)
        self.client.simulate(filled)
        if not isinstance(self.wallet, Wallet):
            raise XrplError("this worker has no signing capability")
        signed = sign(filled, self.wallet)
        if signed.sequence is None or signed.last_ledger_sequence is None:
            raise XrplError("autofilled transaction is missing sequence bounds")
        prepared = PreparedTransaction(
            transfer_id=transfer.transfer_id,
            kind=kind,
            tx_hash=signed.get_hash().upper(),
            signed_blob=signed.blob(),
            sequence=int(signed.sequence),
            last_ledger_sequence=int(signed.last_ledger_sequence),
        )
        stored = self.artifacts.put_prepared(
            prepared.transfer_id,
            prepared.kind,
            prepared.tx_hash,
            prepared.signed_blob,
            prepared.sequence,
            prepared.last_ledger_sequence,
        )
        return PreparedTransaction(**stored)

    def submit(self, prepared: PreparedTransaction) -> str:
        try:
            existing = self.client.transaction(prepared.tx_hash)
        except Exception as error:
            raise XrplError("pre-submit ledger lookup requires reconciliation") from error
        if existing is not None:
            return "RECONCILED"
        try:
            result = self.client.submit_blob(prepared.signed_blob)
        except Exception as error:
            raise XrplError("submission outcome requires reconciliation") from error
        engine = str(result.get("engine_result", "unknown"))
        if engine not in {"tesSUCCESS", "terQUEUED", "tefALREADY"}:
            try:
                current_ledger = self.client.ledger_index()
            except Exception as error:
                raise XrplError("submission outcome requires reconciliation") from error
            if current_ledger > prepared.last_ledger_sequence:
                raise TransactionRejected(f"signed transaction expired: {engine}")
            raise XrplError(f"submission outcome requires reconciliation: {engine}")
        return engine

    def reconcile(
        self,
        transfer: Transfer,
        prepared: PreparedTransaction,
        *,
        expected_destination: str,
        expected_amount: Amount,
        expected_kind: str,
        expected_destination_tag: int | None = None,
        expected_send_max: Amount | None = None,
        expected_paths: list[list[dict[str, Any]]] | None = None,
        expected_source_tag: int = SOURCE_TAG,
        expected_invoice_id: str | None = None,
    ) -> Reconciliation:
        try:
            result = self.client.transaction(prepared.tx_hash)
        except Exception:
            return Reconciliation("PENDING", prepared.tx_hash)
        if result is None:
            try:
                current_ledger = self.client.ledger_index()
            except Exception:
                return Reconciliation("PENDING", prepared.tx_hash)
            if current_ledger > prepared.last_ledger_sequence:
                return Reconciliation("EXPIRED", prepared.tx_hash)
            return Reconciliation("PENDING", prepared.tx_hash)
        if result.get("validated") is not True:
            return Reconciliation("PENDING", prepared.tx_hash)

        tx_json = result.get("tx_json", result)
        meta = result.get("meta") or {}
        tx_hash = str(result.get("hash") or tx_json.get("hash") or "").upper()
        transaction_result = str(meta.get("TransactionResult", ""))
        delivered = meta.get("delivered_amount", meta.get("DeliveredAmount"))
        memo = decode_workflow_memo(tx_json.get("Memos"))
        if tx_hash != prepared.tx_hash:
            raise SettlementMismatch("ledger hash does not match prepared hash")
        if transaction_result != "tesSUCCESS":
            raise SettlementMismatch(f"validated transaction failed: {transaction_result}")
        if tx_json.get("TransactionType") != "Payment":
            raise SettlementMismatch("validated transaction is not a Payment")
        if int(tx_json.get("Flags") or 0) & 0x00020000:
            raise SettlementMismatch("partial-payment transactions are not accepted")
        if tx_json.get("Account") != self.wallet.address:
            raise SettlementMismatch("validated source account mismatch")
        if tx_json.get("Destination") != expected_destination:
            raise SettlementMismatch("validated destination account mismatch")
        if tx_json.get("DestinationTag") != expected_destination_tag:
            raise SettlementMismatch("validated destination tag mismatch")
        if tx_json.get("SourceTag") != expected_source_tag:
            raise SettlementMismatch("validated source tag mismatch")
        if expected_invoice_id is not None:
            if tx_json.get("InvoiceID") != invoice_id_hash(expected_invoice_id):
                raise SettlementMismatch("validated x402 InvoiceID mismatch")
            if not has_invoice_memo(tx_json.get("Memos"), expected_invoice_id):
                raise SettlementMismatch("validated x402 invoice memo mismatch")
        elif tx_json.get("InvoiceID") is not None:
            raise SettlementMismatch("unexpected validated InvoiceID")
        # rippled API v2 returns a Payment's serialized Amount field as
        # DeliverMax; API v1 and locally serialized transactions use Amount.
        payment_amount = tx_json.get("DeliverMax", tx_json.get("Amount"))
        if not amount_matches_xrpl(payment_amount, expected_amount):
            raise SettlementMismatch("validated payment Amount mismatch")
        if expected_send_max is not None and not amount_matches_xrpl(
            tx_json.get("SendMax"),
            expected_send_max,
        ):
            raise SettlementMismatch("validated SendMax mismatch")
        if expected_paths is not None and (tx_json.get("Paths") or []) != expected_paths:
            raise SettlementMismatch("validated path mismatch")
        if memo != {
            "approval_hash": transfer.approval_hash,
            "contract_version": 1,
            "kind": expected_kind,
            "transfer_id": transfer.transfer_id,
        }:
            raise SettlementMismatch("workflow memo mismatch")

        currency, issuer, value = amount_from_xrpl(delivered)
        if currency != expected_amount.asset.currency:
            raise SettlementMismatch("delivered currency mismatch")
        if issuer != expected_amount.asset.issuer:
            raise SettlementMismatch("delivered issuer mismatch")
        if value != expected_amount.canonical_value():
            # Compare decimals to accept equivalent ledger formatting.
            if Decimal(value) != expected_amount.value:
                raise SettlementMismatch("delivered amount mismatch")
        ledger_index = int(result.get("ledger_index") or 0)
        if ledger_index <= 0:
            raise SettlementMismatch("validated transaction has no ledger index")
        return Reconciliation(
            "SETTLED",
            prepared.tx_hash,
            ledger_index=ledger_index,
            delivered_amount=expected_amount,
            result=transaction_result,
        )


@lru_cache(maxsize=8)
def wallet_from_secret(secret_id: str, region: str) -> Wallet:
    response = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_id
    )
    raw = response.get("SecretString", "")
    try:
        value = json.loads(raw)
        seed = value["seed"]
    except (json.JSONDecodeError, KeyError, TypeError):
        seed = raw
    if not isinstance(seed, str) or not seed.strip():
        raise XrplError("wallet seed secret is empty")
    return Wallet.from_seed(seed.strip())


def configured_client() -> JsonRpcXrplClient:
    if os.environ.get("XRPL_NETWORK", "testnet") != "testnet":
        raise XrplError("this POC refuses non-testnet XRPL configuration")
    return JsonRpcXrplClient(
        os.environ.get("XRPL_RPC_URL", "https://s.altnet.rippletest.net:51234")
    )
