"""Selecting a subset of chats and messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .model import Chat

_SELECTOR_RE = re.compile(r"^(id|chat|recipient|group|contact|aci|e164):(.*)$", re.IGNORECASE)


class FilterError(ValueError):
    """A selector or time bound could not be understood."""


def parse_timestamp(value: str) -> int:
    """Parse ``--since``/``--until``: epoch millis, a date, or an ISO datetime."""
    text = value.strip()
    if text.isdigit():
        number = int(text)
        # Anything below this is implausible as milliseconds; treat it as seconds.
        return number if number > 10_000_000_000 else number * 1000
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise FilterError(
            f"could not parse time {value!r}; use YYYY-MM-DD, an ISO-8601 "
            "datetime, or epoch milliseconds"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


@dataclass
class ChatFilter:
    """Matches chats by id, name, group, or contact identity.

    Selectors are OR-ed together; an empty filter matches every chat.  A bare
    selector matches the chat id (if it is all digits) or a case-insensitive
    substring of the display name.  Identifiers - ACI, PNI, username, phone
    number - must match in full, so a short selector cannot accidentally match
    part of somebody's UUID.
    """

    selectors: list[str] = field(default_factory=list)
    groups_only: bool = False
    dms_only: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.selectors and not self.groups_only and not self.dms_only

    def matches(self, chat: Chat) -> bool:
        if self.groups_only and chat.kind != "group":
            return False
        if self.dms_only and chat.kind not in ("contact", "self"):
            return False
        if not self.selectors:
            return True
        return any(self._matches_selector(chat, selector) for selector in self.selectors)

    def _matches_selector(self, chat: Chat, selector: str) -> bool:
        match = _SELECTOR_RE.match(selector)
        scope, value = (match.group(1).lower(), match.group(2)) if match else ("", selector)
        value = value.strip()
        needle = value.casefold()
        recipient = chat.recipient

        if scope in ("id", "chat"):
            return _as_int(value) == chat.id
        if scope == "recipient":
            return _as_int(value) == chat.recipient_id
        if scope == "group":
            if chat.kind != "group":
                return False
            return needle in chat.name.casefold() or _key_matches(recipient, value)
        if scope == "contact":
            if chat.kind not in ("contact", "self"):
                return False
            return _identity_matches(chat, needle)
        if scope == "aci":
            return bool(recipient and recipient.aci and recipient.aci.casefold() == needle)
        if scope == "e164":
            digits = re.sub(r"\D", "", value)
            actual = re.sub(r"\D", "", recipient.e164 or "") if recipient else ""
            return bool(digits) and digits == actual

        # Bare selector: the chat id, or a substring of the display name.
        if value.isdigit() and int(value) == chat.id:
            return True
        return _identity_matches(chat, needle)


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _key_matches(recipient: Any, value: str) -> bool:
    master_key = getattr(recipient, "master_key", None)
    return bool(master_key) and master_key.casefold() == value.casefold()


def _identity_matches(chat: Chat, needle: str) -> bool:
    """Substring-match the display name, but require identifiers to match whole.

    Identifiers are matched exactly on purpose: a short selector like "11" would
    otherwise match any recipient whose UUID merely contains those digits.
    """
    if needle in chat.name.casefold():
        return True
    recipient = chat.recipient
    if recipient is None:
        return False
    for value in (recipient.aci, recipient.pni, recipient.username):
        if value and value.casefold() == needle:
            return True
    if recipient.e164:
        digits = re.sub(r"\D", "", needle)
        return bool(digits) and digits == re.sub(r"\D", "", recipient.e164)
    return False


@dataclass
class MessageFilter:
    """Post-selection constraints applied to individual chat items."""

    since_ms: int | None = None
    until_ms: int | None = None
    include_updates: bool = True
    search: str | None = None

    def matches(self, record: dict[str, Any]) -> bool:
        sent = record.get("dateSent")
        if self.since_ms is not None and (sent is None or sent < self.since_ms):
            return False
        if self.until_ms is not None and (sent is None or sent > self.until_ms):
            return False
        if not self.include_updates and record.get("type") == "update":
            return False
        if self.search:
            body = record.get("body") or ""
            if self.search.casefold() not in body.casefold():
                return False
        return True
