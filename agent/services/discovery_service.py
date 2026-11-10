"""Discovery service for automatic Athena table detection using AWS Glue Catalog.

Discovers tables in an Athena database via the Glue API (get_tables),
then classifies each table by column signature (CUR, VPC Flow Log, or unknown).
Results are cached per database for the lifetime of the agent session.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import boto3

from agent.services.table_classifier import TableType, classify_table

logger = logging.getLogger("DiscoveryService")


class DiscoveryError(Exception):
    """Raised when table discovery fails or produces ambiguous results."""
    pass


@dataclass
class TableInfo:
    """Metadata for a single discovered table."""
    name: str
    table_type: TableType
    column_names: list[str]


@dataclass
class DiscoveryResult:
    """Result of discovering tables in a database."""
    database: str
    tables: list[TableInfo]
    cur_tables: list[TableInfo] = field(default_factory=list)
    vpc_flow_log_tables: list[TableInfo] = field(default_factory=list)
    unknown_tables: list[TableInfo] = field(default_factory=list)


class DiscoveryService:
    """Discovers and classifies Athena tables using the Glue Catalog API.

    Uses boto3 Glue client (get_tables) for fast, free table discovery
    instead of running Athena queries. Results are cached in memory
    per database name for the session lifetime.
    """

    def __init__(self, session: boto3.Session, region: str = "us-east-1") -> None:
        self._glue = session.client("glue", region_name=region)
        self._cache: dict[str, DiscoveryResult] = {}

    def discover_tables(self, database: str) -> DiscoveryResult:
        """Discover and classify all tables in a database via Glue API.

        Returns cached results if available. Otherwise calls glue:GetTables
        and classifies each table by its column names.

        Args:
            database: The Glue/Athena database name.

        Returns:
            DiscoveryResult with all tables categorized by type.

        Raises:
            DiscoveryError: If the Glue API call fails.
        """
        if database in self._cache:
            logger.debug(f"Returning cached discovery result for database '{database}'")
            return self._cache[database]

        try:
            # Get all tables from Glue Catalog
            tables_response = self._glue.get_tables(DatabaseName=database)
            glue_tables = tables_response.get("TableList", [])

            # Handle pagination if needed
            while "NextToken" in tables_response:
                tables_response = self._glue.get_tables(
                    DatabaseName=database, NextToken=tables_response["NextToken"]
                )
                glue_tables.extend(tables_response.get("TableList", []))

        except self._glue.exceptions.EntityNotFoundException:
            raise DiscoveryError(
                f"Database '{database}' not found in Glue Catalog. "
                f"Verify the database exists and you have glue:GetTables permission."
            )
        except Exception as e:
            raise DiscoveryError(
                f"Failed to discover tables in database '{database}': {e}. "
                f"Verify you have glue:GetTables and glue:GetTable permissions."
            )

        logger.info(f"Glue returned {len(glue_tables)} table(s) in database '{database}'")

        if not glue_tables:
            result = DiscoveryResult(database=database, tables=[])
            self._cache[database] = result
            return result

        # Classify each table by its columns
        all_tables: list[TableInfo] = []
        cur_tables: list[TableInfo] = []
        vpc_flow_log_tables: list[TableInfo] = []
        unknown_tables: list[TableInfo] = []

        for glue_table in glue_tables:
            table_name = glue_table["Name"]
            # Extract column names from StorageDescriptor
            columns = glue_table.get("StorageDescriptor", {}).get("Columns", [])
            # Also include partition keys as columns
            partition_keys = glue_table.get("PartitionKeys", [])
            column_names = [col["Name"] for col in columns] + [pk["Name"] for pk in partition_keys]

            table_type = classify_table(column_names)
            table_info = TableInfo(
                name=table_name,
                table_type=table_type,
                column_names=column_names,
            )

            all_tables.append(table_info)
            if table_type == TableType.CUR:
                cur_tables.append(table_info)
            elif table_type == TableType.VPC_FLOW_LOG:
                vpc_flow_log_tables.append(table_info)
            else:
                unknown_tables.append(table_info)

        result = DiscoveryResult(
            database=database,
            tables=all_tables,
            cur_tables=cur_tables,
            vpc_flow_log_tables=vpc_flow_log_tables,
            unknown_tables=unknown_tables,
        )

        self._cache[database] = result
        logger.info(
            f"Discovered {len(all_tables)} table(s) in '{database}': "
            f"{len(cur_tables)} CUR, {len(vpc_flow_log_tables)} VPC Flow Log, "
            f"{len(unknown_tables)} unknown"
        )

        return result

    def resolve_table(
        self,
        database: str,
        table_type: TableType,
        explicit_table: Optional[str],
    ) -> str:
        """Resolve a table name: use explicit if provided, otherwise discover.

        When explicit_table is set, returns it directly (backward compatibility).
        Otherwise discovers tables and selects based on type. If classification
        doesn't match but tables exist, uses the first table (user already
        specified the database purpose).

        Args:
            database: The Athena/Glue database name.
            table_type: The type of table to resolve (CUR or VPC_FLOW_LOG).
            explicit_table: An explicitly configured table name, or None.

        Returns:
            The resolved table name.

        Raises:
            DiscoveryError: If no tables found in the database.
        """
        if explicit_table is not None:
            return explicit_table

        result = self.discover_tables(database)

        # Try classified matches first
        if table_type == TableType.CUR:
            matches = result.cur_tables
            type_label = "CUR"
        elif table_type == TableType.VPC_FLOW_LOG:
            matches = result.vpc_flow_log_tables
            type_label = "VPC Flow Log"
        else:
            matches = result.tables
            type_label = "unknown"

        if len(matches) == 1:
            return matches[0].name

        if len(matches) > 1:
            # Multiple classified matches — use the first one
            logger.info(f"Multiple {type_label} tables found, using '{matches[0].name}'")
            return matches[0].name

        # No classified matches — but user told us this database is for this purpose
        # Use the first table available
        if result.tables:
            selected = result.tables[0]
            logger.info(
                f"No tables matched {type_label} signature in '{database}', "
                f"using table '{selected.name}' as configured by user"
            )
            return selected.name

        raise DiscoveryError(
            f"No tables found in database '{database}'. "
            f"Verify the database exists and contains tables."
        )
