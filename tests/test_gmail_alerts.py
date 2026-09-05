from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from sf_housing import gmail_alerts
from sf_housing.gmail_alerts import GMAIL_BODY_LIMIT_CHARS, GmailAlertError, GmailAlertMailbox


class FakeRequest:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=0):
        return self.value


class FakeGet:
    def __init__(self, message_id: str):
        self.message_id = message_id


class FakeMessages:
    def __init__(self):
        self.list_limit = None

    def list(self, **kwargs):
        self.list_limit = kwargs["maxResults"]
        return FakeRequest({"messages": [{"id": "one"}, {"id": "two"}]})

    def get(self, **kwargs):
        return FakeGet(kwargs["id"])


class FakeUsers:
    def __init__(self, messages: FakeMessages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeBatch:
    def __init__(self, callback):
        self.callback = callback
        self.requests = []
        self.execute_count = 0

    def add(self, request, request_id):
        self.requests.append((request, request_id))

    def execute(self):
        self.execute_count += 1
        for request, request_id in self.requests:
            body = base64.urlsafe_b64encode(f"Body {request.message_id}".encode()).decode()
            self.callback(
                request_id,
                {
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [{"name": "Subject", "value": f"Alert {request_id}"}],
                        "body": {"data": body},
                    }
                },
                None,
            )


class FakeService:
    def __init__(self):
        self.messages_resource = FakeMessages()
        self.batch = None

    def users(self):
        return FakeUsers(self.messages_resource)

    def new_batch_http_request(self, callback):
        self.batch = FakeBatch(callback)
        return self.batch


def test_gmail_fetches_message_bodies_in_one_bounded_batch(tmp_path, monkeypatch) -> None:
    mailbox = GmailAlertMailbox(tmp_path / "client.json", tmp_path / "token.json", tmp_path / "state.json")
    service = FakeService()
    monkeypatch.setattr(mailbox, "_credentials", lambda: object())
    monkeypatch.setattr(gmail_alerts.httplib2, "Http", lambda timeout: object())
    monkeypatch.setattr(gmail_alerts, "AuthorizedHttp", lambda credentials, http: object())
    monkeypatch.setattr(gmail_alerts, "build", lambda *args, **kwargs: service)

    messages = mailbox.messages("from:zillow.com", max_results=100)

    assert service.messages_resource.list_limit == 50
    assert service.batch.execute_count == 1
    assert len(service.batch.requests) == 2
    assert [message.subject for message in messages] == ["Alert one", "Alert two"]
    assert [message.text for message in messages] == ["Body one", "Body two"]


def test_corrupt_token_does_not_appear_connected(tmp_path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("not json", encoding="utf-8")
    mailbox = GmailAlertMailbox(tmp_path / "client.json", token_path, tmp_path / "state.json")

    assert mailbox.is_connected is False


def test_oauth_token_exchange_allows_only_loopback_http(monkeypatch) -> None:
    calls = []

    class FakeFlow:
        def fetch_token(self, code):
            calls.append((code, os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")))

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)

    gmail_alerts._fetch_oauth_token(
        FakeFlow(),
        "http://127.0.0.1:8000/alerts/gmail/callback",
        "approved-code",
    )

    assert calls == [("approved-code", "1")]
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


def test_oauth_token_exchange_keeps_security_check_for_nonlocal_urls(monkeypatch) -> None:
    calls = []

    class FakeFlow:
        def fetch_token(self, code):
            calls.append((code, os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")))

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)

    gmail_alerts._fetch_oauth_token(
        FakeFlow(),
        "https://housing.example.test/alerts/gmail/callback",
        "approved-code",
    )

    assert calls == [("approved-code", None)]


def test_begin_authorization_persists_pkce_verifier(tmp_path) -> None:
    client_path = tmp_path / "client.json"
    state_path = tmp_path / "state.json"
    client_path.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "test.apps.googleusercontent.com",
                    "client_secret": "test-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [
                        "http://127.0.0.1:8000/alerts/gmail/callback"
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    mailbox = GmailAlertMailbox(client_path, tmp_path / "token.json", state_path)

    authorization_url = mailbox.begin_authorization(
        "http://127.0.0.1:8000/alerts/gmail/callback"
    )
    pending = json.loads(state_path.read_text(encoding="utf-8"))

    assert "code_challenge=" in authorization_url
    assert len(pending["code_verifier"]) >= 43


def test_complete_authorization_restores_pkce_verifier(tmp_path, monkeypatch) -> None:
    client_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    state_path = tmp_path / "state.json"
    callback_url = "http://127.0.0.1:8000/alerts/gmail/callback"
    client_path.write_text("{}", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "state": "expected-state",
                "callback_url": callback_url,
                "code_verifier": "v" * 64,
            }
        ),
        encoding="utf-8",
    )
    created_with = {}

    class FakeCredentials:
        def to_json(self):
            return '{"refresh_token":"saved"}'

    class FakeFlow:
        credentials = FakeCredentials()
        redirect_uri = None

    def fake_flow_factory(path, scopes, **kwargs):
        created_with.update(kwargs)
        return FakeFlow()

    monkeypatch.setattr(gmail_alerts.Flow, "from_client_secrets_file", fake_flow_factory)
    monkeypatch.setattr(gmail_alerts, "_fetch_oauth_token", lambda flow, callback, code: None)
    mailbox = GmailAlertMailbox(client_path, token_path, state_path)

    mailbox.complete_authorization(callback_url, "expected-state", "approved-code")

    assert created_with["code_verifier"] == "v" * 64
    assert created_with["autogenerate_code_verifier"] is False
    assert json.loads(token_path.read_text(encoding="utf-8"))["refresh_token"] == "saved"
    assert not state_path.exists()


def test_invalid_client_file_is_not_presented_as_ready(tmp_path) -> None:
    client_path = tmp_path / "client.json"
    client_path.write_text("not json", encoding="utf-8")
    mailbox = GmailAlertMailbox(client_path, tmp_path / "token.json", tmp_path / "state.json")

    assert mailbox.has_client_secret is False
    assert "invalid" in (mailbox.client_configuration_error or "").casefold()


def test_remote_only_client_is_rejected_before_oauth(tmp_path) -> None:
    mailbox = GmailAlertMailbox(
        tmp_path / "client.json", tmp_path / "token.json", tmp_path / "state.json"
    )
    remote_only = json.dumps(
        {
            "web": {
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["https://example.test/callback"],
            }
        }
    ).encode()

    with pytest.raises(GmailAlertError, match="localhost"):
        mailbox.save_client_secret(remote_only)

    assert not mailbox.client_secret_path.exists()


def test_authorization_rejects_nonlocal_callback(tmp_path) -> None:
    mailbox = GmailAlertMailbox(tmp_path / "client.json", tmp_path / "token.json", tmp_path / "state.json")

    with pytest.raises(GmailAlertError, match="local dashboard"):
        mailbox.begin_authorization("https://example.test/alerts/gmail/callback")


def test_expired_oauth_state_is_single_use_and_removed(tmp_path) -> None:
    client_path = tmp_path / "client.json"
    state_path = tmp_path / "state.json"
    callback = "http://127.0.0.1:8000/alerts/gmail/callback"
    client_path.write_text("{}", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "state": "expected",
                "callback_url": callback,
                "code_verifier": "v" * 64,
                "created_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    mailbox = GmailAlertMailbox(client_path, tmp_path / "token.json", state_path)

    with pytest.raises(GmailAlertError, match="expired"):
        mailbox.complete_authorization(callback, "expected", "code")

    assert not state_path.exists()


def test_oauth_state_mismatch_does_not_exchange_or_replace_token(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    token_path = tmp_path / "token.json"
    callback = "http://127.0.0.1:8000/alerts/gmail/callback"
    state_path.write_text(
        json.dumps(
            {
                "state": "expected",
                "callback_url": callback,
                "code_verifier": "v" * 64,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gmail_alerts,
        "_fetch_oauth_token",
        lambda *args, **kwargs: pytest.fail("a mismatched state must not exchange a code"),
    )
    mailbox = GmailAlertMailbox(tmp_path / "client.json", token_path, state_path)

    with pytest.raises(GmailAlertError, match="did not match"):
        mailbox.complete_authorization(callback, "wrong", "never-exchange")

    assert not token_path.exists()


def test_successful_oauth_callback_cannot_be_reused(tmp_path, monkeypatch) -> None:
    client_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    state_path = tmp_path / "state.json"
    callback = "http://127.0.0.1:8000/alerts/gmail/callback"
    client_path.write_text("{}", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "state": "expected",
                "callback_url": callback,
                "code_verifier": "v" * 64,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    class Credentials:
        def to_json(self):
            return '{"refresh_token":"saved"}'

    class FlowFixture:
        credentials = Credentials()
        redirect_uri = None

    monkeypatch.setattr(
        gmail_alerts.Flow, "from_client_secrets_file", lambda *args, **kwargs: FlowFixture()
    )
    monkeypatch.setattr(gmail_alerts, "_fetch_oauth_token", lambda *args, **kwargs: None)
    mailbox = GmailAlertMailbox(client_path, token_path, state_path)

    mailbox.complete_authorization(callback, "expected", "first-code")
    with pytest.raises(GmailAlertError, match="missing or has expired"):
        mailbox.complete_authorization(callback, "expected", "reused-code")

    assert not state_path.exists()


def test_token_without_gmail_readonly_scope_is_not_connected(tmp_path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            }
        ),
        encoding="utf-8",
    )
    mailbox = GmailAlertMailbox(tmp_path / "client.json", token_path, tmp_path / "state.json")

    assert mailbox.is_connected is False


def test_token_with_exact_gmail_readonly_scope_is_connected(tmp_path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": [gmail_alerts.GMAIL_READONLY_SCOPE],
            }
        ),
        encoding="utf-8",
    )
    mailbox = GmailAlertMailbox(tmp_path / "client.json", token_path, tmp_path / "state.json")

    assert mailbox.is_connected is True


def test_disconnect_removes_local_token_even_when_google_revoke_fails(tmp_path, monkeypatch) -> None:
    token_path = tmp_path / "token.json"
    state_path = tmp_path / "state.json"
    token_path.write_text(
        '{"refresh_token":"refresh","client_id":"id","client_secret":"secret",'
        '"token_uri":"https://oauth2.googleapis.com/token"}',
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")

    class FailedHttp:
        def __init__(self, timeout):
            pass

        def request(self, *args, **kwargs):
            raise OSError("offline")

    monkeypatch.setattr(gmail_alerts.httplib2, "Http", FailedHttp)
    mailbox = GmailAlertMailbox(tmp_path / "client.json", token_path, state_path)

    assert mailbox.disconnect(revoke=True) is False
    assert not token_path.exists()
    assert not state_path.exists()


def test_mail_body_decode_is_bounded() -> None:
    oversized = base64.urlsafe_b64encode(
        b"x" * (GMAIL_BODY_LIMIT_CHARS + 100_000)
    ).decode()

    assert len(gmail_alerts._decode_body(oversized)) == GMAIL_BODY_LIMIT_CHARS


# --------------------------------------------------------------------------
# mail from a provider is not the same thing as an alert from a provider
# --------------------------------------------------------------------------


class Envelope:
    def __init__(self, subject: str, html: str = ""):
        self.subject = subject
        self.html = html
        self.text = ""
        self.message_id = subject


class Inbox:
    def __init__(self, emails):
        self._emails = list(emails)
        self.is_connected = True

    def messages(self, query, max_results=50):
        return list(self._emails)[:max_results]


def zillow(mailbox):
    from sf_housing.sources import ZillowAlertSource

    return ZillowAlertSource(mailbox)


def test_a_welcome_email_is_not_a_failed_alert() -> None:
    """The real mailbox held one message from Zillow -- "Welcome to Zillow" --
    and the page reported a parser failure telling the reader their notification
    settings needed attention. Nothing was wrong: they had not saved a search."""
    from sf_housing.preferences import parse_preferences
    from tests.conftest import TEST_PREFERENCES

    source = zillow(Inbox([Envelope("Welcome to Zillow", "<a href='https://click.mail.zillow.com/x'>Start</a>")]))

    listings = source.search(None, parse_preferences(TEST_PREFERENCES))

    assert listings == []
    assert source.last_alert_count == 0, "no alert has arrived, so none is counted"


@pytest.mark.parametrize(
    "subject",
    [
        "Welcome to Zillow",
        "Verify your email address",
        "Your password has been changed",
        "Receipt for your payment",
        "New sign-in to your account",
        "Getting started with Zillow",
        "Tour request confirmed",
    ],
)
def test_account_mail_never_counts_as_an_alert(subject: str) -> None:
    source = zillow(Inbox([Envelope(subject)]))

    assert source._is_alert_email(Envelope(subject)) is False, subject


@pytest.mark.parametrize(
    "subject",
    [
        "606 Capp St #105 just listed",
        "New for rent: 1200 Market St, San Francisco",
        "3 new listings match your saved search",
        "New rentals in San Francisco",
    ],
)
def test_a_real_alert_still_counts(subject: str) -> None:
    source = zillow(Inbox([Envelope(subject)]))

    assert source._is_alert_email(Envelope(subject)) is True, subject


def test_an_alert_that_cannot_be_parsed_is_still_reported() -> None:
    """The guarantee that must survive: a genuine alert yielding no listing is a
    real problem and has to keep saying so."""
    from sf_housing.preferences import parse_preferences
    from sf_housing.sources import SourceError
    from tests.conftest import TEST_PREFERENCES

    source = zillow(Inbox([Envelope("4 new listings match your saved search", "<p>no links</p>")]))

    with pytest.raises(SourceError) as raised:
        source.search(None, parse_preferences(TEST_PREFERENCES))

    assert "none contained a direct listing link" in str(raised.value)
    assert source.last_alert_count == 1


def test_a_mailbox_with_only_account_mail_reads_as_waiting_not_broken() -> None:
    """last_alert_count is what decides between "waiting for first alert" and
    "degraded", so it has to count alerts rather than mail."""
    from sf_housing.preferences import parse_preferences
    from tests.conftest import TEST_PREFERENCES

    source = zillow(Inbox([Envelope("Welcome to Zillow"), Envelope("Verify your email address")]))
    source.search(None, parse_preferences(TEST_PREFERENCES))

    assert source.last_alert_count == 0
