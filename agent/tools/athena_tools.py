"""Athena query tools."""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

from strands import tool

from .base_tool import BaseTool
from agent.services.athena_service import AthenaService
from agent.services.table_classifier import TableType
from agent.services.discovery_service import DiscoveryError

if TYPE_CHECKING:
    from agent.services.discovery_service import DiscoveryService

logger = logging.getLogger("AthenaTools")


class AthenaTools(BaseTool):
    """Provides Athena query execution tools."""
    
    def __init__(self, athena_service: Optional[AthenaService] = None, vpc_flowlog_config: dict = None,
                 member_athena_services: dict = None, multi_account_executor=None,
                 discovery_service: Optional['DiscoveryService'] = None):
        self.athena_service = athena_service
        self.vpc_flowlog_config = vpc_flowlog_config or {}
        self.member_athena_services = member_athena_services or {}
        self.multi_account_executor = multi_account_executor
        self.discovery_service = discovery_service
    
    def _resolve_cur_table(self) -> Optional[str]:
        """Resolve the CUR table name via discovery if not explicitly configured.

        Returns:
            None on success (table is set on self.athena_service),
            or an error message string if resolution fails.
        """
        if self.athena_service is None:
            return None

        # If table is already set (explicitly configured), nothing to do
        if self.athena_service.table is not None:
            return None

        # If no discovery service, we can't resolve
        if self.discovery_service is None:
            return (
                f"No table configured for CUR in database '{self.athena_service.database}' "
                f"and no discovery service available. Specify the table name in config.yaml."
            )

        try:
            resolved = self.discovery_service.resolve_table(
                self.athena_service.database, TableType.CUR, None
            )
            self.athena_service.table = resolved
            logger.info(f"Auto-discovered CUR table: {self.athena_service.database}.{resolved}")
            return None
        except DiscoveryError as e:
            return str(e)

    def _resolve_vpc_table(self, service: AthenaService) -> Optional[str]:
        """Resolve a VPC Flow Log table name via discovery if not explicitly configured.

        Args:
            service: The AthenaService instance for a specific VPC Flow Log account.

        Returns:
            None on success (table is set on the service),
            or an error message string if resolution fails.
        """
        # If table is already set (explicitly configured), nothing to do
        if service.table is not None:
            return None

        # If no discovery service, we can't resolve
        if self.discovery_service is None:
            return (
                f"No table configured for VPC Flow Logs in database '{service.database}' "
                f"and no discovery service available. Specify the table name in config.yaml."
            )

        try:
            resolved = self.discovery_service.resolve_table(
                service.database, TableType.VPC_FLOW_LOG, None
            )
            service.table = resolved
            logger.info(f"Auto-discovered VPC Flow Log table: {service.database}.{resolved}")
            return None
        except DiscoveryError as e:
            return str(e)

    def get_tools(self):
        tools = []
        
        # Include CUR tools only when athena_service is configured
        if self.athena_service is not None:
            tools.extend([
                self.get_cur_schema_info,
                self.execute_cur_athena_query
            ])
        
        # Add VPC Flow Logs tools if configured
        if self.vpc_flowlog_config.get('enabled', False):
            tools.extend([
                self.get_vpc_flowlog_schema_info,
                self.execute_vpc_flowlog_query
            ])
        
        # Add multi-account VPC flow log tool when executor is available
        if self.multi_account_executor is not None:
            tools.append(self.execute_multi_account_vpc_flowlog_query)
        
        return tools
    
    @tool
    def get_cur_schema_info(self) -> str:
        """Get CUR 2.0 schema information for detailed resource-level queries.
        
        CRITICAL: Call this ONCE per conversation before writing Athena queries.
        
        Returns:
            Key columns, query patterns, and best practices for CUR analysis
        """
        # Lazy resolution: ensure CUR table is resolved before use
        error = self._resolve_cur_table()
        if error:
            return error

        # Generate dynamic date examples based on current date
        now = datetime.now()
        if now.month == 1:
            last_month_start = datetime(now.year - 1, 12, 1)
            last_month_end = datetime(now.year, 1, 1)
        else:
            last_month_start = datetime(now.year, now.month - 1, 1)
            last_month_end = datetime(now.year, now.month, 1)

        info = {
            "table": f"{self.athena_service.database}.{self.athena_service.table}",
            
            "key_columns": {
                "partition": "bill_billing_period_start_date (ALWAYS use in WHERE clause for performance)",
                "cost": "line_item_unblended_cost (use SUM for aggregations)",
                "service": "line_item_product_code (e.g., 'AmazonEC2', 'AmazonElastiCache')",
                "account": "line_item_usage_account_id",
                "resource": "line_item_resource_id (EC2 instance IDs, S3 bucket names, etc.)",
                "usage_type": "line_item_usage_type",
                "region": "product_region (NOT product.region - no struct)",
                "operation": "line_item_operation"
            },
            
            "best_practices": {
                "1_partition_filter": "ALWAYS filter by bill_billing_period_start_date (partition key) - REQUIRED for performance",
                "2_comprehensive_query": "Write ONE comprehensive query with all needed info - avoid multiple queries",
                "3_group_by": "Use GROUP BY with multiple dimensions for complete breakdowns",
                "4_resource_filter": "Filter out empty resource IDs: WHERE line_item_resource_id != ''",
                "5_order_limit": "Always ORDER BY cost DESC and LIMIT results (e.g., LIMIT 20)",
                "6_date_format": "Use DATE('YYYY-MM-DD') format for partition filter"
            },
            
            "common_service_codes": {
                "EC2": "AmazonEC2",
                "S3": "AmazonS3",
                "RDS": "AmazonRDS",
                "Lambda": "AWSLambda",
                "ElastiCache": "AmazonElastiCache",
                "DynamoDB": "AmazonDynamoDB",
                "ECS": "AmazonECS",
                "EKS": "AmazonEKS"
            },
            
            "example_query_template": f"""
-- Top resources by cost for a service (last complete month)
-- IMPORTANT: Replace dates with actual target period. Use get_current_date_context() for ranges.
SELECT 
    line_item_resource_id,
    line_item_product_code,
    SUM(line_item_unblended_cost) as total_cost,
    COUNT(*) as line_items
FROM {self.athena_service.database}.{self.athena_service.table}
WHERE bill_billing_period_start_date >= DATE('{last_month_start.strftime('%Y-%m-%d')}')
    AND bill_billing_period_start_date < DATE('{last_month_end.strftime('%Y-%m-%d')}')
    AND line_item_product_code = 'AmazonElastiCache'
    AND line_item_resource_id != ''
GROUP BY line_item_resource_id, line_item_product_code
ORDER BY total_cost DESC
LIMIT 20
"""
        }
        
        return json.dumps(info, indent=2)
    
    @tool
    def execute_cur_athena_query(self, sql_query: str) -> str:
        """Execute an Athena query against CUR data for detailed resource-level cost analysis.
        
        BEST PRACTICES (follow these rules):
        1. ALWAYS filter by bill_billing_period_start_date (partition key) - REQUIRED
        2. Write ONE comprehensive query with all needed information
        3. Use GROUP BY with multiple dimensions for complete breakdowns
        4. Filter out empty resource IDs: WHERE line_item_resource_id != ''
        5. Always ORDER BY cost DESC and LIMIT results (e.g., LIMIT 20)
        6. Use DATE('YYYY-MM-DD') format for date filters
        
        Example for top ElastiCache resources in January 2026:
        ```sql
        SELECT 
            line_item_resource_id,
            SUM(line_item_unblended_cost) as total_cost
        FROM database.table
        WHERE bill_billing_period_start_date >= DATE('2026-01-01')
            AND bill_billing_period_start_date < DATE('2026-02-01')
            AND line_item_product_code = 'AmazonElastiCache'
            AND line_item_resource_id != ''
        GROUP BY line_item_resource_id
        ORDER BY total_cost DESC
        LIMIT 20
        ```
        
        Args:
            sql_query: The SQL query to execute (must follow best practices above)
            
        Returns:
            Query results formatted as text with statistics
        """
        if self.athena_service is None:
            return "No CUR Athena configuration found. Configure athena.cur on an account in config.yaml."
        
        # Lazy resolution: ensure CUR table is resolved before use
        error = self._resolve_cur_table()
        if error:
            return error

        result = self.athena_service.execute_query(sql_query)
        
        if result['status'] == 'success':
            table = self.athena_service.format_results_as_table(result['rows'])
            stats = result['statistics']
            
            return f"""Query Results:

{table}

Query Stats: {stats['data_scanned_mb']:.2f} MB scanned in {stats['execution_time_sec']:.2f}s"""
        
        elif result['status'] == 'failed':
            return f"Query failed: {result['error']}"
        else:
            return f"Error executing query: {result['error']}"
    
    @tool
    def get_vpc_flowlog_schema_info(self) -> str:
        """Get VPC Flow Logs schema information for network traffic analysis.
        
        CRITICAL: Call this FIRST before writing VPC Flow Logs queries.
        
        Returns:
        - Actual table columns and data types
        - Partition structure (for query performance)
        - Simple, safe query examples
        - Best practices
        
        Returns:
            Complete schema information with safe query examples
        """
        if not self.vpc_flowlog_config.get('enabled', False):
            return "VPC Flow Logs not configured. Please configure vpc_flowlogs in config.yaml"
        
        database = self.vpc_flowlog_config.get('database', 'vpc_flow_logs')
        table = self.vpc_flowlog_config.get('table', 'flow_logs')
        
        # Use the first available VPC flow log service for schema queries
        if not self.member_athena_services:
            return "No VPC Flow Logs Athena services configured."
        first_service = next(iter(self.member_athena_services.values()))

        # Lazy resolution: ensure VPC table is resolved before use
        error = self._resolve_vpc_table(first_service)
        if error:
            return error

        database = first_service.database
        table = first_service.table
        
        # Get actual table schema
        schema = first_service.get_table_schema(database, table)
        
        if schema['status'] != 'success':
            return f"Failed to get table schema: {schema.get('error', 'Unknown error')}\n\nPlease verify:\n1. Database '{database}' exists\n2. Table '{table}' exists\n3. You have permissions to access the table"
        
        # Get partition information
        partition_info = first_service.get_partition_info(database, table)
        
        # Build partition filter examples
        partition_examples = ""
        if partition_info.get('has_partitions'):
            partition_cols = partition_info.get('partition_columns', [])
            if 'partition_0' in partition_cols:
                partition_examples = """partition_0 = 'us-east-1'
  AND partition_1 = '2026'
  AND partition_2 = '02'
  AND partition_3 = '07'"""
            elif 'year' in partition_cols:
                partition_examples = """year = '2026'
  AND month = '02'
  AND day = '07'"""
            elif 'date' in partition_cols:
                partition_examples = """date = '2026-02-07'"""
        
        # Generate dynamic timestamp examples for VPC queries
        # Use yesterday as a safe default example date
        yesterday = datetime.now() - timedelta(days=1)
        example_date = yesterday.strftime('%Y-%m-%d')
        
        info = {
            "table": f"{database}.{table}",
            "status": "validated",
            
            "columns": schema['column_names'],
            "partitions": partition_info.get('partition_columns', []),
            
            "timestamp_guidance": f"""
VPC Flow Logs use Unix epoch timestamps (seconds since 1970-01-01 UTC) for start/end fields.

To convert dates to timestamps in Athena SQL, use:
  to_unixtime(TIMESTAMP 'YYYY-MM-DD HH:MM:SS')

Example for {example_date}:
  start >= to_unixtime(TIMESTAMP '{example_date} 00:00:00')
  AND end <= to_unixtime(TIMESTAMP '{example_date} 23:59:59')

ALWAYS use get_current_date_context() to determine the correct date range,
then use to_unixtime() in your query. Do NOT hardcode epoch values.
""",
            
            "simple_query_examples": {
                
                "1_egress_traffic_by_source": f"""
-- Egress traffic by source instance (AVOIDS DOUBLE-COUNTING + CORRECT IP MAPPING)
-- With flow_direction='egress': instance_id = true owner of srcaddr
-- Without this filter, instance_id may show the RECEIVER's instance for the same srcaddr
SELECT 
    srcaddr,
    dstaddr,
    instance_id as source_instance,
    az_id as source_az,
    SUM(bytes) / 1073741824.0 as gb_transferred,
    COUNT(*) as flow_count
FROM {database}.{table}
WHERE {partition_examples}
  AND start >= to_unixtime(TIMESTAMP '{example_date} 00:00:00')
  AND "end" <= to_unixtime(TIMESTAMP '{example_date} 23:59:59')
  AND log_status = 'OK'
  AND action = 'ACCEPT'
  AND flow_direction = 'egress'
GROUP BY srcaddr, dstaddr, instance_id, az_id
ORDER BY gb_transferred DESC
LIMIT 100
""",
                
                "2_top_instances_egress": f"""
-- Top instances by EGRESS traffic (sender-side only, no double-count)
SELECT 
    instance_id,
    az_id,
    SUM(bytes) / 1073741824.0 as gb_transferred,
    COUNT(*) as flow_count
FROM {database}.{table}
WHERE {partition_examples}
  AND start >= to_unixtime(TIMESTAMP '{example_date} 00:00:00')
  AND "end" <= to_unixtime(TIMESTAMP '{example_date} 23:59:59')
  AND log_status = 'OK'
  AND action = 'ACCEPT'
  AND instance_id != '-'
  AND flow_direction = 'egress'
GROUP BY instance_id, az_id
ORDER BY gb_transferred DESC
LIMIT 50
""",
                
                "3_traffic_by_destination": f"""
-- Traffic received by destination IP (ingress perspective)
-- NOTE: Use flow_direction='ingress' here to see what each destination receives
-- without double-counting with egress records
SELECT 
    dstaddr,
    SUM(bytes) / 1073741824.0 as gb_received,
    COUNT(DISTINCT srcaddr) as unique_sources,
    COUNT(*) as flow_count
FROM {database}.{table}
WHERE {partition_examples}
  AND start >= to_unixtime(TIMESTAMP '{example_date} 00:00:00')
  AND "end" <= to_unixtime(TIMESTAMP '{example_date} 23:59:59')
  AND log_status = 'OK'
  AND action = 'ACCEPT'
  AND flow_direction = 'ingress'
GROUP BY dstaddr
ORDER BY gb_received DESC
LIMIT 50
""",

                "4_fallback_no_flow_direction": f"""
-- FALLBACK: If flow_direction column is NOT available
-- ⚠️ WARNING: Without flow_direction, instance_id does NOT reliably indicate
-- which instance owns srcaddr. The same srcaddr may appear with DIFFERENT
-- instance_ids (sender's ENI vs receiver's ENI both log the same packets).
-- DO NOT conclude one IP belongs to multiple instances from this query.
SELECT 
    srcaddr,
    dstaddr,
    SUM(bytes) / 1073741824.0 as gb_transferred,
    COUNT(*) as flow_count
FROM {database}.{table}
WHERE {partition_examples}
  AND start >= to_unixtime(TIMESTAMP '{example_date} 00:00:00')
  AND "end" <= to_unixtime(TIMESTAMP '{example_date} 23:59:59')
  AND log_status = 'OK'
  AND action = 'ACCEPT'
GROUP BY srcaddr, dstaddr
ORDER BY gb_transferred DESC
LIMIT 100
-- ⚠️ NOTES:
-- 1. Totals may be ~2x actual if both ENIs have flow logging (double-counted).
-- 2. To map IPs to instances, use EC2 DescribeInstances or filter by
--    flow_direction='egress' (if available) where instance_id = srcaddr owner.
-- 3. Cross-reference with CUR data transfer costs for ground truth.
"""
            },
            
            "best_practices": [
                "ALWAYS use partition filters for performance",
                "Filter by time range using start/end timestamps",
                "Filter log_status='OK' for valid records",
                "Filter action='ACCEPT' for successful connections",
                "Convert bytes to GB: bytes / 1073741824.0",
                "Use simple aggregations (SUM, COUNT, GROUP BY)",
                "AVOID complex JOINs - they can cause data duplication",
                "Always LIMIT results (e.g., LIMIT 100)",
                "Use actual column names from 'columns' list above",
                "⚠️ CRITICAL: Use flow_direction='egress' to avoid double-counting (preferred)",
                "⚠️ If flow_direction unavailable, note that totals may be 2x actual transfer",
                "⚠️ Cross-reference with CUR data transfer costs for ground truth on billed bytes",
                "⚠️ Never conclude 'instance sends to itself' — that's a double-count artifact"
            ],
            
            "double_counting_warning": """
⚠️ VPC FLOW LOG DOUBLE-COUNTING AND instance_id SEMANTICS:

VPC Flow Logs record traffic PER ENI. For traffic between instances A and B,
BOTH ENIs generate a record for the SAME packets:
  - A's ENI: srcaddr=A, dstaddr=B, bytes=X, instance_id=A, flow_direction=egress
  - B's ENI: srcaddr=A, dstaddr=B, bytes=X, instance_id=B, flow_direction=ingress

A naive GROUP BY srcaddr, dstaddr with SUM(bytes) will count X TWICE.
A naive GROUP BY srcaddr, instance_id will show ONE IP mapped to TWO instances (WRONG).

The instance_id field = "which instance's ENI logged this record", NOT "who owns srcaddr".

TO FIX BOTH ISSUES:
1. BEST: Add WHERE flow_direction = 'egress'
   - Deduplicates bytes (only sender-side counted)
   - Makes instance_id = true owner of srcaddr
2. FALLBACK: If flow_direction unavailable:
   - Do NOT conclude one IP belongs to multiple instances
   - Cross-reference with EC2 DescribeInstances for authoritative IP-to-instance mapping
   - Note that byte totals may be ~2x actual

COMMON MISINTERPRETATION:
"Source IP 172.31.9.176 (instance i-aaa & i-bbb)" — THIS IS WRONG.
One IP belongs to ONE instance. The second instance_id appeared because its ENI
logged the ingress copy of the same packets. Filter by flow_direction='egress'
to get the correct single-instance attribution.
""",
            
            "inter_az_analysis_note": """
IMPORTANT: Inter-AZ Analysis Limitation

VPC Flow Logs only show the SOURCE AZ (az_id field), not destination AZ.
To identify Inter-AZ traffic, you need to:

1. Query EGRESS traffic by source AZ (use flow_direction='egress')
2. Manually correlate destination IPs with their AZ using AWS Console or CLI
3. Use CUR data to see actual Inter-AZ charges (ground truth for billing)

DO NOT use complex JOINs to find destination AZs - this causes data duplication
AND compounds the double-counting problem.

Simple approach:
- Filter flow_direction='egress' to get sender-side only
- Group by source az_id and destination IP
- Check which destination IPs are in different AZs
- Compare with CUR Inter-AZ line items for cost validation
"""
        }
        
        return json.dumps(info, indent=2)
    
    @tool
    def execute_vpc_flowlog_query(self, sql_query: str, account_id: str = None) -> str:
        """Execute an Athena query against VPC Flow Logs for network traffic analysis.
        
        IMPORTANT: Call get_vpc_flowlog_schema_info() FIRST to see available columns and query examples.
        
        Use this to identify which resources (IPs, ENIs, EC2 instances) are generating high data transfer.
        This helps pinpoint the source of inter-AZ, inter-region, or internet egress costs.
        
        When multiple member accounts are configured, you can target a specific account by
        providing its account_id. If omitted, the first configured member account's Athena service is used.
        
        ⚠️ DOUBLE-COUNTING WARNING:
        VPC Flow Logs record per ENI. Traffic between two instances generates records on BOTH ENIs.
        A naive SUM(bytes) will count the same transfer TWICE.
        - ALWAYS use flow_direction='egress' filter to count only sender-side records
        - If flow_direction is unavailable, note in results that totals may be ~2x actual
        - Cross-reference with CUR data transfer costs for ground truth
        
        BEST PRACTICES:
        1. Use partition columns for better performance
        2. Filter by time range using start/end timestamps
        3. Filter by action='ACCEPT' for successful traffic
        4. Filter by log_status='OK' for valid records
        5. Filter by flow_direction='egress' to avoid double-counting
        6. Aggregate by srcaddr, instance_id, or interface_id
        7. Convert bytes to GB: bytes / 1073741824.0
        8. Always LIMIT results (e.g., LIMIT 100)
        9. AVOID complex JOINs - they cause data duplication
        
        Args:
            sql_query: The SQL query to execute against VPC Flow Logs
            account_id: Optional AWS account ID to target a specific member account's VPC Flow Logs
            
        Returns:
            Query results formatted as text with statistics
        """
        if not self.vpc_flowlog_config.get('enabled', False):
            return "VPC Flow Logs not configured. Please configure vpc_flowlogs in config.yaml"
        
        # Route to the correct AthenaService based on account_id
        if account_id and account_id in self.member_athena_services:
            service = self.member_athena_services[account_id]
        elif account_id and account_id not in self.member_athena_services:
            available = ", ".join(self.member_athena_services.keys()) if self.member_athena_services else "none"
            return f"Account {account_id} not found in configured member accounts. Available accounts: {available}"
        else:
            # No account_id specified — use the first available VPC service
            if not self.member_athena_services:
                return "No VPC Flow Logs Athena services configured."
            service = next(iter(self.member_athena_services.values()))
        
        # Lazy resolution: ensure VPC table is resolved before use
        error = self._resolve_vpc_table(service)
        if error:
            return error

        # Execute the query directly - let Athena handle validation
        result = service.execute_query(sql_query)
        
        if result['status'] == 'success':
            table_output = service.format_results_as_table(result['rows'])
            stats = result['statistics']
            
            account_info = f" (account {account_id})" if account_id else ""
            return f"""VPC Flow Logs Query Results{account_info}:

{table_output}

Query Stats: {stats['data_scanned_mb']:.2f} MB scanned in {stats['execution_time_sec']:.2f}s

Next Steps:
- Correlate high-traffic resources with CUR cost data
- Check partition usage for better query performance
- Consider co-locating resources or optimizing network architecture"""
        
        elif result['status'] == 'failed':
            error_msg = result['error']
            
            # Provide helpful hints for common errors
            if 'column' in error_msg.lower() and 'not found' in error_msg.lower():
                return f"""Query failed - Column not found: {error_msg}

Tip: Call get_vpc_flowlog_schema_info() to see available columns and query examples."""
            
            return f"Query failed: {error_msg}"
        else:
            return f"Error executing query: {result['error']}"
    
    @tool
    def execute_multi_account_vpc_flowlog_query(self, sql_query: str) -> str:
        """Execute a VPC Flow Logs query across all configured member accounts in parallel.
        
        Results are aggregated from all member accounts. If some accounts fail,
        partial results are returned with error details for failed accounts.
        
        Args:
            sql_query: The SQL query to execute against VPC Flow Logs in each member account
            
        Returns:
            Aggregated results from all member accounts with per-account status
        """
        if not self.multi_account_executor:
            return "Multi-account execution not configured. Please configure accounts in config.yaml"
        
        account_ids = list(self.member_athena_services.keys())
        if not account_ids:
            return "No member accounts configured for VPC Flow Logs queries."
        
        # Lazy resolution: resolve VPC tables for all member services before querying
        for acct_id, service in self.member_athena_services.items():
            error = self._resolve_vpc_table(service)
            if error:
                return f"Failed to resolve VPC Flow Log table for account {acct_id}: {error}"

        def query_fn(account_id, session):
            service = self.member_athena_services[account_id]
            return service.execute_query(sql_query)
        
        result = self.multi_account_executor.execute_across_accounts(account_ids, query_fn)
        
        # Format output
        output_parts = []
        for r in result.succeeded:
            if r.data and r.data.get('status') == 'success':
                service = self.member_athena_services.get(r.account_id)
                table = service.format_results_as_table(r.data['rows']) if service else str(r.data['rows'])
                stats = r.data['statistics']
                output_parts.append(
                    f"Account {r.account_id}:\n{table}\n"
                    f"Stats: {stats['data_scanned_mb']:.2f} MB scanned in {stats['execution_time_sec']:.2f}s"
                )
            elif r.data:
                output_parts.append(f"Account {r.account_id}: {r.data.get('error', 'Unknown error')}")
        
        for r in result.failed:
            output_parts.append(f"Account {r.account_id}: FAILED - {r.error}")
        
        output_parts.append(f"\nSummary: {result.summary()}")
        return "\n\n".join(output_parts)
