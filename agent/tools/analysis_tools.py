"""Analysis and recommendation tools."""
from strands import tool
from .base_tool import BaseTool


class AnalysisTools(BaseTool):
    """Provides analysis and recommendation tools."""
    
    def get_tools(self):
        return [
            self.suggest_followup_questions,
            self.get_optimization_recommendations
        ]
    
    @tool
    def suggest_followup_questions(self, context: str) -> str:
        """Generate relevant follow-up questions based on the current analysis context.
        
        Args:
            context: Current analysis context (e.g., "high EC2 costs", "S3 storage trends")
            
        Returns:
            List of suggested follow-up questions
        """
        suggestions = {
            "ec2": [
                "Would you like me to identify the specific EC2 instances driving these costs?",
                "Should I analyze the instance types and their utilization patterns?",
                "Want to see which regions or availability zones have the highest EC2 spend?",
                "Shall I check for optimization opportunities like rightsizing or Spot instances?"
            ],
            "s3": [
                "Would you like me to identify the specific S3 buckets with highest costs?",
                "Should I analyze storage class distribution and potential savings?",
                "Want to see data transfer costs and patterns?",
                "Shall I check for lifecycle policy optimization opportunities?"
            ],
            "rds": [
                "Would you like me to identify specific RDS instances and their costs?",
                "Should I analyze database engine types and their spending?",
                "Want to see backup and snapshot storage costs?",
                "Shall I check for Reserved Instance opportunities?"
            ],
            "lambda": [
                "Would you like me to identify the top Lambda functions by cost?",
                "Should I analyze invocation patterns and duration costs?",
                "Want to see memory allocation efficiency?",
                "Shall I check for optimization opportunities?"
            ],
            "general": [
                "Would you like me to drill down into specific services for resource-level details?",
                "Should I analyze costs by account or region?",
                "Want to see usage type breakdowns for the top services?",
                "Shall I identify potential cost optimization opportunities?"
            ]
        }
        
        context_lower = context.lower()
        if "ec2" in context_lower or "instance" in context_lower:
            questions = suggestions["ec2"]
        elif "s3" in context_lower or "storage" in context_lower or "bucket" in context_lower:
            questions = suggestions["s3"]
        elif "rds" in context_lower or "database" in context_lower:
            questions = suggestions["rds"]
        elif "lambda" in context_lower or "function" in context_lower:
            questions = suggestions["lambda"]
        else:
            questions = suggestions["general"]
        
        result = "🤔 **Suggested Next Steps:**\n\n"
        for i, q in enumerate(questions, 1):
            result += f"{i}. {q}\n"
        
        return result
    
    @tool
    def get_optimization_recommendations(self, service: str, cost_data: str = "") -> str:
        """Get cost optimization recommendations for a specific service.
        
        This tool provides initial quick reference tips and prompts you to use
        AWS Knowledge MCP for detailed, up-to-date best practices from official AWS documentation.
        
        Args:
            service: AWS service name (e.g., EC2, S3, RDS, Lambda)
            cost_data: Optional context about current costs/usage
            
        Returns:
            Quick reference tips with guidance to search AWS documentation
        """
        quick_tips = {
            "ec2": [
                "Right-sizing with AWS Compute Optimizer",
                "Savings Plans and Reserved Instances",
                "Spot Instances for fault-tolerant workloads",
                "Instance Scheduler for non-production",
                "Graviton instances for better price/performance"
            ],
            "s3": [
                "S3 Intelligent-Tiering",
                "Lifecycle policies for storage class transitions",
                "Delete incomplete multipart uploads",
                "S3 Storage Lens for insights",
                "Optimize data transfer patterns"
            ],
            "rds": [
                "Right-sizing based on CloudWatch metrics",
                "Reserved Instances for production databases",
                "Aurora Serverless for variable workloads",
                "Storage optimization (gp3 vs gp2)",
                "Backup retention optimization"
            ],
            "lambda": [
                "Memory optimization with Lambda Power Tuning",
                "Code optimization to reduce duration",
                "ARM/Graviton2 architecture",
                "Minimize cold starts",
                "Optimize concurrency settings"
            ],
            "general": [
                "Comprehensive tagging strategy",
                "Cost Anomaly Detection",
                "Savings Plans",
                "Regular Cost Explorer reviews",
                "Budget alerts and monitoring"
            ]
        }
        
        service_key = service.lower().replace("amazon", "").strip()
        tips = quick_tips.get(service_key, quick_tips["general"])
        
        result = f"💡 **{service.upper()} Cost Optimization**\n\n"
        if cost_data:
            result += f"📊 Context: {cost_data}\n\n"
        
        result += "**Quick Reference:**\n"
        for i, tip in enumerate(tips, 1):
            result += f"{i}. {tip}\n"
        
        result += f"\n🔍 **For detailed, official AWS guidance:**\n"
        result += f"1. Use AWS Knowledge MCP to search documentation:\n"
        result += f"   - Search: '{service} cost optimization best practices'\n"
        result += f"   - Search: '{service} pricing optimization strategies'\n"
        result += f"2. Use cost_optimization tool for specific savings recommendations\n"
        result += f"3. Use compute_optimizer for performance-based rightsizing\n"
        result += f"\nWould you like me to search AWS documentation for detailed {service} cost optimization guidance?"
        
        return result
