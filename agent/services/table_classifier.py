"""Table classifier module for identifying Athena table types by column signature.

Classifies tables as CUR (Cost and Usage Report), VPC Flow Log, or unknown
based on the presence of required column names. All matching is case-insensitive.
"""

from enum import Enum


class TableType(Enum):
    """Classification of an Athena table based on its column signature."""

    CUR = "cur"
    VPC_FLOW_LOG = "vpc_flow_log"
    UNKNOWN = "unknown"


# Column signatures — all matching is case-insensitive
CUR_REQUIRED_COLUMNS: set[str] = {
    "line_item_unblended_cost",
    "bill_billing_period_start_date",
    "line_item_product_code",
}

VPC_FLOW_LOG_REQUIRED_COLUMNS: set[str] = {
    "srcaddr",
    "dstaddr",
    "bytes",
}


def classify_table(column_names: list[str]) -> TableType:
    """Classify a table by checking column names against known signatures.

    Checks CUR signature first, then VPC Flow Log. Returns UNKNOWN if
    neither signature matches.

    Args:
        column_names: List of column names from DESCRIBE output.

    Returns:
        TableType indicating the classification.
    """
    lower_columns = {c.lower() for c in column_names}

    if CUR_REQUIRED_COLUMNS.issubset(lower_columns):
        return TableType.CUR
    if VPC_FLOW_LOG_REQUIRED_COLUMNS.issubset(lower_columns):
        return TableType.VPC_FLOW_LOG
    return TableType.UNKNOWN
