"""Native billing and cost management tools routed through the payer account."""
import json
import logging
from typing import Any, Callable, Dict, List

from botocore.exceptions import ClientError
from strands import tool

from .base_tool import BaseTool
from agent.services.session_manager import SessionManager
from agent.services.account_registry import AccountRegistry

logger = logging.getLogger(__name__)


class BillingTools(BaseTool):
    """Provides billing and cost management tools routed through the payer account.

    All API calls are made via SessionManager.get_client(payer_account_id, service_name)
    to ensure org-wide billing data is retrieved from the payer account's assumed-role
    credentials in cross-account setups.
    """

    def __init__(self, session_manager: SessionManager, account_registry: AccountRegistry):
        self._session_manager = session_manager
        self._account_registry = account_registry

        payer = account_registry.get_payer_account()
        if payer is None:
            raise ValueError(
                "BillingTools requires a payer account. "
                "Configure an account with account_type 'payer' in config.yaml."
            )
        self._payer_account_id = payer.account_id

        self._clients: Dict[str, Any] = {}

    # --- Cost Explorer tools ---

    @tool
    def get_cost_and_usage(self, time_period: dict, granularity: str, metrics: list, filter: dict = None, group_by: list = None) -> str:
        """Get cost and usage data from Cost Explorer. Use for service-level cost analysis, trends, and breakdowns.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            granularity: DAILY, MONTHLY, or HOURLY
            metrics: List of metrics e.g. ["UnblendedCost", "UsageQuantity"]
            filter: Optional Cost Explorer filter expression
            group_by: Optional list of group-by dimensions e.g. [{"Type": "DIMENSION", "Key": "SERVICE"}]
        """
        return self._call_api("ce", "get_cost_and_usage",
            TimePeriod=time_period, Granularity=granularity, Metrics=metrics,
            Filter=filter, GroupBy=group_by)

    @tool
    def get_cost_forecast(self, time_period: dict, granularity: str, metric: str, filter: dict = None) -> str:
        """Get cost forecast from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"} - must be future dates
            granularity: DAILY or MONTHLY
            metric: Metric to forecast e.g. "UnblendedCost"
            filter: Optional Cost Explorer filter expression
        """
        return self._call_api("ce", "get_cost_forecast",
            TimePeriod=time_period, Granularity=granularity, Metric=metric, Filter=filter)

    @tool
    def get_usage_forecast(self, time_period: dict, granularity: str, metric: str, filter: dict = None) -> str:
        """Get usage forecast from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"} - must be future dates
            granularity: DAILY or MONTHLY
            metric: Usage metric to forecast e.g. "UsageQuantity"
            filter: Optional Cost Explorer filter expression
        """
        return self._call_api("ce", "get_usage_forecast",
            TimePeriod=time_period, Granularity=granularity, Metric=metric, Filter=filter)

    @tool
    def get_dimension_values(self, time_period: dict, dimension: str, context: str = None, filter: dict = None) -> str:
        """Get available dimension values from Cost Explorer (e.g., list all services, regions, accounts).

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            dimension: Dimension name e.g. SERVICE, LINKED_ACCOUNT, REGION, INSTANCE_TYPE
            context: Optional context for the dimension (e.g. "COST_AND_USAGE")
            filter: Optional Cost Explorer filter expression
        """
        return self._call_api("ce", "get_dimension_values",
            TimePeriod=time_period, Dimension=dimension, Context=context, Filter=filter)

    @tool
    def get_tags(self, time_period: dict, tag_key: str = None, filter: dict = None) -> str:
        """Get available tag keys and values from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            tag_key: Optional specific tag key to get values for
            filter: Optional Cost Explorer filter expression
        """
        return self._call_api("ce", "get_tags",
            TimePeriod=time_period, TagKey=tag_key, Filter=filter)

    @tool
    def get_cost_categories(self, time_period: dict, cost_category_name: str = None) -> str:
        """Get cost category names and values from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            cost_category_name: Optional specific cost category name
        """
        return self._call_api("ce", "get_cost_categories",
            TimePeriod=time_period, CostCategoryName=cost_category_name)

    @tool
    def get_anomalies(self, date_interval: dict, monitor_arn: str = None, max_results: int = None) -> str:
        """Get cost anomalies detected by Cost Explorer.

        Args:
            date_interval: {"StartDate": "YYYY-MM-DD", "EndDate": "YYYY-MM-DD"}
            monitor_arn: Optional anomaly monitor ARN to filter by
            max_results: Optional maximum number of results
        """
        return self._call_api("ce", "get_anomalies",
            DateInterval=date_interval, MonitorArn=monitor_arn, MaxResults=max_results)

    @tool
    def get_cost_and_usage_comparisons(self, time_period: dict, metrics: list, filter: dict = None, group_by: list = None) -> str:
        """Get cost and usage comparisons between time periods.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            metrics: List of metrics e.g. ["UnblendedCost"]
            filter: Optional Cost Explorer filter expression
            group_by: Optional list of group-by dimensions
        """
        return self._call_api("ce", "get_cost_and_usage_comparisons",
            TimePeriod=time_period, Metrics=metrics, Filter=filter, GroupBy=group_by)

    @tool
    def get_cost_comparison_drivers(self, time_period: dict, metric: str, filter: dict = None) -> str:
        """Get drivers of cost changes between time periods.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            metric: Metric to analyze e.g. "UnblendedCost"
            filter: Optional Cost Explorer filter expression
        """
        return self._call_api("ce", "get_cost_comparison_drivers",
            TimePeriod=time_period, Metric=metric, Filter=filter)

    # --- Reserved Instances tools ---

    @tool
    def get_reservation_purchase_recommendation(self, service: str, term_in_years: str, payment_option: str, lookback_period_in_days: str, account_scope: str = None) -> str:
        """Get Reserved Instance purchase recommendations from Cost Explorer.

        Args:
            service: AWS service e.g. "Amazon Elastic Compute Cloud - Compute"
            term_in_years: "ONE_YEAR" or "THREE_YEARS"
            payment_option: "NO_UPFRONT", "PARTIAL_UPFRONT", or "ALL_UPFRONT"
            lookback_period_in_days: "SEVEN_DAYS", "THIRTY_DAYS", or "SIXTY_DAYS"
            account_scope: Optional "LINKED" or "PAYER"
        """
        return self._call_api("ce", "get_reservation_purchase_recommendation",
            Service=service, TermInYears=term_in_years, PaymentOption=payment_option,
            LookbackPeriodInDays=lookback_period_in_days, AccountScope=account_scope)

    @tool
    def get_reservation_coverage(self, time_period: dict, granularity: str = None, filter: dict = None, group_by: list = None) -> str:
        """Get Reserved Instance coverage data from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            granularity: Optional DAILY or MONTHLY
            filter: Optional Cost Explorer filter expression
            group_by: Optional list of group-by dimensions
        """
        return self._call_api("ce", "get_reservation_coverage",
            TimePeriod=time_period, Granularity=granularity, Filter=filter, GroupBy=group_by)

    @tool
    def get_reservation_utilization(self, time_period: dict, granularity: str = None, filter: dict = None, group_by: list = None) -> str:
        """Get Reserved Instance utilization data from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            granularity: Optional DAILY or MONTHLY
            filter: Optional Cost Explorer filter expression
            group_by: Optional list of group-by dimensions
        """
        return self._call_api("ce", "get_reservation_utilization",
            TimePeriod=time_period, Granularity=granularity, Filter=filter, GroupBy=group_by)

    # --- Savings Plans tools ---

    @tool
    def get_savings_plans_purchase_recommendation(self, savings_plans_type: str, term_in_years: str, payment_option: str, lookback_period_in_days: str, account_scope: str = None) -> str:
        """Get Savings Plans purchase recommendations from Cost Explorer.

        Args:
            savings_plans_type: "COMPUTE_SP", "EC2_INSTANCE_SP", or "SAGEMAKER_SP"
            term_in_years: "ONE_YEAR" or "THREE_YEARS"
            payment_option: "NO_UPFRONT", "PARTIAL_UPFRONT", or "ALL_UPFRONT"
            lookback_period_in_days: "SEVEN_DAYS", "THIRTY_DAYS", or "SIXTY_DAYS"
            account_scope: Optional "LINKED" or "PAYER"
        """
        return self._call_api("ce", "get_savings_plans_purchase_recommendation_details",
            SavingsPlansType=savings_plans_type, TermInYears=term_in_years,
            PaymentOption=payment_option, LookbackPeriodInDays=lookback_period_in_days,
            AccountScope=account_scope)

    @tool
    def get_savings_plans_utilization(self, time_period: dict, granularity: str = None, filter: dict = None) -> str:
        """Get Savings Plans utilization data from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            granularity: Optional DAILY or MONTHLY
            filter: Optional Cost Explorer filter expression
        """
        return self._call_api("ce", "get_savings_plans_utilization_details",
            TimePeriod=time_period, Granularity=granularity, Filter=filter)

    @tool
    def get_savings_plans_coverage(self, time_period: dict, granularity: str = None, filter: dict = None, group_by: list = None) -> str:
        """Get Savings Plans coverage data from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            granularity: Optional DAILY or MONTHLY
            filter: Optional Cost Explorer filter expression
            group_by: Optional list of group-by dimensions
        """
        return self._call_api("ce", "get_savings_plans_coverage",
            TimePeriod=time_period, Granularity=granularity, Filter=filter, GroupBy=group_by)

    @tool
    def get_savings_plans_details(self, time_period: dict, filter: dict = None, max_results: int = None) -> str:
        """Get Savings Plans details from Cost Explorer.

        Args:
            time_period: {"Start": "YYYY-MM-DD", "End": "YYYY-MM-DD"}
            filter: Optional Cost Explorer filter expression
            max_results: Optional maximum number of results
        """
        return self._call_api("ce", "get_savings_plans_utilization_details",
            TimePeriod=time_period, Filter=filter, MaxResults=max_results)

    # --- Cost Optimization Hub tools ---

    @tool
    def get_recommendation(self, recommendation_id: str) -> str:
        """Get a single cost optimization recommendation from Cost Optimization Hub.

        Args:
            recommendation_id: The recommendation ID to retrieve
        """
        return self._call_api("cost-optimization-hub", "get_recommendation",
            recommendationId=recommendation_id)

    @tool
    def list_recommendations(self, filter: dict = None, max_results: int = None, next_token: str = None) -> str:
        """List cost optimization recommendations from Cost Optimization Hub.

        Args:
            filter: Optional filter criteria
            max_results: Optional maximum number of results
            next_token: Optional pagination token
        """
        return self._call_api("cost-optimization-hub", "list_recommendations",
            filter=filter, maxResults=max_results, nextToken=next_token)

    @tool
    def list_recommendation_summaries(self, group_by: str, filter: dict = None, max_results: int = None) -> str:
        """List recommendation summaries grouped by a dimension from Cost Optimization Hub.

        Args:
            group_by: Dimension to group by e.g. "Region", "ResourceType", "AccountId"
            filter: Optional filter criteria
            max_results: Optional maximum number of results
        """
        return self._call_api("cost-optimization-hub", "list_recommendation_summaries",
            groupBy=group_by, filter=filter, maxResults=max_results)

    # --- Compute Optimizer tools ---

    @tool
    def get_ec2_instance_recommendations(self, instance_arns: list = None, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get EC2 instance rightsizing recommendations from Compute Optimizer.

        Args:
            instance_arns: Optional list of instance ARNs to filter
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_ec2_instance_recommendations",
            instanceArns=instance_arns, filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_ebs_volume_recommendations(self, volume_arns: list = None, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get EBS volume rightsizing recommendations from Compute Optimizer.

        Args:
            volume_arns: Optional list of volume ARNs to filter
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_ebs_volume_recommendations",
            volumeArns=volume_arns, filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_lambda_function_recommendations(self, function_arns: list = None, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get Lambda function rightsizing recommendations from Compute Optimizer.

        Args:
            function_arns: Optional list of function ARNs to filter
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_lambda_function_recommendations",
            functionArns=function_arns, filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_auto_scaling_group_recommendations(self, auto_scaling_group_arns: list = None, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get Auto Scaling group rightsizing recommendations from Compute Optimizer.

        Args:
            auto_scaling_group_arns: Optional list of ASG ARNs to filter
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_auto_scaling_group_recommendations",
            autoScalingGroupArns=auto_scaling_group_arns, filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_ecs_service_recommendations(self, service_arns: list = None, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get ECS service rightsizing recommendations from Compute Optimizer.

        Args:
            service_arns: Optional list of ECS service ARNs to filter
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_ecs_service_recommendations",
            serviceArns=service_arns, filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_rds_db_instance_recommendations(self, instance_arns: list = None, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get RDS DB instance rightsizing recommendations from Compute Optimizer.

        Args:
            instance_arns: Optional list of RDS instance ARNs to filter
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_rds_db_instance_recommendations",
            instanceArns=instance_arns, filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_idle_recommendations(self, filters: list = None, account_ids: list = None, max_results: int = None) -> str:
        """Get idle resource recommendations from Compute Optimizer.

        Args:
            filters: Optional list of filter objects
            account_ids: Optional list of account IDs
            max_results: Optional maximum number of results
        """
        return self._call_api("compute-optimizer", "get_idle_recommendations",
            filters=filters, accountIds=account_ids, maxResults=max_results)

    @tool
    def get_enrollment_status(self) -> str:
        """Get Compute Optimizer enrollment status for the account."""
        return self._call_api("compute-optimizer", "get_enrollment_status")

    # --- Budgets and Free Tier tools ---

    @tool
    def describe_budgets(self, max_results: int = None) -> str:
        """Get budget configurations from AWS Budgets.

        Args:
            max_results: Optional maximum number of results
        """
        return self._call_api("budgets", "describe_budgets",
            AccountId=self._payer_account_id, MaxResults=max_results)

    @tool
    def get_free_tier_usage(self, filter: dict = None) -> str:
        """Get AWS Free Tier usage information.

        Args:
            filter: Optional filter expression
        """
        return self._call_api("freetier", "get_free_tier_usage",
            filter=filter)

    # --- Pricing tools ---

    @tool
    def describe_services(self, service_code: str = None, max_results: int = None) -> str:
        """List available AWS services and their attribute names from the Pricing API.

        Args:
            service_code: Optional service code to get details for a specific service
            max_results: Optional maximum number of results
        """
        return self._call_api("pricing", "describe_services",
            ServiceCode=service_code, MaxResults=max_results)

    @tool
    def get_attribute_values(self, service_code: str, attribute_name: str, max_results: int = None) -> str:
        """Get available attribute values for a service from the Pricing API.

        Args:
            service_code: AWS service code e.g. "AmazonEC2"
            attribute_name: Attribute name e.g. "instanceType", "location"
            max_results: Optional maximum number of results
        """
        return self._call_api("pricing", "get_attribute_values",
            ServiceCode=service_code, AttributeName=attribute_name, MaxResults=max_results)

    @tool
    def get_products(self, service_code: str, filters: list = None, max_results: int = None) -> str:
        """Get product pricing information from the Pricing API.

        Args:
            service_code: AWS service code e.g. "AmazonEC2"
            filters: Optional list of filter objects e.g. [{"Type": "TERM_MATCH", "Field": "instanceType", "Value": "m5.xlarge"}]
            max_results: Optional maximum number of results
        """
        return self._call_api("pricing", "get_products",
            ServiceCode=service_code, Filters=filters, MaxResults=max_results)

    # --- Pricing Calculator tools ---

    @tool
    def get_preferences(self) -> str:
        """Get Pricing Calculator preferences."""
        return self._call_api("bcm-pricing-calculator", "get_preferences")

    @tool
    def get_workload_estimate(self, workload_estimate_id: str) -> str:
        """Get a workload cost estimate from Pricing Calculator.

        Args:
            workload_estimate_id: The workload estimate ID
        """
        return self._call_api("bcm-pricing-calculator", "get_workload_estimate",
            workloadEstimateId=workload_estimate_id)

    @tool
    def list_workload_estimate_usage(self, workload_estimate_id: str, max_results: int = None) -> str:
        """List usage items for a workload estimate from Pricing Calculator.

        Args:
            workload_estimate_id: The workload estimate ID
            max_results: Optional maximum number of results
        """
        return self._call_api("bcm-pricing-calculator", "list_workload_estimate_usage",
            workloadEstimateId=workload_estimate_id, maxResults=max_results)

    @tool
    def list_workload_estimates(self, filter: dict = None, max_results: int = None) -> str:
        """List workload estimates from Pricing Calculator.

        Args:
            filter: Optional filter criteria
            max_results: Optional maximum number of results
        """
        return self._call_api("bcm-pricing-calculator", "list_workload_estimates",
            filter=filter, maxResults=max_results)

    # --- Billing Conductor tools ---

    @tool
    def list_billing_groups(self, max_results: int = None) -> str:
        """List billing groups from Billing Conductor.

        Args:
            max_results: Optional maximum number of results
        """
        return self._call_api("billingconductor", "list_billing_groups",
            MaxResults=max_results)

    @tool
    def list_billing_group_cost_reports(self, billing_group_arn: str = None, max_results: int = None) -> str:
        """List billing group cost reports from Billing Conductor.

        Args:
            billing_group_arn: Optional billing group ARN to filter
            max_results: Optional maximum number of results
        """
        return self._call_api("billingconductor", "list_billing_group_cost_reports",
            BillingGroupArn=billing_group_arn, MaxResults=max_results)

    @tool
    def get_billing_group_cost_report(self, billing_group_arn: str, billing_period_range: dict = None) -> str:
        """Get a billing group cost report from Billing Conductor.

        Args:
            billing_group_arn: The billing group ARN
            billing_period_range: Optional billing period range
        """
        return self._call_api("billingconductor", "get_billing_group_cost_report",
            Arn=billing_group_arn, BillingPeriodRange=billing_period_range)

    @tool
    def list_account_associations(self, billing_group_arn: str = None, max_results: int = None) -> str:
        """List account associations from Billing Conductor.

        Args:
            billing_group_arn: Optional billing group ARN to filter
            max_results: Optional maximum number of results
        """
        return self._call_api("billingconductor", "list_account_associations",
            BillingGroupArn=billing_group_arn, MaxResults=max_results)

    @tool
    def list_pricing_plans(self, max_results: int = None) -> str:
        """List pricing plans from Billing Conductor.

        Args:
            max_results: Optional maximum number of results
        """
        return self._call_api("billingconductor", "list_pricing_plans",
            MaxResults=max_results)

    @tool
    def list_pricing_rules(self, max_results: int = None) -> str:
        """List pricing rules from Billing Conductor.

        Args:
            max_results: Optional maximum number of results
        """
        return self._call_api("billingconductor", "list_pricing_rules",
            MaxResults=max_results)

    @tool
    def list_custom_line_items(self, billing_period_range: dict = None, max_results: int = None) -> str:
        """List custom line items from Billing Conductor.

        Args:
            billing_period_range: Optional billing period range
            max_results: Optional maximum number of results
        """
        return self._call_api("billingconductor", "list_custom_line_items",
            BillingPeriodRange=billing_period_range, MaxResults=max_results)

    def get_tools(self) -> List[Callable]:
        """Return all billing tool functions."""
        tools: List[Callable] = [
            self.get_cost_and_usage,
            self.get_cost_forecast,
            self.get_usage_forecast,
            self.get_dimension_values,
            self.get_tags,
            self.get_cost_categories,
            self.get_anomalies,
            self.get_cost_and_usage_comparisons,
            self.get_cost_comparison_drivers,
            self.get_reservation_purchase_recommendation,
            self.get_reservation_coverage,
            self.get_reservation_utilization,
            self.get_savings_plans_purchase_recommendation,
            self.get_savings_plans_utilization,
            self.get_savings_plans_coverage,
            self.get_savings_plans_details,
            self.get_recommendation,
            self.list_recommendations,
            self.list_recommendation_summaries,
            self.get_ec2_instance_recommendations,
            self.get_ebs_volume_recommendations,
            self.get_lambda_function_recommendations,
            self.get_auto_scaling_group_recommendations,
            self.get_ecs_service_recommendations,
            self.get_rds_db_instance_recommendations,
            self.get_idle_recommendations,
            self.get_enrollment_status,
            self.describe_budgets,
            self.get_free_tier_usage,
            self.describe_services,
            self.get_attribute_values,
            self.get_products,
            self.get_preferences,
            self.get_workload_estimate,
            self.list_workload_estimate_usage,
            self.list_workload_estimates,
            self.list_billing_groups,
            self.list_billing_group_cost_reports,
            self.get_billing_group_cost_report,
            self.list_account_associations,
            self.list_pricing_plans,
            self.list_pricing_rules,
            self.list_custom_line_items,
        ]
        logger.info("BillingTools: registered %d billing tools", len(tools))
        return tools

    def _get_client(self, service_name: str):
        """Get or create a cached boto3 client for the given service via the payer account."""
        if service_name not in self._clients:
            self._clients[service_name] = self._session_manager.get_client(
                self._payer_account_id, service_name
            )
        return self._clients[service_name]

    def _call_api(self, service_name: str, method_name: str, **kwargs) -> str:
        """Call a billing API method and return JSON. Handles errors uniformly."""
        try:
            client = self._get_client(service_name)
            params = {k: v for k, v in kwargs.items() if v is not None}
            response = getattr(client, method_name)(**params)
            response.pop("ResponseMetadata", None)
            return json.dumps(response, default=str)
        except ClientError as e:
            return json.dumps({
                "error": True,
                "api": method_name,
                "code": e.response["Error"]["Code"],
                "message": e.response["Error"]["Message"],
            })
        except Exception as e:
            return json.dumps({
                "error": True,
                "api": method_name,
                "exception_type": type(e).__name__,
                "message": str(e),
            })
