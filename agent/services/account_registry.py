"""Account registry for cross-account AWS access configuration."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


VALID_ACCOUNT_TYPES = {"payer", "member"}


@dataclass
class AthenaConfig:
    """Holds database/table for a single Athena dataset."""
    database: str
    table: Optional[str] = None


@dataclass
class AccountEntry:
    """A single target AWS account configuration."""
    account_id: str
    account_type: str  # "payer" or "member"
    role_arn: Optional[str] = None
    external_id: Optional[str] = None
    region: Optional[str] = None
    services: Optional[List[str]] = field(default_factory=list)
    athena_cur: Optional[AthenaConfig] = None
    athena_vpc_flowlogs: Optional[AthenaConfig] = None


class AccountRegistry:
    """Parses, validates, and stores account entries from YAML config."""

    REQUIRED_FIELDS = ("account_id", "account_type")

    def __init__(self, accounts_config: List[dict], default_region: str):
        self.entries: Dict[str, AccountEntry] = {}
        self._default_region = default_region
        self._parse_and_validate(accounts_config, default_region)

    def _parse_and_validate(self, accounts_config: List[dict], default_region: str) -> None:
        seen_ids: Dict[str, int] = {}

        for idx, entry_dict in enumerate(accounts_config):
            # Validate required fields
            for req in self.REQUIRED_FIELDS:
                if req not in entry_dict or entry_dict[req] is None:
                    raise ValueError(
                        f"Account entry {idx}: missing required field '{req}'"
                    )

            account_type = entry_dict["account_type"]
            if account_type not in VALID_ACCOUNT_TYPES:
                raise ValueError(
                    f"Account entry {idx}: invalid account_type '{account_type}'. "
                    f"Must be one of: {', '.join(sorted(VALID_ACCOUNT_TYPES))}"
                )

            account_id = str(entry_dict["account_id"])

            if account_id in seen_ids:
                raise ValueError(
                    f"Account entry {idx}: duplicate account_id '{account_id}' "
                    f"(first seen at entry {seen_ids[account_id]})"
                )
            seen_ids[account_id] = idx

            # Parse optional athena sub-object
            athena_cur = None
            athena_vpc_flowlogs = None
            athena_dict = entry_dict.get("athena")
            if athena_dict:
                athena_cur = self._parse_athena_sub_object(
                    athena_dict.get("cur"), "cur", idx, account_id
                )
                athena_vpc_flowlogs = self._parse_athena_sub_object(
                    athena_dict.get("vpc_flowlogs"), "vpc_flowlogs", idx, account_id
                )

            account_entry = AccountEntry(
                account_id=account_id,
                account_type=account_type,
                role_arn=entry_dict.get("role_arn"),
                external_id=entry_dict.get("external_id"),
                region=entry_dict.get("region", default_region),
                services=entry_dict.get("services", []),
                athena_cur=athena_cur,
                athena_vpc_flowlogs=athena_vpc_flowlogs,
            )
            self.entries[account_id] = account_entry

        # Validate exactly one payer account
        payer_entries = [
            (aid, e) for aid, e in self.entries.items() if e.account_type == "payer"
        ]
        if len(payer_entries) != 1:
            raise ValueError(
                f"Configuration must contain exactly one payer account, found {len(payer_entries)}"
            )

        # Validate payer has role_arn
        payer_id, payer_entry = payer_entries[0]
        if payer_entry.role_arn is None:
            # Find the original index for the error message
            payer_idx = list(self.entries.keys()).index(payer_id)
            raise ValueError(
                f"Account entry {payer_idx}: payer account '{payer_id}' must have a role_arn"
            )

    @staticmethod
    def _parse_athena_sub_object(
        sub_dict: Optional[dict],
        sub_name: str,
        entry_idx: int,
        account_id: str,
    ) -> Optional[AthenaConfig]:
        """Parse and validate an athena sub-object (cur or vpc_flowlogs)."""
        if sub_dict is None:
            return None

        # Only database is required; table is optional (None triggers auto-discovery)
        if "database" not in sub_dict or sub_dict["database"] is None:
            raise ValueError(
                f"Account entry {entry_idx} (account_id '{account_id}'): "
                f"athena.{sub_name} missing required field 'database'"
            )

        return AthenaConfig(
            database=sub_dict["database"],
            table=sub_dict.get("table"),
        )

    def get_account(self, account_id: str) -> AccountEntry:
        if account_id not in self.entries:
            raise KeyError(f"Account '{account_id}' not found in registry")
        return self.entries[account_id]

    def get_accounts_by_type(self, account_type: str) -> List[AccountEntry]:
        return [e for e in self.entries.values() if e.account_type == account_type]

    def get_payer_account(self) -> Optional[AccountEntry]:
        payers = self.get_accounts_by_type("payer")
        return payers[0] if payers else None

    def get_member_accounts(self) -> List[AccountEntry]:
        return self.get_accounts_by_type("member")

    def get_cur_account(self) -> Optional[AccountEntry]:
        """Return the first account with athena_cur configured."""
        for entry in self.entries.values():
            if entry.athena_cur is not None:
                return entry
        return None

    def get_vpc_flowlogs_accounts(self) -> List[AccountEntry]:
        """Return all accounts with athena_vpc_flowlogs configured."""
        return [e for e in self.entries.values() if e.athena_vpc_flowlogs is not None]

    @staticmethod
    def _athena_config_to_dict(config: AthenaConfig) -> dict:
        """Serialize an AthenaConfig, omitting table when it is None."""
        d: dict = {"database": config.database}
        if config.table is not None:
            d["table"] = config.table
        return d

    def to_dict_list(self) -> List[dict]:
        """Serialize entries back to a list of dicts (YAML round-trip)."""
        result = []
        for entry in self.entries.values():
            d = {
                "account_id": entry.account_id,
                "account_type": entry.account_type,
            }
            if entry.role_arn is not None:
                d["role_arn"] = entry.role_arn
            if entry.external_id is not None:
                d["external_id"] = entry.external_id
            if entry.region is not None:
                d["region"] = entry.region
            if entry.services:
                d["services"] = list(entry.services)

            # Serialize athena sub-objects into nested dict
            athena = {}
            if entry.athena_cur is not None:
                athena["cur"] = self._athena_config_to_dict(entry.athena_cur)
            if entry.athena_vpc_flowlogs is not None:
                athena["vpc_flowlogs"] = self._athena_config_to_dict(entry.athena_vpc_flowlogs)
            if athena:
                d["athena"] = athena

            result.append(d)
        return result
