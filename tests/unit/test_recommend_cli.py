"""Exercise actual CLI request loading with Windows and plain UTF-8 files."""
import json
import sys
from unittest.mock import Mock

import pytest

from agent import recommend


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_reads_requests_with_or_without_windows_bom(tmp_path, monkeypatch, capsys, encoding, explicit):
    request = {"regions": ["us-east-1"], "workload_inputs": []}
    if explicit:
        request["alternative_id"] = "ec2-short-event-driven-to-lambda"
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding=encoding)
    config, service = Mock(), Mock()
    config.get.return_value = True
    service.run.return_value = {"status": "evaluated"}
    service.evaluate.return_value = {"status": "evaluated"}
    monkeypatch.setattr(recommend, "ConfigService", Mock(return_value=config))
    monkeypatch.setattr(recommend, "SessionManager", Mock())
    monkeypatch.setattr(recommend, "RecommendationService", Mock(return_value=service))
    monkeypatch.setattr(sys, "argv", ["recommend", "--config", "unused.yaml", "--request", str(path)])
    recommend.main()
    assert json.loads(capsys.readouterr().out) == {"status": "evaluated"}
    selected = service.evaluate if explicit else service.run
    unselected = service.run if explicit else service.evaluate
    selected.assert_called_once_with(**request)
    unselected.assert_not_called()
