"""HTTP/Swagger tests using fake services; no AWS calls or credentials."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent.api import app, get_service


@pytest.fixture
def api():
    service = Mock()
    service.run.return_value = {"status": "evaluated", "recommendations": 0, "aws_resources_changed": False}
    service.evaluate.return_value = {"status": "evaluated", "reports": [{"status": "needs_information"}], "aws_resources_changed": False}
    app.dependency_overrides[get_service] = lambda: service
    with TestClient(app) as client:
        yield client, service
    app.dependency_overrides.clear()


def test_swagger_and_health_work_without_config_or_aws_credentials():
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/health").json()["aws_access_checked"] is False
        spec = client.get("/openapi.json").json()
        assert "/threshold-recommendations" not in spec["paths"]
        assert "/alternatives/evaluate" not in spec["paths"]
        assert [p for p, methods in spec["paths"].items() if any(
            op.get("tags") == ["Main workflow"] for op in methods.values())] == [
                "/health", "/cost-analysis", "/cost-analysis/{analysis_id}", "/recommendations", "/recommendations/{recommendation_id}"]
        result = client.get("/alternatives").json()
        assert len(result["alternatives"]) == 5


def test_default_request_checks_threshold_workflow(api):
    client, service = api
    response = client.post("/threshold-recommendations", json={"regions": ["us-east-1"]})
    assert response.status_code == 200
    assert response.json()["recommendations"] == 0
    service.run.assert_called_once_with(account_id=None, regions=["us-east-1"], workload_inputs=[], start_date=None, end_date=None)
    service.evaluate.assert_not_called()


def test_specific_alternative_bypasses_thresholds_and_preserves_missing_evidence(api):
    client, service = api
    response = client.post("/alternatives/evaluate", json={"alternative_id": "ec2-short-event-driven-to-lambda", "regions": ["us-east-1"]})
    assert response.status_code == 200
    assert response.json()["reports"][0]["status"] == "needs_information"
    service.evaluate.assert_called_once_with(alternative_id="ec2-short-event-driven-to-lambda", account_id=None,
                                             regions=["us-east-1"], resource_id=None, workload_inputs=[])
    service.run.assert_not_called()


def test_dates_are_serialized_for_existing_cost_service(api):
    client, service = api
    assert client.post("/threshold-recommendations", json={"start_date": "2026-11-01", "end_date": "2026-11-13"}).status_code == 200
    assert service.run.call_args.kwargs["start_date"] == "2026-11-01"


@pytest.mark.parametrize("path,body", [("/threshold-recommendations", {"start_date": "2026-11-01"}),
    ("/threshold-recommendations", {"start_date": "2026-11-13", "end_date": "2026-11-01"}),
    ("/threshold-recommendations", {"account_id": "<PAYER_ACCOUNT_ID>"}),
    ("/threshold-recommendations", {"regions": []}), ("/threshold-recommendations", {"regions": ["us-east-1"]*11}),
    ("/threshold-recommendations", {"config_path": "another-account.yaml"}),
    ("/alternatives/evaluate", {})])
def test_bad_inputs_are_rejected_before_service_calls(api, path, body):
    client, service = api
    assert client.post(path, json=body).status_code == 422
    service.run.assert_not_called()
    service.evaluate.assert_not_called()


def test_unknown_or_unconfigured_resource_scope_becomes_http_422(api):
    client, service = api
    service.evaluate.side_effect = ValueError("Unknown or unapproved alternative_id.")
    response = client.post("/alternatives/evaluate", json={"alternative_id": "not-supported"})
    assert response.status_code == 422
    assert "unapproved" in response.json()["detail"]


def test_private_exception_details_are_not_returned_to_http_callers(api):
    client, service = api
    service.run.side_effect = RuntimeError("private upstream diagnostic")
    response = client.post("/threshold-recommendations", json={})
    assert response.status_code == 500
    assert "private upstream" not in response.text
