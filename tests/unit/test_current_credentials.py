"""Explicit same-account profile authentication; all identity calls are fakes."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.services.account_registry import AccountRegistry
from agent.services.session_manager import SessionManager

ACCOUNT = "111111111111"


def registry(**changes):
    return AccountRegistry([{"account_id": ACCOUNT, "account_type": "payer",
                             "use_current_credentials": True, **changes}], "us-east-1")


def test_explicit_current_credentials_allows_payer_without_role():
    assert registry().get_account(ACCOUNT).use_current_credentials is True


@pytest.mark.parametrize("changes", [{"account_id": "<PAYER_ACCOUNT_ID>"},
    {"role_arn": "arn:aws:iam::111111111111:role/Example"}, {"external_id": "example"},
    {"use_current_credentials": "true"}, {"use_current_credentials": False}])
def test_direct_mode_rejects_placeholders_conflicts_and_implicit_fallback(changes):
    with pytest.raises(ValueError):
        registry(**changes)


def test_same_session_identity_is_verified_before_returning_session(monkeypatch):
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {"Account": ACCOUNT}
    session_factory = Mock(return_value=session)
    monkeypatch.setattr("agent.services.session_manager.boto3.Session", session_factory)
    sts = Mock()
    manager = SessionManager(registry(), sts_client=sts)
    assert manager.get_session(ACCOUNT) is session
    session.client.assert_called_once_with("sts")
    session.client.return_value.get_caller_identity.assert_called_once_with()
    sts.assume_role.assert_not_called()


@pytest.mark.parametrize("identity", [{"Account": "222222222222"}, {}])
def test_wrong_account_identity_never_returns_a_session_or_data_client(monkeypatch, identity):
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = identity
    monkeypatch.setattr("agent.services.session_manager.boto3.Session", Mock(return_value=session))
    manager = SessionManager(registry(), sts_client=Mock())
    with pytest.raises(RuntimeError, match="do not match"):
        manager.get_client(ACCOUNT, "ce")
    session.client.assert_called_once_with("sts")


def test_failed_identity_lookup_is_not_suppressed_or_exposed_as_raw_credentials(monkeypatch):
    session = Mock()
    session.client.return_value.get_caller_identity.side_effect = RuntimeError("sensitive upstream diagnostic")
    monkeypatch.setattr("agent.services.session_manager.boto3.Session", Mock(return_value=session))
    with pytest.raises(RuntimeError, match="identity verification failed") as exc:
        SessionManager(registry(), sts_client=Mock()).get_session(ACCOUNT)
    assert "sensitive" not in str(exc.value)
