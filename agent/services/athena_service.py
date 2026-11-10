"""Athena service for executing CUR queries."""
import boto3
import logging
import re
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger("AthenaService")

# T8 Mitigation: Only allow read-only SQL operations
_ALLOWED_SQL_PREFIXES = ('select', 'show', 'describe', 'with', 'explain')
_BLOCKED_SQL_PATTERNS = [
    r'\b(drop|delete|insert|update|alter|create|replace|truncate|merge)\b',
    r'\b(grant|revoke)\b',
    r'\binto\s+outfile\b',
    r'\bunion\s+all\s+select\b.*\bfrom\s+information_schema\b',
]


class AthenaService:
    """Handles Athena query execution against CUR or VPC flowlog data."""
    
    def __init__(self, region: str, database: str, table: Optional[str] = None, session: boto3.Session = None):
        effective_session = session or boto3.Session(region_name=region)
        self.athena_client = effective_session.client('athena', region_name=region)
        self.s3_client = effective_session.client('s3', region_name=region)
        self.database = database
        self.table = table
        self._table_schema_cache = {}  # Cache for table schemas
        self._partition_cache = {}  # Cache for partition info
    
    def execute_query(self, sql_query: str, max_wait: int = 60) -> Dict[str, Any]:
        """Execute an Athena query and return results.
        
        Args:
            sql_query: SQL query to execute
            max_wait: Maximum wait time in seconds
            
        Returns:
            Dictionary with status, results, and statistics
        """
        # T8 Mitigation: Validate SQL is read-only (SELECT/SHOW/DESCRIBE only)
        normalized = sql_query.strip().lower()
        if not normalized.startswith(_ALLOWED_SQL_PREFIXES):
            logger.warning(f"Blocked non-read SQL operation: {normalized[:50]}...")
            return {
                'status': 'error',
                'error': 'Only SELECT, SHOW, DESCRIBE, WITH, and EXPLAIN queries are allowed',
                'rows': [],
            }
        
        for pattern in _BLOCKED_SQL_PATTERNS:
            if re.search(pattern, normalized, re.IGNORECASE):
                logger.warning(f"Blocked dangerous SQL pattern: {pattern}")
                return {
                    'status': 'error',
                    'error': 'Query contains blocked SQL operations',
                    'rows': [],
                }
        
        # T18 Mitigation: Limit query length to prevent resource-intensive queries
        MAX_QUERY_LENGTH = 10000
        if len(sql_query) > MAX_QUERY_LENGTH:
            return {
                'status': 'error',
                'error': f'Query exceeds maximum length of {MAX_QUERY_LENGTH} characters',
                'rows': [],
            }
        
        try:
            # Start query execution
            response = self.athena_client.start_query_execution(
                QueryString=sql_query,
                QueryExecutionContext={'Database': self.database},
            )
            
            query_id = response['QueryExecutionId']
            
            # Wait for completion
            wait_time = 0
            while wait_time < max_wait:
                result = self.athena_client.get_query_execution(QueryExecutionId=query_id)
                status = result['QueryExecution']['Status']['State']
                
                if status in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
                    break
                time.sleep(2)  # nosemgrep: arbitrary-sleep  # Polling interval for Athena query status
                wait_time += 2
            
            if status == 'SUCCEEDED':
                # Get results
                results = self.athena_client.get_query_results(
                    QueryExecutionId=query_id,
                    MaxResults=100
                )
                
                stats = result['QueryExecution']['Statistics']
                
                return {
                    'status': 'success',
                    'query_id': query_id,
                    'rows': results['ResultSet']['Rows'],
                    'statistics': {
                        'data_scanned_mb': stats.get('DataScannedInBytes', 0) / (1024 * 1024),
                        'execution_time_sec': stats.get('EngineExecutionTimeInMillis', 0) / 1000
                    }
                }
            else:
                error = result['QueryExecution']['Status'].get('StateChangeReason', 'Unknown error')
                return {
                    'status': 'failed',
                    'query_id': query_id,
                    'error': error
                }
                
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def format_results_as_table(self, rows: list) -> str:
        """Format query results as a text table."""
        if not rows:
            return "No results found."
        
        output = []
        for i, row in enumerate(rows):
            row_data = [col.get('VarCharValue', 'NULL') for col in row['Data']]
            if i == 0:
                output.append("| " + " | ".join(row_data) + " |")
                output.append("|" + "|".join(["-" * (len(d) + 2) for d in row_data]) + "|")
            else:
                output.append("| " + " | ".join(row_data) + " |")
        
        return "\n".join(output)
    
    def get_table_schema(self, database: str = None, table: str = None) -> Dict[str, Any]:
        """Get table schema information including columns and data types.
        
        Args:
            database: Database name (defaults to self.database)
            table: Table name (defaults to self.table)
            
        Returns:
            Dictionary with columns, data types, and partition info
        """
        db = database or self.database
        tbl = table or self.table
        cache_key = f"{db}.{tbl}"
        
        # Check cache first
        if cache_key in self._table_schema_cache:
            return self._table_schema_cache[cache_key]
        
        try:
            # Try SHOW COLUMNS first (works better with Glue catalog)
            query = f"SHOW COLUMNS FROM {db}.{tbl}"
            result = self.execute_query(query, max_wait=30)
            
            if result['status'] == 'success' and len(result['rows']) > 1:
                columns = []
                for row in result['rows'][1:]:  # Skip header
                    col_data = [col.get('VarCharValue', '') for col in row['Data']]
                    if len(col_data) >= 1 and col_data[0]:
                        col_name = col_data[0]
                        # SHOW COLUMNS doesn't return type, so we'll get it separately
                        columns.append({'name': col_name, 'type': 'unknown'})
                
                if columns:
                    schema_info = {
                        'status': 'success',
                        'database': db,
                        'table': tbl,
                        'columns': columns,
                        'partitions': [],  # Will be detected separately
                        'column_names': [c['name'] for c in columns],
                        'partition_names': []
                    }
                    self._table_schema_cache[cache_key] = schema_info
                    return schema_info
            
            # Fallback to DESCRIBE if SHOW COLUMNS fails
            query = f"DESCRIBE {db}.{tbl}"
            result = self.execute_query(query, max_wait=30)
            
            if result['status'] != 'success':
                return {
                    'status': 'error',
                    'error': f"Failed to describe table: {result.get('error', 'Unknown error')}"
                }
            
            columns = []
            partitions = []
            
            for row in result['rows'][1:]:  # Skip header row
                col_data = [col.get('VarCharValue', '') for col in row['Data']]
                if len(col_data) >= 2:
                    col_name = col_data[0]
                    col_type = col_data[1]
                    
                    # Check if it's a partition column
                    if col_name and col_name != '':
                        # Partition columns often have comment or appear after blank line
                        is_partition = False
                        if len(col_data) > 2:
                            comment = col_data[2].lower() if col_data[2] else ''
                            is_partition = 'partition' in comment
                        
                        # Also check for common partition naming patterns
                        if col_name.startswith('partition_') or col_name in ['year', 'month', 'day', 'date', 'dt']:
                            is_partition = True
                        
                        if is_partition:
                            partitions.append({'name': col_name, 'type': col_type})
                        else:
                            columns.append({'name': col_name, 'type': col_type})
            
            schema_info = {
                'status': 'success',
                'database': db,
                'table': tbl,
                'columns': columns,
                'partitions': partitions,
                'column_names': [c['name'] for c in columns],
                'partition_names': [p['name'] for p in partitions]
            }
            
            # Cache the result
            self._table_schema_cache[cache_key] = schema_info
            return schema_info
            
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def get_partition_info(self, database: str = None, table: str = None) -> Dict[str, Any]:
        """Get partition information for a table.
        
        Args:
            database: Database name (defaults to self.database)
            table: Table name (defaults to self.table)
            
        Returns:
            Dictionary with partition structure and sample values
        """
        db = database or self.database
        tbl = table or self.table
        cache_key = f"{db}.{tbl}"
        
        # Check cache first
        if cache_key in self._partition_cache:
            return self._partition_cache[cache_key]
        
        try:
            # First get schema to identify partition columns
            schema = self.get_table_schema(db, tbl)
            if schema['status'] != 'success':
                return schema
            
            partition_cols = schema.get('partition_names', [])
            
            if not partition_cols:
                return {
                    'status': 'success',
                    'database': db,
                    'table': tbl,
                    'has_partitions': False,
                    'message': 'Table is not partitioned'
                }
            
            # Query to get sample partition values
            partition_select = ', '.join(partition_cols)
            query = f"""
            SELECT DISTINCT {partition_select}
            FROM {db}.{tbl}
            ORDER BY {partition_cols[0]} DESC
            LIMIT 10
            """
            
            result = self.execute_query(query, max_wait=30)
            
            if result['status'] != 'success':
                return {
                    'status': 'error',
                    'error': f"Failed to query partitions: {result.get('error', 'Unknown error')}"
                }
            
            # Parse partition values
            sample_partitions = []
            if len(result['rows']) > 1:  # Skip header
                for row in result['rows'][1:]:
                    partition_values = [col.get('VarCharValue', '') for col in row['Data']]
                    sample_partitions.append(dict(zip(partition_cols, partition_values)))
            
            partition_info = {
                'status': 'success',
                'database': db,
                'table': tbl,
                'has_partitions': True,
                'partition_columns': partition_cols,
                'sample_partitions': sample_partitions,
                'partition_structure': self._detect_partition_structure(partition_cols)
            }
            
            # Cache the result
            self._partition_cache[cache_key] = partition_info
            return partition_info
            
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def _detect_partition_structure(self, partition_cols: List[str]) -> str:
        """Detect the partition structure type based on column names."""
        col_names_lower = [c.lower() for c in partition_cols]
        
        if 'year' in col_names_lower and 'month' in col_names_lower and 'day' in col_names_lower:
            return 'year/month/day'
        elif 'year' in col_names_lower and 'month' in col_names_lower:
            return 'year/month'
        elif 'date' in col_names_lower:
            return 'date'
        elif any('period' in c for c in col_names_lower):
            return 'billing_period'
        else:
            return 'custom'
    
    def validate_columns_exist(self, columns: List[str], database: str = None, table: str = None) -> Dict[str, Any]:
        """Validate that specified columns exist in the table.
        
        Args:
            columns: List of column names to validate
            database: Database name (defaults to self.database)
            table: Table name (defaults to self.table)
            
        Returns:
            Dictionary with validation results
        """
        schema = self.get_table_schema(database, table)
        
        if schema['status'] != 'success':
            return schema
        
        available_columns = set(schema['column_names'])
        requested_columns = set(columns)
        
        missing_columns = requested_columns - available_columns
        valid_columns = requested_columns & available_columns
        
        return {
            'status': 'success',
            'all_valid': len(missing_columns) == 0,
            'valid_columns': list(valid_columns),
            'missing_columns': list(missing_columns),
            'available_columns': schema['column_names']
        }
