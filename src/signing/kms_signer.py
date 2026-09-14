# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — KMS Signing Module

Provides XRPL transaction signing with two modes:
1. LOCAL (testnet): Uses xrpl-py wallet seed for signing (fast, free)
2. KMS (production): Uses AWS KMS secp256k1 key (HSM-backed, auditable)

The module exposes the same interface regardless of mode, so the MCP server
and agents don't need to know which backend is active.

Architecture:
  Transaction → serialize → hash → sign (KMS or local) → attach signature → submit

For KMS mode (production):
  - Key spec: ECC_SECG_P256K1 (same curve as XRPL/Bitcoin/Ethereum)
  - Sign operation: ECDSA with SHA-256 hash
  - Key never leaves the HSM — only the signature is returned
  - Every sign operation logged in CloudTrail for audit
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from xrpl.core.binarycodec import encode, encode_for_signing
from xrpl.core.keypairs import sign as xrpl_sign
from xrpl.models import Transaction
from xrpl.wallet import Wallet

logger = logging.getLogger("xrpl_agentic_payments.signing")


class SigningMode(Enum):
    LOCAL = "local"  # Testnet: sign with wallet seed
    KMS = "kms"      # Production: sign with AWS KMS


@dataclass
class SigningConfig:
    """Configuration for the signing module."""
    mode: SigningMode = SigningMode.LOCAL
    kms_key_id: Optional[str] = None  # AWS KMS Key ID (for KMS mode)
    aws_region: str = "us-west-2"
    wallet_name: str = "execution"  # Which wallet to use for local signing


class XRPLSigner:
    """
    Signs XRPL transactions using either local wallet keys or AWS KMS.

    Usage:
        signer = XRPLSigner(config)
        signed_tx_blob = signer.sign_transaction(transaction)
    """

    def __init__(self, config: SigningConfig):
        self.config = config
        self._wallet: Optional[Wallet] = None
        self._kms_client = None

        if config.mode == SigningMode.LOCAL:
            self._load_local_wallet()
        elif config.mode == SigningMode.KMS:
            self._init_kms_client()

    def _load_local_wallet(self) -> None:
        """Load wallet from config/wallets.json for local signing."""
        wallets_file = Path(__file__).resolve().parent.parent.parent / "config" / "wallets.json"
        if not wallets_file.exists():
            # Try CWD
            wallets_file = Path.cwd() / "config" / "wallets.json"

        if not wallets_file.exists():
            raise FileNotFoundError(
                f"Wallet file not found. Run scripts/provision_wallets.py first."
            )

        with open(wallets_file) as f:
            wallets_data = json.load(f)

        wallet_data = wallets_data.get(self.config.wallet_name)
        if not wallet_data:
            raise ValueError(
                f"Wallet '{self.config.wallet_name}' not found in config. "
                f"Available: {[k for k in wallets_data.keys() if not k.startswith('_')]}"
            )

        self._wallet = Wallet.from_seed(wallet_data["seed"])
        logger.info(f"Loaded local wallet: {self._wallet.address} ({self.config.wallet_name})")

    def _init_kms_client(self) -> None:
        """Initialize AWS KMS client for production signing."""
        try:
            import boto3
            self._kms_client = boto3.client("kms", region_name=self.config.aws_region)
            logger.info(f"KMS client initialized (key: {self.config.kms_key_id})")
        except Exception as e:
            raise RuntimeError(f"Failed to initialize KMS client: {e}")

    @property
    def address(self) -> str:
        """Get the XRPL address of the signing key."""
        if self.config.mode == SigningMode.LOCAL and self._wallet:
            return self._wallet.address
        elif self.config.mode == SigningMode.KMS:
            # In KMS mode, the address is derived from the public key
            # which is fetched from KMS during initialization
            return self._get_kms_address()
        return ""

    def _get_kms_address(self) -> str:
        """Derive XRPL address from KMS public key."""
        if not self._kms_client or not self.config.kms_key_id:
            return ""

        response = self._kms_client.get_public_key(KeyId=self.config.kms_key_id)
        public_key_der = response["PublicKey"]

        # Extract raw public key from DER encoding
        # For secp256k1, the DER-encoded public key has a standard prefix
        # The raw key starts at offset 23 for uncompressed, or needs compression
        # This is a simplified version; production would use proper ASN.1 parsing
        from xrpl.core.keypairs import derive_classic_address
        # TODO: Implement full DER → XRPL address derivation for production
        logger.warning("KMS address derivation not fully implemented yet")
        return ""

    def sign_transaction(self, transaction: Transaction) -> str:
        """
        Sign an XRPL transaction and return the signed blob.

        Args:
            transaction: An xrpl-py Transaction model (Payment, TrustSet, etc.)

        Returns:
            Signed transaction blob (hex string) ready for submission.
        """
        if self.config.mode == SigningMode.LOCAL:
            return self._sign_local(transaction)
        elif self.config.mode == SigningMode.KMS:
            return self._sign_kms(transaction)
        else:
            raise ValueError(f"Unknown signing mode: {self.config.mode}")

    def _sign_local(self, transaction: Transaction) -> str:
        """Sign transaction using local wallet seed."""
        if not self._wallet:
            raise RuntimeError("Local wallet not loaded")

        # xrpl-py handles signing internally via submit_and_wait,
        # but for explicit signing we use the wallet's sign method
        from xrpl.transaction import sign as xrpl_sign_tx
        signed = xrpl_sign_tx(transaction, self._wallet)

        logger.info(
            f"[LOCAL] Signed tx: type={transaction.transaction_type}, "
            f"account={transaction.account}"
        )
        return signed.blob()

    def _sign_kms(self, transaction: Transaction) -> str:
        """
        Sign transaction using AWS KMS secp256k1 key.

        Flow:
        1. Serialize transaction for signing (XRPL binary format)
        2. SHA-512Half hash (XRPL's native hash for signing)
        3. Send hash to KMS for ECDSA signature
        4. Attach DER-encoded signature to transaction
        5. Return signed blob

        The private key NEVER leaves the KMS HSM.
        """
        if not self._kms_client or not self.config.kms_key_id:
            raise RuntimeError("KMS client not initialized or key ID not set")

        # Step 1: Serialize for signing
        signing_blob = encode_for_signing(transaction.to_dict())
        signing_bytes = bytes.fromhex(signing_blob)

        # Step 2: Hash with SHA-512Half (XRPL's signing hash)
        hash_full = hashlib.sha512(signing_bytes).digest()
        hash_half = hash_full[:32]  # First 32 bytes = SHA-512Half

        # Step 3: Sign with KMS
        response = self._kms_client.sign(
            KeyId=self.config.kms_key_id,
            Message=hash_half,
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        signature_der = response["Signature"]

        logger.info(
            f"[KMS] Signed tx: type={transaction.transaction_type}, "
            f"key={self.config.kms_key_id[:8]}..."
        )

        # Step 4: Convert DER signature to XRPL format and attach
        # Note: XRPL expects the signature in a specific format within
        # the serialized transaction. This requires DER → (r,s) extraction
        # and then re-encoding in XRPL's TxnSignature field format.
        #
        # For Phase 1, this is stubbed — full implementation requires:
        # - Extracting r,s integers from DER
        # - Encoding as XRPL-compatible hex
        # - Setting TxnSignature + SigningPubKey on the transaction
        # - Re-serializing the complete signed transaction
        #
        # Production implementation will be completed in Phase 2.
        raise NotImplementedError(
            "KMS signing DER-to-XRPL conversion not yet implemented. "
            "Use LOCAL mode for testnet. KMS mode will be completed in Phase 2 "
            "when we deploy to mainnet."
        )

    def get_wallet(self) -> Optional[Wallet]:
        """Get the underlying wallet (LOCAL mode only). Used by MCP server for submit_and_wait."""
        if self.config.mode == SigningMode.LOCAL:
            return self._wallet
        return None


def create_signer(
    mode: str = "local",
    kms_key_id: Optional[str] = None,
    wallet_name: str = "execution",
    aws_region: str = "us-west-2",
) -> XRPLSigner:
    """
    Factory function to create a signer with the appropriate mode.

    Args:
        mode: "local" for testnet, "kms" for production
        kms_key_id: AWS KMS key ID (required for kms mode)
        wallet_name: Which wallet to use for local signing
        aws_region: AWS region for KMS

    Returns:
        Configured XRPLSigner instance.
    """
    signing_mode = SigningMode(mode.lower())

    if signing_mode == SigningMode.KMS and not kms_key_id:
        kms_key_id = os.environ.get("KMS_KEY_ID")
        if not kms_key_id:
            raise ValueError("KMS key ID required for KMS mode. Set KMS_KEY_ID env var or pass kms_key_id.")

    config = SigningConfig(
        mode=signing_mode,
        kms_key_id=kms_key_id,
        aws_region=aws_region,
        wallet_name=wallet_name,
    )

    return XRPLSigner(config)
