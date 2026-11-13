"""Strands tools for the complete read-only recommendation workflow."""
import logging
from typing import Optional

from strands import tool

from agent.services.recommendation_service import RecommendationService
from agent.tools.alternatives_tools import _dump, _error, _exception_error, is_alternatives_enabled

logger = logging.getLogger(__name__)


class RecommendationTools:
    def __init__(self, service):
        self.service = service

    def get_tools(self):
        return [self.analyze_cost_recommendations, self.evaluate_service_alternative]

    @tool
    def analyze_cost_recommendations(self, start_date: Optional[str] = None, end_date: Optional[str] = None,
                                     account_id: Optional[str] = None, regions: Optional[list[str]] = None,
                                     workload_inputs: Optional[list[dict]] = None) -> str:
        """Check cost thresholds, validate unused alternatives, estimate equivalent workload costs and report.

        On-demand, read-only. No AWS resource changes and no email/SNS notification.
        Dates are inclusive start / EXCLUSIVE end; pass both or neither (month-to-date).
        Only configured accounts are inspected; omitted account_id means all configured accounts.
        Regions default to each account's configured region; at most ten may be provided.
        workload_inputs are resource-scoped user-provided facts, measurements and pricing inputs
        as documented in docs/recommendations.md. NEVER invent or fill missing inputs with guesses.
        Missing evidence is returned as needs_information; it is never a migration recommendation.
        """
        try:
            return _dump(self.service.run(start_date, end_date, account_id, regions, workload_inputs))
        except ValueError as exc:
            return _dump(_error("analyze_cost_recommendations", "InvalidInput", str(exc)))
        except Exception as exc:
            return _dump(_exception_error("analyze_cost_recommendations", exc))

    @tool
    def evaluate_service_alternative(self, alternative_id: str, account_id: Optional[str] = None,
                                     regions: Optional[list[str]] = None, resource_id: Optional[str] = None,
                                     workload_inputs: Optional[list[dict]] = None) -> str:
        """Evaluate a catalog alternative for a workload without requiring a cost threshold crossing.

        Fetches fresh AWS evidence, executes catalog rules, and only prices compatible workloads.
        Use resource_id to inspect a specific resource. Alternative IDs come from the catalog/high-cost screen.
        workload_inputs must contain real user statements or sourced measurements; never fabricate them.
        Returns scoped decisions, matched rules, missing inputs, pricing and explanatory recommendations.
        Target usage is checked only in the explicitly inspected accounts and regions.
        """
        try:
            return _dump(self.service.evaluate(alternative_id, account_id, regions, workload_inputs, resource_id))
        except ValueError as exc:
            return _dump(_error("evaluate_service_alternative", "InvalidInput", str(exc)))
        except Exception as exc:
            return _dump(_exception_error("evaluate_service_alternative", exc))


def get_recommendation_tools(config, session_manager, account_registry):
    if not is_alternatives_enabled(config):
        return []
    try:
        return RecommendationTools(RecommendationService(config, session_manager, account_registry)).get_tools()
    except Exception:
        logger.exception("Recommendation tool initialization failed")
        return []
