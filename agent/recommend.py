"""Run the recommendation workflow locally without starting the model runtime.

python -m agent.recommend --config agent/config.yaml --request request.json
JSON is printed to stdout; diagnostics use stderr. AWS calls are read-only.
"""
import argparse
import json
import logging

from agent.services.config_service import ConfigService
from agent.services.session_manager import SessionManager
from agent.services.recommendation_service import RecommendationService
from agent.tools.alternatives_tools import _dump, is_alternatives_enabled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--request", required=True, help="JSON request, with optional scoped workload_inputs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    config = ConfigService(args.config)
    if not is_alternatives_enabled(config):
        parser.error("Set alternatives.enabled: true in the chosen configuration.")
    with open(args.request, encoding="utf-8") as handle:
        request = json.load(handle)
    if not isinstance(request, dict):
        parser.error("Request must be a JSON object.")
    registry = config.account_registry
    service = RecommendationService(config, SessionManager(registry), registry)
    if "alternative_id" in request:
        allowed = {"alternative_id", "account_id", "regions", "workload_inputs", "resource_id"}
        method = service.evaluate
    else:
        allowed = {"start_date", "end_date", "account_id", "regions", "workload_inputs"}
        method = service.run
    if set(request) - allowed:
        parser.error("Unknown request keys: " + ", ".join(sorted(set(request) - allowed)))
    print(_dump(method(**request)))


if __name__ == "__main__":
    main()
