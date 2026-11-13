"""Local FastAPI interface to the existing read-only AWS recommendation service.

Start from the project root:
python -m uvicorn agent.api:app --host 127.0.0.1 --port 8000
Open http://127.0.0.1:8000/docs. Uses AWS_PROFILE and agent/config.yaml.
"""
import json
import logging
import os
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.config_service import ConfigService
from agent.services.cost_analysis_workflow import AnalysisStore, CostAnalysisWorkflow, SavedReportReader, WorkflowError
from agent.services.report_views import cost_view, recommendation_view
from agent.services.recommendation_service import RecommendationService
from agent.services.session_manager import SessionManager
from agent.tools.alternatives_tools import _dump, is_alternatives_enabled

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AWS Cost and Alternative Service Recommendations",
    version="1.2.0",
    openapi_tags=[
        {"name": "Main workflow", "description": "Check health, create and view cost analysis, then create and view recommendations."},
        {"name": "Supported alternatives", "description": "Optional list of configured candidate mappings."},
    ],
    description=("Use Try it out to read your AWS cost and workload data. "
                 "First POST /cost-analysis with dates, then POST /recommendations with its analysis_id. "
                 "Use the GET routes to retrieve saved results without running AWS analysis again. "
                 "Reports are concise by default; include_details=true shows technical evidence. "
                 "Missing evidence is reported, never guessed. No AWS resources are changed."),
)


class WorkloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$",
                                    description="Omit to inspect all accounts in your local configuration.")
    regions: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, min_length=1, max_length=10, description="Omit to use configured account regions.")
    workload_inputs: list[dict[str, Any]] = Field(default_factory=list, max_length=200,
        description="Leave empty for the first test. Later supply verified workload facts using docs/recommendations.md.")


class ThresholdRequest(WorkloadRequest):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {"regions": ["us-east-1"], "workload_inputs": []}})
    start_date: date | None = Field(default=None, description="Inclusive start. Omit both dates for month-to-date.")
    end_date: date | None = Field(default=None, description="Exclusive end; must be after start_date.")

    @model_validator(mode="after")
    def check_period(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both dates or neither.")
        if self.start_date is not None and self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date (exclusive end).")
        return self


class CostAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {
        "start_date": "2026-11-01", "end_date": "2026-11-13", "top_n": 5}})
    start_date: date | None = Field(default=None, description="Inclusive start. Omit both dates for month-to-date.")
    end_date: date | None = Field(default=None, description="Exclusive end, no later than today. Actual costs only.")
    top_n: int = Field(default=5, ge=1, le=50, strict=True)

    @model_validator(mode="after")
    def check_period(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both dates or neither.")
        if self.start_date is not None and self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date (exclusive end).")
        return self


class RecommendationRequest(WorkloadRequest):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {
        "analysis_id": "PASTE_ANALYSIS_ID_FROM_COST_ANALYSIS", "service": "Amazon EC2",
        "regions": ["us-east-1"], "workload_inputs": []}})
    analysis_id: str = Field(min_length=1, max_length=128, description="Copy analysis_id from POST /cost-analysis.")
    service: str | None = Field(default=None, min_length=1, max_length=300,
        description="A returned service or service_key. Omit to select the highest-cost service.")
    resource_id: str | None = Field(default=None, min_length=1, max_length=2048)


class AlternativeRequest(WorkloadRequest):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {
        "alternative_id": "ec2-short-event-driven-to-lambda", "regions": ["us-east-1"], "workload_inputs": []}})
    alternative_id: str = Field(min_length=1, max_length=200, description="An approved ID from GET /alternatives.")
    resource_id: str | None = Field(default=None, min_length=1, max_length=2048,
                                     description="Optional instance/function/database/NAT identifier.")


def _build_service() -> RecommendationService:
    # Local server configuration only: callers cannot choose config files or AWS credentials.
    # Fresh per request so private config edits take effect without restarting the server.
    try:
        config = ConfigService(os.environ.get("COST_ANALYZER_CONFIG", "agent/config.yaml"))
        registry = config.account_registry
        return RecommendationService(config, SessionManager(registry), registry)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Local recommendation configuration could not be loaded")
        raise HTTPException(status_code=503, detail="Check your local configuration and AWS_PROFILE login; server setup failed.") from None


def get_service() -> RecommendationService:
    service = _build_service()
    if not is_alternatives_enabled(service.config):
        raise HTTPException(status_code=503, detail="Set alternatives.enabled: true in the local configuration.")
    return service


def get_cost_workflow() -> CostAnalysisWorkflow:
    return CostAnalysisWorkflow(_build_service(), AnalysisStore(os.environ.get("COST_ANALYZER_ANALYSIS_DB", ".cost_analyses.sqlite3")))


def get_saved_reports() -> SavedReportReader:
    try:
        config = ConfigService(os.environ.get("COST_ANALYZER_CONFIG", "agent/config.yaml"))
        return SavedReportReader(AnalysisStore(os.environ.get("COST_ANALYZER_ANALYSIS_DB", ".cost_analyses.sqlite3")), config.account_registry)
    except Exception:
        logger.exception("Saved-report configuration could not be loaded")
        raise HTTPException(status_code=503, detail="Check the local report configuration.") from None


Service = Annotated[RecommendationService, Depends(get_service)]
Workflow = Annotated[CostAnalysisWorkflow, Depends(get_cost_workflow)]
SavedReports = Annotated[SavedReportReader, Depends(get_saved_reports)]
DetailFlag = Annotated[bool, Query(description="Show technical evidence and settings. Leave false for a concise report.")]


def _respond(method, request=None, renderer=None, **params):
    try:
        result = method(**(request.model_dump(mode="json") if request is not None else params))
        if renderer:
            result = renderer(result)
        # Preserve evidence and sanitize sensitive strings using the existing tool contract.
        return json.loads(_dump(result))
    except WorkflowError as exc:
        safe = json.loads(_dump({"code": exc.code, "message": str(exc)}))
        raise HTTPException(status_code=exc.status_code, detail=safe) from None
    except ValueError as exc:
        safe = json.loads(_dump({"message": str(exc)}))
        raise HTTPException(status_code=422, detail=safe["message"]) from None
    except Exception:
        logger.exception("Recommendation request failed")
        raise HTTPException(status_code=500, detail="Recommendation execution failed. Check the server log; no AWS changes were made.") from None


@app.get("/health", tags=["Main workflow"], summary="1. Check the API server")
def health():
    """Check the API server only. This makes no AWS calls and does not verify AWS access."""
    return {"status": "ok", "aws_access_checked": False, "aws_resources_changed": False}


@app.post("/cost-analysis", tags=["Main workflow"], summary="2. Create cost analysis")
def cost_analysis(request: CostAnalysisRequest, workflow: Workflow, include_details: DetailFlag = False):
    """Rank actual costs before credits; show credits and net costs separately and save the report."""
    return _respond(workflow.analyze, request, renderer=lambda r: cost_view(r, include_details))


@app.get("/cost-analysis/{analysis_id}", tags=["Main workflow"], summary="3. View saved cost analysis")
def saved_cost_analysis(analysis_id: str, saved: SavedReports, include_details: DetailFlag = False):
    """Retrieve a saved cost report. No AWS sessions, API calls or cost refresh are performed."""
    return _respond(saved.analysis, analysis_id=analysis_id, renderer=lambda r: cost_view(r, include_details))


@app.post("/recommendations", tags=["Main workflow"], summary="4. Create recommendations")
def recommendations(request: RecommendationRequest, workflow: Workflow, include_details: DetailFlag = False):
    """Evaluate and save alternatives using an analysis ID; return a recommendation ID."""
    if not is_alternatives_enabled(workflow.engine.config):
        raise HTTPException(status_code=503, detail="Set alternatives.enabled: true in the local configuration.")
    return _respond(workflow.recommend, request, renderer=lambda r: recommendation_view(r, include_details))


@app.get("/recommendations/{recommendation_id}", tags=["Main workflow"], summary="5. View saved recommendations")
def saved_recommendation(recommendation_id: str, saved: SavedReports, include_details: DetailFlag = False):
    """Retrieve the saved recommendation, without reevaluating AWS resources or pricing."""
    return _respond(saved.recommendation, recommendation_id=recommendation_id, renderer=lambda r: recommendation_view(r, include_details))


@app.get("/alternatives", tags=["Supported alternatives"])
def list_alternatives():
    """List all five configured candidate mappings without contacting AWS."""
    return {"alternatives": [{"alternative_id": e.id, "source": e.source_name, "alternative": e.alternative_name}
                             for e in AlternativesCatalog.load().approved_entries()]}


@app.post("/threshold-recommendations", include_in_schema=False)
def threshold_recommendations(request: ThresholdRequest, service: Service):
    """Check live AWS service spending; evaluate alternatives only when thresholds are crossed."""
    return _respond(service.run, request)


@app.post("/alternatives/evaluate", include_in_schema=False)
def evaluate_alternative(request: AlternativeRequest, service: Service):
    """Inspect live workload evidence for an alternative without requiring a threshold breach."""
    return _respond(service.evaluate, request)
