"""Multi-account query executor with parallel execution and graceful degradation."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import boto3

from agent.services.account_registry import AccountRegistry
from agent.services.session_manager import SessionManager

logger = logging.getLogger(__name__)


@dataclass
class AccountQueryResult:
    """Result of a query executed against a single account."""
    account_id: str
    success: bool
    data: Optional[Dict] = None
    error: Optional[str] = None


@dataclass
class MultiAccountResult:
    """Aggregated results from executing queries across multiple accounts."""
    results: List[AccountQueryResult] = field(default_factory=list)

    @property
    def succeeded(self) -> List[AccountQueryResult]:
        """Return results for accounts that succeeded."""
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> List[AccountQueryResult]:
        """Return results for accounts that failed."""
        return [r for r in self.results if not r.success]

    @property
    def all_failed(self) -> bool:
        """True if every account query failed."""
        return len(self.results) > 0 and all(not r.success for r in self.results)

    def summary(self) -> str:
        """Human-readable summary of successes and failures."""
        if not self.results:
            return "No accounts queried."

        if self.all_failed:
            lines = ["All accounts failed:"]
            for r in self.failed:
                lines.append(f"  - {r.account_id}: {r.error}")
            return "\n".join(lines)

        lines = []
        if self.succeeded:
            acct_ids = ", ".join(r.account_id for r in self.succeeded)
            lines.append(f"Succeeded: {acct_ids}")
        if self.failed:
            lines.append("Failed:")
            for r in self.failed:
                lines.append(f"  - {r.account_id}: {r.error}")
        return "\n".join(lines)


class MultiAccountQueryExecutor:
    """Executes a query function across multiple accounts in parallel."""

    def __init__(self, session_manager: SessionManager, account_registry: AccountRegistry):
        self._session_manager = session_manager
        self._registry = account_registry

    def execute_across_accounts(
        self,
        account_ids: List[str],
        query_fn: Callable[[str, boto3.Session], Dict],
    ) -> MultiAccountResult:
        """Execute query_fn for each account_id in parallel using ThreadPoolExecutor.

        For each account, obtains a session from SessionManager and calls
        query_fn(account_id, session). Failures are captured per-account so
        that remaining accounts can still return results.
        """
        results: List[AccountQueryResult] = []

        if not account_ids:
            return MultiAccountResult(results=results)

        with ThreadPoolExecutor(max_workers=len(account_ids)) as executor:
            future_to_account = {
                executor.submit(self._execute_single, account_id, query_fn): account_id
                for account_id in account_ids
            }

            for future in as_completed(future_to_account):
                account_id = future_to_account[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as exc:
                    # Defensive: should not happen since _execute_single catches errors,
                    # but guard against unexpected issues in the executor itself.
                    logger.error("Unexpected executor error for account %s: %s", account_id, exc)
                    results.append(AccountQueryResult(
                        account_id=account_id,
                        success=False,
                        error=str(exc),
                    ))

        return MultiAccountResult(results=results)

    def _execute_single(
        self,
        account_id: str,
        query_fn: Callable[[str, boto3.Session], Dict],
    ) -> AccountQueryResult:
        """Execute query_fn for a single account, capturing any errors."""
        try:
            session = self._session_manager.get_session(account_id)
            data = query_fn(account_id, session)
            return AccountQueryResult(
                account_id=account_id,
                success=True,
                data=data,
            )
        except Exception as exc:
            logger.warning("Query failed for account %s: %s", account_id, exc)
            return AccountQueryResult(
                account_id=account_id,
                success=False,
                error=str(exc),
            )
