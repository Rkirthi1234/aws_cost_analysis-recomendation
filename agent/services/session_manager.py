"""Session manager for cross-account AWS access with credential caching."""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

from agent.services.account_registry import AccountEntry, AccountRegistry

logger = logging.getLogger(__name__)

BILLING_SERVICES = {"ce", "cur", "budgets", "cost-optimization-hub", "bcm-data-exports"}


def _mask_account_id(account_id: str) -> str:
    """T16 Mitigation: Mask account IDs in log output to prevent information disclosure."""
    if len(account_id) >= 8:
        return account_id[:4] + "****" + account_id[-4:]
    return "****"


@dataclass
class CachedSession:
    """Cached STS assumed-role credentials."""
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime
    region: str


class SessionManager:
    """Handles STS role assumption, credential caching, and boto3 session/client creation."""

    REFRESH_BUFFER_SECONDS = 300  # 5 minutes before expiry
    # T2 Mitigation: Reduced session duration from 1 hour to 15 minutes
    # to limit the window of exposure if credentials are compromised.
    # Credentials are automatically refreshed when they expire.
    SESSION_DURATION_SECONDS = 900  # 15 minutes
    AGENT_SESSION_PREFIX = "cost-analyzer-agent"

    def __init__(self, account_registry: AccountRegistry, sts_client=None):
        self._registry = account_registry
        self._default_region = account_registry._default_region
        self._sts_client = sts_client or boto3.client("sts")
        self._cache: Dict[str, CachedSession] = {}

    def get_session(self, account_id: str) -> boto3.Session:
        """Get a boto3 Session for the given account, assuming role if needed.

        When role_arn is None, returns a default session using the execution
        role credentials (no STS call). When role_arn is present, assumes the
        role via STS with credential caching.
        """
        entry = self._registry.get_account(account_id)

        # No role_arn → return default session (no STS call)
        if entry.role_arn is None:
            return boto3.Session(
                region_name=entry.region or self._default_region,
            )

        # role_arn present → assume role with caching
        if not self._is_cache_valid(account_id):
            self._cache[account_id] = self._assume_role(entry)

        cached = self._cache[account_id]
        return boto3.Session(
            aws_access_key_id=cached.access_key_id,
            aws_secret_access_key=cached.secret_access_key,
            aws_session_token=cached.session_token,
            region_name=cached.region,
        )

    def get_client(self, account_id: str, service_name: str, region: str = None) -> Any:
        """Get a boto3 client for a service in a specific account.

        For payer accounts requesting billing services, forces us-east-1.
        """
        entry = self._registry.get_account(account_id)
        session = self.get_session(account_id)
        effective_region = region or self._get_region(entry, service_name)
        return session.client(service_name, region_name=effective_region)

    def _assume_role(self, entry: AccountEntry) -> CachedSession:
        """Assume an IAM role in the target account and return cached credentials."""
        params = {
            "RoleArn": entry.role_arn,
            "RoleSessionName": self.AGENT_SESSION_PREFIX,
            "DurationSeconds": self.SESSION_DURATION_SECONDS,
        }
        if entry.external_id is not None:
            params["ExternalId"] = entry.external_id

        try:
            response = self._sts_client.assume_role(**params)
        except Exception as e:
            raise RuntimeError(
                f"Failed to assume role for account {_mask_account_id(entry.account_id)} "
                f"(role configured): {e}"
            ) from e

        creds = response["Credentials"]
        return CachedSession(
            access_key_id=creds["AccessKeyId"],
            secret_access_key=creds["SecretAccessKey"],
            session_token=creds["SessionToken"],
            expiration=creds["Expiration"],
            region=self._get_region(entry),
        )

    def _is_cache_valid(self, account_id: str) -> bool:
        """Check if cached credentials exist and are not within the refresh buffer."""
        if account_id not in self._cache:
            return False
        cached = self._cache[account_id]
        now = datetime.now(timezone.utc)
        remaining = (cached.expiration - now).total_seconds()
        return remaining > self.REFRESH_BUFFER_SECONDS

    def _get_region(self, entry: AccountEntry, service_name: str = None) -> str:
        """Determine the effective region for an account/service combination.

        Payer accounts force us-east-1 for billing services.
        """
        if entry.account_type == "payer" and service_name in BILLING_SERVICES:
            return "us-east-1"
        return entry.region or "us-east-1"
