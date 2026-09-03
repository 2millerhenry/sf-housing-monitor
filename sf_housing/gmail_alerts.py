from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httplib2
from google_auth_httplib2 import AuthorizedHttp, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_TIMEOUT_SECONDS = 15
GMAIL_BODY_LIMIT_CHARS = 500_000
GMAIL_OAUTH_STATE_TTL = timedelta(minutes=10)
LOGGER = logging.getLogger(__name__)
_LOCAL_OAUTH_ENV_LOCK = threading.Lock()


class GmailAlertError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlertEmail:
    message_id: str
    subject: str
    html: str
    text: str


def _has_loopback_redirect(config: dict[str, Any]) -> bool:
    for value in config.get("redirect_uris") or []:
        parsed = urlparse(str(value))
        if (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            return True
    return False


def write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.chmod(temporary_name, 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    # Alert parsers need card markup, not unlimited mailbox payloads. Bound
    # allocation before decoding and bound the decoded text again.
    data = data[:700_000]
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
        "utf-8", errors="replace"
    )[:GMAIL_BODY_LIMIT_CHARS]


def _payload_bodies(payload: dict[str, Any]) -> tuple[str, str]:
    html_parts: list[str] = []
    text_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType", "")).casefold()
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        value = _decode_body(body.get("data"))
        if mime_type == "text/html" and value:
            html_parts.append(value)
        elif mime_type == "text/plain" and value:
            text_parts.append(value)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    return "\n".join(html_parts), "\n".join(text_parts)


def _fetch_oauth_token(flow: Flow, callback_url: str, code: str) -> None:
    """Exchange a code while allowing HTTP only for this machine's loopback callback."""
    parsed = urlparse(callback_url)
    is_local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not is_local_http:
        flow.fetch_token(code=code)
        return

    # oauthlib rejects all plain HTTP by default, including loopback callbacks.
    # Limit its development override to the token exchange and restore the process
    # environment immediately afterward. The lock prevents overlapping exchanges
    # from restoring another request's value.
    with _LOCAL_OAUTH_ENV_LOCK:
        previous = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        try:
            flow.fetch_token(code=code)
        finally:
            if previous is None:
                os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
            else:
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous


class GmailAlertMailbox:
    """A local-only Gmail OAuth client used exclusively for saved-search alerts."""

    def __init__(self, client_secret_path: Path, token_path: Path, pending_state_path: Path):
        self.client_secret_path = Path(client_secret_path)
        self.token_path = Path(token_path)
        self.pending_state_path = Path(pending_state_path)

    @property
    def has_client_secret(self) -> bool:
        return self.client_configuration_error is None

    @property
    def client_configuration_error(self) -> str | None:
        if not self.client_secret_path.is_file():
            return "The owner Google OAuth client is not installed."
        try:
            parsed = json.loads(self.client_secret_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "The owner Google OAuth client file is unreadable or invalid."
        if not isinstance(parsed, dict):
            return "The owner Google OAuth client file is invalid."
        client_kind = (
            "web"
            if isinstance(parsed.get("web"), dict)
            else "installed"
            if isinstance(parsed.get("installed"), dict)
            else None
        )
        if client_kind is None:
            return "Use a Google OAuth Desktop or Web application client JSON file."
        required = {"client_id", "client_secret", "auth_uri", "token_uri"}
        if not required.issubset(parsed[client_kind]):
            return "The owner Google OAuth client is missing required fields."
        if not _has_loopback_redirect(parsed[client_kind]):
            return "The owner Google OAuth client must allow a local loopback redirect URI."
        return None

    @property
    def client_kind(self) -> str | None:
        if not self.has_client_secret:
            return None
        parsed = json.loads(self.client_secret_path.read_text(encoding="utf-8"))
        return "installed" if isinstance(parsed.get("installed"), dict) else "web"

    @property
    def is_connected(self) -> bool:
        if not self.token_path.is_file():
            return False
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
            raw_scopes = payload.get("scopes", []) if isinstance(payload, dict) else []
            token_scopes = (
                {item for item in raw_scopes.split() if item}
                if isinstance(raw_scopes, str)
                else {str(item) for item in raw_scopes}
                if isinstance(raw_scopes, list)
                else set()
            )
            if token_scopes != {GMAIL_READONLY_SCOPE}:
                return False
            credentials = Credentials.from_authorized_user_file(
                str(self.token_path), [GMAIL_READONLY_SCOPE]
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return bool(credentials.refresh_token and credentials.has_scopes([GMAIL_READONLY_SCOPE]))

    def save_client_secret(self, contents: bytes) -> None:
        try:
            parsed = json.loads(contents.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailAlertError("The uploaded file is not valid Google OAuth client JSON.") from exc
        if not isinstance(parsed, dict):
            raise GmailAlertError("The uploaded file is not valid Google OAuth client JSON.")
        client_kind = "web" if isinstance(parsed.get("web"), dict) else "installed" if isinstance(parsed.get("installed"), dict) else None
        if client_kind is None:
            raise GmailAlertError(
                "Use a Google OAuth Desktop or Web application client JSON file for this local dashboard."
            )
        required = {"client_id", "client_secret", "auth_uri", "token_uri"}
        if not required.issubset(parsed[client_kind]):
            raise GmailAlertError("The OAuth client JSON is missing required client fields.")
        if not _has_loopback_redirect(parsed[client_kind]):
            raise GmailAlertError(
                "The OAuth client must allow an http://localhost or http://127.0.0.1 redirect URI."
            )
        write_private(self.client_secret_path, json.dumps(parsed, indent=2))

    def begin_authorization(self, callback_url: str) -> str:
        self._validate_callback_url(callback_url)
        if not self.has_client_secret:
            raise GmailAlertError(self.client_configuration_error or "Add the Google OAuth client before connecting Gmail.")
        state = secrets.token_urlsafe(32)
        flow = Flow.from_client_secrets_file(str(self.client_secret_path), scopes=[GMAIL_READONLY_SCOPE])
        flow.redirect_uri = callback_url
        authorization_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="false",
            prompt="consent",
            state=state,
        )
        if not flow.code_verifier:
            raise GmailAlertError("Google could not create a secure connection challenge. Start the connection again.")
        write_private(
            self.pending_state_path,
            json.dumps(
                {
                    "state": state,
                    "callback_url": callback_url,
                    "code_verifier": flow.code_verifier,
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            ),
        )
        return authorization_url

    def complete_authorization(self, callback_url: str, state: str | None, code: str | None) -> None:
        self._validate_callback_url(callback_url)
        if not state or not code or not self.pending_state_path.is_file():
            raise GmailAlertError("The Google connection link is missing or has expired. Start it again from Alerts.")
        try:
            pending = json.loads(self.pending_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GmailAlertError("The Google connection state could not be read. Start it again from Alerts.") from exc
        expected_state = str(pending.get("state", ""))
        expected_callback = str(pending.get("callback_url", ""))
        try:
            created_at = datetime.fromisoformat(str(pending.get("created_at") or ""))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            expired = datetime.now(UTC) - created_at.astimezone(UTC) > GMAIL_OAUTH_STATE_TTL
        except (TypeError, ValueError):
            # Legacy pending files did not contain a timestamp. Their file
            # modification time provides one bounded compatibility window.
            try:
                created_at = datetime.fromtimestamp(
                    self.pending_state_path.stat().st_mtime, tz=UTC
                )
                expired = datetime.now(UTC) - created_at > GMAIL_OAUTH_STATE_TTL
            except OSError:
                expired = True
        if expired:
            self.pending_state_path.unlink(missing_ok=True)
            raise GmailAlertError("The Google connection link expired. Start it again from Sources.")
        if not hmac.compare_digest(state, expected_state) or callback_url != expected_callback:
            raise GmailAlertError("The Google connection callback did not match the request that started it.")
        code_verifier = str(pending.get("code_verifier", ""))
        if not code_verifier:
            raise GmailAlertError("The Google connection link is missing or has expired. Start it again from Alerts.")
        flow = Flow.from_client_secrets_file(
            str(self.client_secret_path),
            scopes=[GMAIL_READONLY_SCOPE],
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        flow.redirect_uri = callback_url
        try:
            _fetch_oauth_token(flow, callback_url, code)
        except Exception as exc:
            # Keep OAuth codes/secrets out of logs, but retain the provider's
            # error class/message locally so a failed personal setup is
            # diagnosable instead of looking like a transient dashboard issue.
            LOGGER.warning("Google OAuth token exchange failed: %s", type(exc).__name__)
            raise GmailAlertError(
                "Google could not complete the read-only connection. Start the connection again."
            ) from exc
        write_private(self.token_path, flow.credentials.to_json())
        self.pending_state_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_callback_url(callback_url: str) -> None:
        parsed = urlparse(callback_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != "/alerts/gmail/callback"
            or parsed.query
            or parsed.fragment
        ):
            raise GmailAlertError("Gmail must return to this Mac through the local dashboard.")

    def disconnect(self, *, revoke: bool = True) -> bool:
        """Remove local authorization and best-effort revoke it at Google."""
        revoked = not revoke
        if revoke and self.token_path.is_file():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(self.token_path), [GMAIL_READONLY_SCOPE]
                )
                value = credentials.refresh_token or credentials.token
                if value:
                    response, _ = httplib2.Http(timeout=GMAIL_TIMEOUT_SECONDS).request(
                        "https://oauth2.googleapis.com/revoke",
                        method="POST",
                        body=urlencode({"token": value}),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    status = getattr(response, "status", None)
                    if status is None and isinstance(response, dict):
                        status = response.get("status", 0)
                    revoked = int(status or 0) == 200
            except Exception as exc:
                LOGGER.warning("Google authorization revocation was not confirmed: %s", type(exc).__name__)
                revoked = False
        self.token_path.unlink(missing_ok=True)
        self.pending_state_path.unlink(missing_ok=True)
        return revoked

    def _credentials(self) -> Credentials:
        if not self.token_path.is_file():
            raise GmailAlertError("Gmail is not connected yet.")
        if not self.is_connected:
            raise GmailAlertError(
                "Gmail authorization is missing the exact read-only scope. Reconnect it from Sources."
            )
        try:
            credentials = Credentials.from_authorized_user_file(
                str(self.token_path), [GMAIL_READONLY_SCOPE]
            )
            if credentials.expired and credentials.refresh_token:
                refresh_http = httplib2.Http(timeout=GMAIL_TIMEOUT_SECONDS)
                credentials.refresh(Request(refresh_http))
                write_private(self.token_path, credentials.to_json())
        except Exception as exc:
            raise GmailAlertError("Gmail authorization is invalid or expired. Reconnect it from Alerts.") from exc
        if not credentials.valid:
            raise GmailAlertError("Gmail authorization expired. Reconnect it from Alerts.")
        return credentials

    def messages(self, query: str, max_results: int = 100) -> list[AlertEmail]:
        credentials = self._credentials()
        transport = AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=GMAIL_TIMEOUT_SECONDS)
        )
        service = build("gmail", "v1", http=transport, cache_discovery=False)
        limit = max(1, min(int(max_results), 50))
        try:
            response = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=limit)
                .execute(num_retries=1)
            )
        except Exception as exc:
            raise GmailAlertError("Gmail alert search timed out or failed.") from exc

        message_ids = [str(item["id"]) for item in response.get("messages", []) if item.get("id")]
        if not message_ids:
            return []

        responses: dict[str, dict[str, Any]] = {}
        failures: list[tuple[str, Exception]] = []

        def receive(request_id: str, message: Any, exception: Exception | None) -> None:
            if exception is not None:
                failures.append((request_id, exception))
            elif isinstance(message, dict):
                responses[request_id] = message

        batch = service.new_batch_http_request(callback=receive)
        for message_id in message_ids:
            batch.add(
                service.users().messages().get(userId="me", id=message_id, format="full"),
                request_id=message_id,
            )
        try:
            batch.execute()
        except Exception as exc:
            raise GmailAlertError("Gmail alert messages timed out or failed to download.") from exc
        # Gmail occasionally omits individual messages from a successful batch
        # response. Retry only those IDs one at a time before calling them
        # skipped, so a transient batch failure cannot silently shrink a saved
        # search alert feed.
        if failures:
            batch_failures = failures
            failures = []
            for message_id, _ in batch_failures:
                try:
                    message = (
                        service.users()
                        .messages()
                        .get(userId="me", id=message_id, format="full")
                        .execute(num_retries=1)
                    )
                    if isinstance(message, dict):
                        responses[message_id] = message
                    else:
                        failures.append((message_id, RuntimeError("Gmail returned an invalid message.")))
                except Exception as exc:
                    failures.append((message_id, exc))
        if failures:
            LOGGER.warning("Gmail skipped %s alert message(s) that failed to download", len(failures))
        if failures and not responses:
            raise GmailAlertError("Gmail found alert messages, but none could be downloaded.")

        emails: list[AlertEmail] = []
        for message_id in message_ids:
            message = responses.get(message_id)
            if not message:
                continue
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            headers = payload.get("headers") if isinstance(payload.get("headers"), list) else []
            subject = next(
                (
                    str(header.get("value", ""))
                    for header in headers
                    if isinstance(header, dict) and str(header.get("name", "")).casefold() == "subject"
                ),
                "Housing alert",
            )
            html, text = _payload_bodies(payload)
            emails.append(AlertEmail(message_id, subject, html, text))
        return emails
