"""Read saved-search alert mail over IMAP with an app password.

The Gmail API path needs an owner-supplied OAuth client, because `gmail.readonly`
is a restricted scope: shipping one shared client to strangers requires Google
verification plus an annual third-party security assessment, and leaving the
consent screen in testing expires every user's token after seven days. IMAP with
an app password has none of that, costs nothing, and works on iCloud and Fastmail
as well as Gmail.

This module provides the same two members the alert sources actually use --
``is_connected`` and ``messages(query, max_results)`` -- so the six
``GmailHousingAlertSource`` subclasses work unchanged behind either backend.
"""

from __future__ import annotations

import email
import imaplib
import json
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

from .gmail_alerts import AlertEmail, write_private


IMAP_TIMEOUT_SECONDS = 20
BODY_LIMIT_CHARS = 500_000
MAX_RESULTS_CEILING = 50

# Providers that publish a stable IMAP host, so the user pastes an address and a
# password rather than hunting for server settings. Anything else is accepted
# with an explicit host.
KNOWN_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "fastmail.com": "imap.fastmail.com",
    "fastmail.fm": "imap.fastmail.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "aol.com": "imap.aol.com",
}

# Providers that no longer accept a password for IMAP at all, so the failure can
# be explained instead of looking like a typo.
OAUTH_ONLY_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


class ImapAlertError(RuntimeError):
    """A mail failure, optionally carrying the connector state it implies.

    Classifying by message text alone cannot separate "your password was
    refused" from "the server refused the search", and the two need opposite
    advice, so the credential paths say which state they mean.
    """

    def __init__(self, message: str, *, connector_state: str | None = None):
        super().__init__(message)
        self.connector_state = connector_state


def host_for_address(address: str) -> str | None:
    domain = address.strip().rsplit("@", 1)[-1].casefold()
    return KNOWN_HOSTS.get(domain)


def translate_query(query: str, *, now: datetime | None = None) -> list[str]:
    """Turn one of the shipped Gmail queries into IMAP SEARCH arguments.

    The alert sources pass Gmail syntax such as
    ``from:(zillow.com) newer_than:90d`` and, for Facebook, a bare ``Marketplace``
    term. IMAP speaks a different grammar, and a mistake here surfaces as "no
    alerts found" rather than as an error, so the mapping is deliberately narrow:
    anything it does not recognise becomes a full-text term rather than being
    dropped.
    """
    current = now or datetime.now(UTC)
    arguments: list[str] = []
    remaining = query or ""

    for match in re.finditer(r"from:\(([^)]*)\)|from:(\S+)", remaining, re.IGNORECASE):
        sender = (match.group(1) or match.group(2) or "").strip()
        if sender:
            arguments.extend(["FROM", sender])
    remaining = re.sub(r"from:\([^)]*\)|from:\S+", " ", remaining, flags=re.IGNORECASE)

    for match in re.finditer(r"newer_than:(\d+)([dmy])", remaining, re.IGNORECASE):
        amount = int(match.group(1))
        unit = match.group(2).casefold()
        days = amount * {"d": 1, "m": 31, "y": 365}[unit]
        since = current - timedelta(days=days)
        arguments.extend(["SINCE", since.strftime("%d-%b-%Y")])
    remaining = re.sub(r"newer_than:\d+[dmy]", " ", remaining, flags=re.IGNORECASE)

    # Anything Gmail-specific we do not model is dropped rather than sent to the
    # server as a literal, which would match nothing.
    remaining = re.sub(r"\b[a-z_]+:\S+", " ", remaining, flags=re.IGNORECASE)

    for term in remaining.split():
        cleaned = term.strip('"()').strip()
        if cleaned:
            arguments.extend(["TEXT", cleaned])

    return arguments or ["ALL"]


def _decode(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    return text[:BODY_LIMIT_CHARS]


def message_to_alert(raw: bytes, fallback_id: str) -> AlertEmail:
    """Turn one fetched RFC822 message into the shape the alert parsers expect."""
    parsed = email.message_from_bytes(raw[: BODY_LIMIT_CHARS * 2])
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        if part.get_content_maintype() == "multipart":
            continue
        # An attachment is not alert content and must not be decoded as one.
        if str(part.get("Content-Disposition") or "").casefold().startswith("attachment"):
            continue
        content_type = part.get_content_type().casefold()
        value = _decode(part)
        if not value:
            continue
        if content_type == "text/html":
            html_parts.append(value)
        elif content_type == "text/plain":
            text_parts.append(value)

    subject = ""
    try:
        decoded = email.header.decode_header(parsed.get("Subject") or "")
        subject = "".join(
            fragment.decode(encoding or "utf-8", errors="replace")
            if isinstance(fragment, bytes)
            else str(fragment)
            for fragment, encoding in decoded
        )
    except Exception:
        subject = str(parsed.get("Subject") or "")

    return AlertEmail(
        message_id=str(parsed.get("Message-ID") or fallback_id).strip(),
        subject=subject.strip()[:500],
        html="\n".join(html_parts)[:BODY_LIMIT_CHARS],
        text="\n".join(text_parts)[:BODY_LIMIT_CHARS],
    )


@dataclass(frozen=True, slots=True)
class ImapCredential:
    address: str
    password: str
    host: str
    folder: str = "INBOX"


class ImapAlertMailbox:
    """The alert mailbox backed by IMAP instead of the Gmail API."""

    def __init__(self, credential_path: Path, *, connector=imaplib.IMAP4_SSL):
        self.credential_path = Path(credential_path)
        self._connector = connector

    # -- credential -----------------------------------------------------

    @property
    def credential(self) -> ImapCredential | None:
        try:
            stored = json.loads(self.credential_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(stored, dict):
            return None
        address = str(stored.get("address") or "").strip()
        password = str(stored.get("password") or "")
        host = str(stored.get("host") or "").strip() or (host_for_address(address) or "")
        if not address or not password or not host:
            return None
        return ImapCredential(
            address=address,
            password=password,
            host=host,
            folder=str(stored.get("folder") or "INBOX"),
        )

    @property
    def is_connected(self) -> bool:
        return self.credential is not None

    def save_credential(self, address: str, password: str, host: str | None = None) -> None:
        address = address.strip()
        password = password.strip()
        if "@" not in address:
            raise ImapAlertError("Enter the full email address, including the @ sign.")
        domain = address.rsplit("@", 1)[-1].casefold()
        if domain in OAUTH_ONLY_DOMAINS:
            raise ImapAlertError(
                "Outlook.com and Hotmail no longer allow app passwords for mail access. "
                "Use the advanced Google sign-in option, or forward the alerts to another address."
            )
        if not password:
            raise ImapAlertError("Paste the app password you generated for this monitor.")
        resolved = (host or "").strip() or host_for_address(address)
        if not resolved:
            raise ImapAlertError(
                f"We do not know the mail server for {domain}. Enter its IMAP server address."
            )
        write_private(
            self.credential_path,
            json.dumps({"address": address, "password": password, "host": resolved}, indent=2),
        )

    def disconnect(self) -> bool:
        try:
            self.credential_path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ImapAlertError("The saved mail credential could not be removed.") from exc

    # -- reading --------------------------------------------------------

    def _connect(self, credential: ImapCredential):
        try:
            connection = self._connector(
                credential.host, timeout=IMAP_TIMEOUT_SECONDS, ssl_context=ssl.create_default_context()
            )
        except TypeError:
            # Test doubles and older signatures need not accept every keyword.
            connection = self._connector(credential.host)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ImapAlertError(
                f"Could not reach {credential.host}. Check the connection and try again."
            ) from exc
        try:
            connection.login(credential.address, credential.password)
        except imaplib.IMAP4.error as exc:
            raise ImapAlertError(
                "That address and app password were refused. Make sure two-step verification is on "
                "and that you pasted an app password rather than your normal password.",
                connector_state="authorization_expired",
            ) from exc
        return connection

    def messages(self, query: str, max_results: int = 100) -> list[AlertEmail]:
        credential = self.credential
        if credential is None:
            raise ImapAlertError("No mail account is connected.")
        limit = max(1, min(int(max_results), MAX_RESULTS_CEILING))
        connection = self._connect(credential)
        try:
            status, _ = connection.select(credential.folder, readonly=True)
            if status != "OK":
                raise ImapAlertError(f"The mail folder {credential.folder} could not be opened.")
            status, data = connection.search(None, *translate_query(query))
            if status != "OK":
                raise ImapAlertError("The mail search was refused by the server.")
            identifiers = (data[0].split() if data and data[0] else [])
            # Newest first, matching what the Gmail path returns.
            identifiers = list(reversed(identifiers))[:limit]

            alerts: list[AlertEmail] = []
            for identifier in identifiers:
                # PEEK so reading alerts never marks the user's mail as read.
                status, payload = connection.fetch(identifier, "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    continue
                for item in payload:
                    if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                        alerts.append(message_to_alert(item[1], identifier.decode("ascii", "replace")))
                        break
            return alerts
        finally:
            try:
                connection.close()
            except Exception:
                pass
            try:
                connection.logout()
            except Exception:
                pass


class AlertMailboxRouter:
    """Read alerts through whichever backend is currently connected.

    The choice cannot be made once at startup: a user who pastes an app password
    expects their next scan to use it, not to have to restart the app. Both
    backends expose the same two members, so this simply asks which one is live
    at the moment of the call.
    """

    def __init__(self, imap: ImapAlertMailbox, gmail):
        self.imap = imap
        self.gmail = gmail

    @property
    def active(self):
        return self.imap if self.imap.is_connected else self.gmail

    @property
    def backend(self) -> str:
        return "imap" if self.imap.is_connected else "gmail"

    @property
    def is_connected(self) -> bool:
        return self.active.is_connected

    def messages(self, query: str, max_results: int = 100) -> list[AlertEmail]:
        return self.active.messages(query, max_results)
