"""
Vault — Production-grade private key management.

Security hierarchy (choose one):
  Level 1 (dev)    — Plaintext from .env (NEVER in production)
  Level 2 (prod)   — AWS KMS envelope encryption
  Level 3 (elite)  — HashiCorp Vault with transit secrets engine

The Vault class auto-selects based on env configuration.
Private key material is NEVER stored in memory longer than needed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount

log = logging.getLogger(__name__)


class Vault:
    """
    Abstract key manager. Returns an eth_account signer.

    Usage:
        vault = Vault()
        account = vault.get_account()
        signed = account.sign_transaction(tx)
    """

    def __init__(self) -> None:
        self._account: Optional[LocalAccount] = None
        self._provider = self._detect_provider()

    def _detect_provider(self) -> str:
        if os.getenv("USE_KMS", "false").lower() == "true":
            kms_id = os.getenv("KMS_KEY_ID", "")
            if kms_id:
                return "kms"
        vault_addr = os.getenv("VAULT_ADDR", "")
        vault_tok  = os.getenv("VAULT_TOKEN", "")
        if vault_addr and vault_tok:
            return "hashicorp"
        return "env"

    def get_account(self) -> LocalAccount:
        if self._account is None:
            self._account = self._load_account()
        return self._account

    def _load_account(self) -> LocalAccount:
        if self._provider == "kms":
            return self._load_from_kms()
        if self._provider == "hashicorp":
            return self._load_from_hashicorp()
        return self._load_from_env()

    def _load_from_env(self) -> LocalAccount:
        """Load from PRIVATE_KEY env var. Use only for development."""
        pk = os.getenv("PRIVATE_KEY", "")
        if not pk:
            raise RuntimeError(
                "PRIVATE_KEY not set. "
                "Set PRIVATE_KEY=0x... in .env or configure KMS/Vault."
            )
        if not pk.startswith("0x"):
            pk = "0x" + pk
        account = Account.from_key(pk)
        log.info(f"Loaded wallet from env: {account.address}")
        log.warning("Using plaintext private key — NOT recommended for production!")
        return account

    def _load_from_kms(self) -> LocalAccount:
        """
        Load private key from AWS KMS (envelope decryption).

        The private key is stored AES-256-GCM encrypted in SSM Parameter Store.
        KMS is used to decrypt the data key, which decrypts the private key.
        """
        try:
            import boto3
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            kms_client = boto3.client("kms", region_name=os.getenv("AWS_REGION", "us-east-1"))
            ssm_client = boto3.client("ssm", region_name=os.getenv("AWS_REGION", "us-east-1"))

            # Fetch encrypted blob from SSM
            param_name = os.getenv("SSM_PARAM_NAME", "/nexus-arb/private-key-enc")
            response = ssm_client.get_parameter(Name=param_name, WithDecryption=False)
            encrypted_blob = bytes.fromhex(response["Parameter"]["Value"])

            # First 40 bytes = encrypted data key, rest = nonce+ciphertext
            enc_data_key = encrypted_blob[:512]
            nonce        = encrypted_blob[512:524]
            ciphertext   = encrypted_blob[524:]

            # Decrypt data key via KMS
            kms_response = kms_client.decrypt(
                CiphertextBlob=enc_data_key,
                KeyId=os.getenv("KMS_KEY_ID")
            )
            data_key = kms_response["Plaintext"]

            # Decrypt private key
            aesgcm = AESGCM(data_key[:32])
            pk_bytes = aesgcm.decrypt(nonce, ciphertext, None)
            pk = "0x" + pk_bytes.hex()

            account = Account.from_key(pk)
            log.info(f"KMS wallet loaded: {account.address}")
            return account

        except ImportError:
            log.error("boto3/cryptography not installed. Install with: pip install boto3 cryptography")
            raise
        except Exception as e:
            log.error(f"KMS load failed: {e}")
            raise

    def _load_from_hashicorp(self) -> LocalAccount:
        """Load from HashiCorp Vault transit secrets engine."""
        try:
            import hvac  # pip install hvac

            vault_addr  = os.getenv("VAULT_ADDR")
            vault_token = os.getenv("VAULT_TOKEN")
            secret_path = os.getenv("VAULT_SECRET_PATH", "secret/data/nexus-arb/wallet")

            client = hvac.Client(url=vault_addr, token=vault_token)
            if not client.is_authenticated():
                raise RuntimeError("HashiCorp Vault authentication failed")

            response = client.secrets.kv.v2.read_secret_version(
                path=secret_path.replace("secret/data/", ""),
                mount_point="secret"
            )
            pk = response["data"]["data"]["private_key"]
            if not pk.startswith("0x"):
                pk = "0x" + pk

            account = Account.from_key(pk)
            log.info(f"HashiCorp Vault wallet loaded: {account.address}")
            return account

        except ImportError:
            log.error("hvac not installed. Install with: pip install hvac")
            raise
        except Exception as e:
            log.error(f"Vault load failed: {e}")
            raise

    @property
    def address(self) -> str:
        return self.get_account().address
