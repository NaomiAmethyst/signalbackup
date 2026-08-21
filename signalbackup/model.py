"""Turning backup frames into records that other tools can consume.

The frame stream is ordered so that a recipient always precedes anything that
references it, and a chat always precedes its messages.  That lets everything
here work in a single streaming pass: recipients and chats accumulate in an
index, and chat items are emitted as they arrive.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import crypto
from .archive import Archive, Snapshot
from .protoschema import Schema, pick_oneof
from .schema import BACKUP_INFO, FRAME, load_schema

FRAME_KINDS = (
    "account", "recipient", "chat", "chatItem",
    "stickerPack", "adHocCall", "notificationProfile", "chatFolder",
)

# Where a FilePointer can hang off a chat item, and what to call it.
_ATTACHMENT_ROLES = {
    "attachment": "attachment",
    "longText": "long-text",
    "sticker": "sticker",
    "linkPreview": "link-preview",
    "quote": "quote-thumbnail",
    "contact": "contact-avatar",
    "viewOnce": "view-once",
}


def iso_timestamp(millis: int | None) -> str | None:
    """Format a Signal timestamp (ms since epoch) as UTC ISO-8601."""
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _uuid_str(hex_value: str | None) -> str | None:
    if not hex_value:
        return None
    try:
        return str(uuid.UUID(hex=hex_value))
    except ValueError:
        return hex_value


def _e164(value: int | None) -> str | None:
    return f"+{value}" if value else None


def _join_name(*parts: str | None) -> str | None:
    joined = " ".join(part for part in parts if part)
    return joined or None


# --------------------------------------------------------------------------
# Recipients and chats
# --------------------------------------------------------------------------

@dataclass
class Recipient:
    id: int
    kind: str
    name: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def detail(self) -> dict[str, Any]:
        return self.raw.get(self.kind, {}) if self.kind in self.raw else {}

    @property
    def aci(self) -> str | None:
        return _uuid_str(self.detail.get("aci"))

    @property
    def pni(self) -> str | None:
        return _uuid_str(self.detail.get("pni"))

    @property
    def e164(self) -> str | None:
        return _e164(self.detail.get("e164"))

    @property
    def username(self) -> str | None:
        return self.detail.get("username")

    @property
    def master_key(self) -> str | None:
        return self.detail.get("masterKey") if self.kind == "group" else None

    def to_json(self, *, verbose: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.kind, "name": self.name}
        detail = self.detail
        if self.kind == "contact":
            out.update({
                "aci": self.aci,
                "pni": self.pni,
                "e164": self.e164,
                "username": self.username,
                "profileName": _join_name(detail.get("profileGivenName"),
                                          detail.get("profileFamilyName")),
                "systemName": _join_name(detail.get("systemGivenName"),
                                         detail.get("systemFamilyName")),
                "nickname": _join_name((detail.get("nickname") or {}).get("given"),
                                       (detail.get("nickname") or {}).get("family")),
                "blocked": bool(detail.get("blocked")),
                "hidden": detail.get("visibility", "VISIBLE") != "VISIBLE",
                "registered": "notRegistered" not in detail,
                "note": detail.get("note") or None,
            })
        elif self.kind == "group":
            snapshot = detail.get("snapshot", {})
            out.update({
                "masterKey": detail.get("masterKey"),
                "description": (snapshot.get("description") or {}).get("descriptionText"),
                "memberCount": len(snapshot.get("members", [])),
                "members": [
                    {
                        "aci": _uuid_str(member.get("userId")),
                        "role": member.get("role", "UNKNOWN"),
                    }
                    for member in snapshot.get("members", [])
                ],
                "blocked": bool(detail.get("blocked")),
                "announcementsOnly": bool(snapshot.get("announcementsOnly")),
                "terminated": bool(snapshot.get("terminated")),
            })
        elif self.kind == "distributionList":
            out["distributionId"] = _uuid_str(detail.get("distributionId"))
        elif self.kind == "callLink":
            out["restrictions"] = detail.get("restrictions")

        out = {key: value for key, value in out.items() if value is not None}
        if verbose:
            out["raw"] = self.raw
        return out


def _recipient_name(kind: str, detail: dict[str, Any]) -> str:
    if kind == "contact":
        nickname = detail.get("nickname") or {}
        return (
            _join_name(nickname.get("given"), nickname.get("family"))
            or detail.get("systemNickname")
            or _join_name(detail.get("systemGivenName"), detail.get("systemFamilyName"))
            or _join_name(detail.get("profileGivenName"), detail.get("profileFamilyName"))
            or detail.get("username")
            or _e164(detail.get("e164"))
            or _uuid_str(detail.get("aci"))
            or "Unknown contact"
        )
    if kind == "group":
        title = ((detail.get("snapshot") or {}).get("title") or {}).get("title")
        return title or "Unknown group"
    if kind == "self":
        return "Note to Self"
    if kind == "releaseNotes":
        return "Signal"
    if kind == "callLink":
        return detail.get("name") or "Call link"
    if kind == "distributionList":
        listing = detail.get("distributionList") or {}
        return listing.get("name") or "My Story"
    return "Unknown"


@dataclass
class Chat:
    id: int
    recipient_id: int
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    recipient: Recipient | None = None
    message_count: int = 0

    @property
    def name(self) -> str:
        return self.recipient.name if self.recipient else f"chat {self.id}"

    @property
    def kind(self) -> str:
        return self.recipient.kind if self.recipient else "unknown"

    def to_json(self, *, verbose: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.kind,
            "recipientId": self.recipient_id,
            "archived": bool(self.raw.get("archived")),
            "pinnedOrder": self.raw.get("pinnedOrder"),
            "expirationTimerMs": self.raw.get("expirationTimerMs"),
            "markedUnread": bool(self.raw.get("markedUnread")),
            "messageCount": self.message_count,
        }
        if self.recipient:
            for key in ("aci", "e164", "username", "masterKey"):
                value = getattr(self.recipient, key, None)
                if value:
                    out[key] = value
        out = {key: value for key, value in out.items() if value is not None}
        if verbose:
            out["raw"] = self.raw
        return out


class Index:
    """Accumulates recipients and chats as the frame stream is read."""

    def __init__(self) -> None:
        self.recipients: dict[int, Recipient] = {}
        self.chats: dict[int, Chat] = {}
        self.chat_by_recipient: dict[int, Chat] = {}
        self.account: dict[str, Any] = {}
        self.self_recipient_id: int | None = None

    def add_recipient(self, value: dict[str, Any], schema: Schema) -> Recipient:
        kind, detail = pick_oneof(
            value, schema.oneof_fields("signal.backup.Recipient", "destination")
        )
        detail = detail or {}
        recipient = Recipient(
            id=value.get("id", 0),
            kind=kind or "unknown",
            name=_recipient_name(kind or "unknown", detail),
            raw=value,
        )
        self.recipients[recipient.id] = recipient
        if recipient.kind == "self":
            self.self_recipient_id = recipient.id
        chat = self.chat_by_recipient.get(recipient.id)
        if chat is not None:
            chat.recipient = recipient
        return recipient

    def add_chat(self, value: dict[str, Any]) -> Chat:
        chat = Chat(
            id=value.get("id", 0),
            recipient_id=value.get("recipientId", 0),
            raw=value,
            recipient=self.recipients.get(value.get("recipientId", 0)),
        )
        self.chats[chat.id] = chat
        self.chat_by_recipient[chat.recipient_id] = chat
        return chat

    def recipient_ref(self, recipient_id: int | None) -> dict[str, Any] | None:
        if recipient_id is None:
            return None
        recipient = self.recipients.get(recipient_id)
        if recipient is None:
            return {"id": recipient_id, "name": f"recipient {recipient_id}"}
        ref: dict[str, Any] = {"id": recipient.id, "name": recipient.name}
        if recipient.kind == "self":
            ref["self"] = True
        if recipient.aci:
            ref["aci"] = recipient.aci
        if recipient.e164:
            ref["e164"] = recipient.e164
        return ref


# --------------------------------------------------------------------------
# Reading a snapshot
# --------------------------------------------------------------------------

class BackupReader:
    """Streams a snapshot's ``main`` archive as decoded frames."""

    def __init__(self, snapshot: Snapshot, keys: crypto.BackupKeys, *,
                 verify_mac: bool = True) -> None:
        self.snapshot = snapshot
        self.keys = keys
        self.verify_mac = verify_mac
        self.schema = load_schema()
        self.header: dict[str, Any] = {}

    def frames(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(kind, value)`` pairs; the header lands in :attr:`header`."""
        records = crypto.read_backup_frames(
            self.snapshot.main_path, self.keys, verify_mac=self.verify_mac
        )
        first = next(records, None)
        if first is None:
            return
        self.header = self.schema.decode(first, BACKUP_INFO, bytes_as="hex")

        oneof = self.schema.oneof_fields(FRAME, "item")
        for record in records:
            frame = self.schema.decode(record, FRAME, bytes_as="hex")
            kind, value = pick_oneof(frame, oneof)
            if kind is not None:
                yield kind, value


def build_index(reader: BackupReader, *, count_messages: bool = False) -> Index:
    """Read a whole snapshot and return its recipients and chats.

    With ``count_messages`` the message stream is consumed too, which costs a
    full decrypt pass but fills in per-chat totals.
    """
    index = Index()
    for kind, value in reader.frames():
        if kind == "recipient":
            index.add_recipient(value, reader.schema)
        elif kind == "chat":
            index.add_chat(value)
        elif kind == "account":
            index.account = value
        elif kind == "chatItem":
            if not count_messages:
                break
            chat = index.chats.get(value.get("chatId", -1))
            if chat is not None:
                chat.message_count += 1
    return index


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------

@dataclass
class AttachmentRef:
    """One file referenced by a message, resolved to its blob in ``files/``."""

    role: str
    pointer: dict[str, Any]
    media_name: str | None
    local_key: bytes | None
    plaintext_size: int | None
    content_type: str | None
    file_name: str | None

    def to_json(self, archive: Archive | None = None, *,
                include_keys: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "contentType": self.content_type,
            "fileName": self.file_name,
            "size": self.plaintext_size,
            "mediaName": self.media_name,
        }
        for key in ("width", "height", "caption", "blurHash"):
            if self.pointer.get(key) is not None:
                out[key] = self.pointer[key]
        if self.media_name and archive is not None:
            path = archive.media_path(self.media_name)
            out["available"] = path.is_file()
            if out["available"]:
                out["path"] = str(path.relative_to(archive.root))
        elif self.media_name is None:
            out["available"] = False
            out["unavailableReason"] = "no local key in backup (media not stored locally)"
        if include_keys and self.local_key is not None:
            out["localKey"] = self.local_key.hex()
        return {key: value for key, value in out.items() if value is not None}


def _pointer_to_ref(pointer: dict[str, Any] | None, role: str) -> AttachmentRef | None:
    if not pointer:
        return None
    locator = pointer.get("locatorInfo") or {}
    plaintext_hash = locator.get("plaintextHash")
    local_key_hex = locator.get("localKey")

    media_name = local_key = None
    if plaintext_hash and local_key_hex:
        local_key = bytes.fromhex(local_key_hex)
        media_name = crypto.media_name_for(bytes.fromhex(plaintext_hash), local_key)

    return AttachmentRef(
        role=role,
        pointer=pointer,
        media_name=media_name,
        local_key=local_key,
        plaintext_size=locator.get("size"),
        content_type=pointer.get("contentType"),
        file_name=pointer.get("fileName"),
    )


def _message_attachment_ref(attachment: dict[str, Any], role: str) -> AttachmentRef | None:
    ref = _pointer_to_ref(attachment.get("pointer"), role)
    if ref is not None:
        flag = attachment.get("flag")
        if flag and flag != "NONE":
            ref.pointer = {**ref.pointer, "flag": flag}
    return ref


def collect_attachments(item_kind: str | None, item: dict[str, Any]) -> list[AttachmentRef]:
    """Every file referenced by one chat item, in a stable order."""
    refs: list[AttachmentRef | None] = []
    item = item or {}

    if item_kind in ("standardMessage", "directStoryReplyMessage"):
        for attachment in item.get("attachments", []):
            refs.append(_message_attachment_ref(attachment, _ATTACHMENT_ROLES["attachment"]))
        refs.append(_pointer_to_ref(item.get("longText"), _ATTACHMENT_ROLES["longText"]))
        for preview in item.get("linkPreview", []):
            refs.append(_pointer_to_ref(preview.get("image"), _ATTACHMENT_ROLES["linkPreview"]))
        reply = (item.get("textReply") or {})
        refs.append(_pointer_to_ref(reply.get("longText"), _ATTACHMENT_ROLES["longText"]))
        quote = item.get("quote") or {}
        for quoted in quote.get("attachments", []):
            refs.append(_message_attachment_ref(quoted.get("thumbnail", {}),
                                                _ATTACHMENT_ROLES["quote"]))
    elif item_kind == "stickerMessage":
        sticker = item.get("sticker") or {}
        refs.append(_pointer_to_ref(sticker.get("data"), _ATTACHMENT_ROLES["sticker"]))
    elif item_kind == "viewOnceMessage":
        refs.append(_message_attachment_ref(item.get("attachment", {}),
                                            _ATTACHMENT_ROLES["viewOnce"]))
    elif item_kind == "contactMessage":
        contact = item.get("contact") or {}
        refs.append(_pointer_to_ref(contact.get("avatar"), _ATTACHMENT_ROLES["contact"]))

    return [ref for ref in refs if ref is not None]


# --------------------------------------------------------------------------
# Chat items
# --------------------------------------------------------------------------

_SIMPLE_UPDATE_TEXT = {
    "JOINED_SIGNAL": "{author} joined Signal",
    "IDENTITY_UPDATE": "Safety number changed",
    "IDENTITY_VERIFIED": "Marked verified",
    "IDENTITY_DEFAULT": "Marked unverified",
    "CHANGE_NUMBER": "{author} changed their phone number",
    "RELEASE_CHANNEL_DONATION_REQUEST": "Signal donation request",
    "END_SESSION": "Session reset",
    "CHAT_SESSION_REFRESH": "Chat session refreshed",
    "BAD_DECRYPT": "A message could not be delivered",
    "PAYMENTS_ACTIVATED": "{author} activated payments",
    "PAYMENT_ACTIVATION_REQUEST": "{author} requested to activate payments",
    "UNSUPPORTED_PROTOCOL_MESSAGE": "Unsupported message",
    "REPORTED_SPAM": "Reported as spam",
    "BLOCKED": "{author} blocked",
    "UNBLOCKED": "{author} unblocked",
    "MESSAGE_REQUEST_ACCEPTED": "Message request accepted",
}


def _update_summary(update: dict[str, Any], schema: Schema, author_name: str) -> tuple[str, str]:
    """Return the update's variant name and a short human-readable summary."""
    kind, value = pick_oneof(
        update, schema.oneof_fields("signal.backup.ChatUpdateMessage", "update")
    )
    value = value if isinstance(value, dict) else {}

    if kind == "simpleUpdate":
        code = value.get("type", "UNKNOWN")
        template = _SIMPLE_UPDATE_TEXT.get(code, code.replace("_", " ").capitalize())
        return kind, template.format(author=author_name)
    if kind == "expirationTimerChange":
        millis = value.get("expiresInMs", 0)
        if not millis:
            return kind, "Disappearing messages off"
        return kind, f"Disappearing messages set to {int(millis) // 1000}s"
    if kind == "profileChange":
        previous, new = value.get("previousName", "?"), value.get("newName", "?")
        return kind, f"{previous} changed their name to {new}"
    if kind == "learnedProfileChange":
        return kind, f"{value.get('e164') or value.get('username') or 'Someone'} joined the chat"
    if kind == "individualCall":
        direction = value.get("direction", "UNKNOWN").lower()
        call_type = value.get("type", "UNKNOWN").replace("_", " ").lower()
        state = value.get("state", "UNKNOWN").lower()
        return kind, f"{direction} {call_type} ({state})"
    if kind == "groupCall":
        return kind, "Group call"
    if kind == "groupChange":
        parts = []
        for change in value.get("updates", []):
            inner, _ = pick_oneof(change, schema.oneof_fields(
                "signal.backup.GroupChangeChatUpdate.Update", "update"))
            if inner:
                parts.append(inner)
        return kind, ("Group updated: " + ", ".join(parts)) if parts else "Group updated"
    if kind == "threadMerge":
        return kind, "Conversations merged"
    if kind == "sessionSwitchover":
        return kind, "Session switched over"
    return kind or "unknown", (kind or "unknown")


def _body_text(item_kind: str | None, item: dict[str, Any]) -> tuple[str | None, list[Any]]:
    text = None
    if item_kind == "standardMessage":
        text = item.get("text")
    elif item_kind == "directStoryReplyMessage":
        reply = item.get("textReply")
        if reply:
            text = reply.get("text")
        elif item.get("emoji"):
            return item["emoji"], []
    if not text:
        return None, []
    return text.get("body"), text.get("bodyRanges", [])


def _body_ranges_json(ranges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for entry in ranges:
        rendered = {"start": entry.get("start", 0), "length": entry.get("length", 0)}
        if "style" in entry:
            rendered["style"] = entry["style"]
        if "mentionAci" in entry:
            rendered["mentionAci"] = _uuid_str(entry["mentionAci"])
        out.append(rendered)
    return out


def _send_status_json(statuses: Iterable[dict[str, Any]], index: Index,
                      schema: Schema) -> list[dict[str, Any]]:
    oneof = schema.oneof_fields("signal.backup.SendStatus", "deliveryStatus")
    out = []
    for status in statuses:
        state, detail = pick_oneof(status, oneof)
        entry = {
            "recipient": index.recipient_ref(status.get("recipientId")),
            "status": state or "pending",
            "timestamp": status.get("timestamp"),
        }
        if isinstance(detail, dict) and detail.get("reason"):
            entry["failureReason"] = detail["reason"]
        out.append(entry)
    return out


def message_to_json(item: dict[str, Any], index: Index, schema: Schema, *,
                    archive: Archive | None = None, include_raw: bool = False,
                    include_keys: bool = False) -> dict[str, Any]:
    """Render one ``ChatItem`` as a self-describing JSON object."""
    chat = index.chats.get(item.get("chatId", -1))
    author = index.recipient_ref(item.get("authorId"))
    direction, direction_detail = pick_oneof(
        item, schema.oneof_fields("signal.backup.ChatItem", "directionalDetails")
    )
    item_kind, payload = pick_oneof(item, schema.oneof_fields("signal.backup.ChatItem", "item"))
    payload = payload if isinstance(payload, dict) else {}
    direction_detail = direction_detail if isinstance(direction_detail, dict) else {}

    body, ranges = _body_text(item_kind, payload)
    record: dict[str, Any] = {
        "chatId": item.get("chatId"),
        "chat": chat.name if chat else None,
        "chatType": chat.kind if chat else None,
        "author": author,
        "direction": {"incoming": "incoming", "outgoing": "outgoing",
                      "directionless": "directionless"}.get(direction, "unknown"),
        "type": item_kind or "unknown",
        "dateSent": item.get("dateSent"),
        "dateSentIso": iso_timestamp(item.get("dateSent")),
    }

    received = direction_detail.get("dateReceived")
    if received:
        record["dateReceived"] = received
        record["dateReceivedIso"] = iso_timestamp(received)
    if direction == "incoming":
        record["read"] = bool(direction_detail.get("read"))
    if direction == "outgoing" and direction_detail.get("sendStatus"):
        record["sendStatus"] = _send_status_json(direction_detail["sendStatus"], index, schema)

    if body:
        record["body"] = body
    if ranges:
        record["bodyRanges"] = _body_ranges_json(ranges)

    if item_kind == "updateMessage":
        variant, summary = _update_summary(payload, schema,
                                           (author or {}).get("name", "Someone"))
        record["update"] = {"type": variant, "text": summary}
        record["type"] = "update"
    elif item_kind == "remoteDeletedMessage":
        record["deleted"] = True
    elif item_kind == "adminDeletedMessage":
        record["deleted"] = True
        record["deletedBy"] = index.recipient_ref(payload.get("adminId"))
    elif item_kind == "viewOnceMessage":
        record["viewOnce"] = True
    elif item_kind == "stickerMessage":
        sticker = payload.get("sticker") or {}
        record["sticker"] = {
            "emoji": sticker.get("emoji"),
            "packId": sticker.get("packId"),
            "stickerId": sticker.get("stickerId"),
        }
    elif item_kind == "poll":
        record["poll"] = _poll_json(payload, index)
    elif item_kind == "giftBadge":
        record["giftBadge"] = {"state": payload.get("state")}
    elif item_kind == "contactMessage":
        record["contact"] = _contact_card_json(payload.get("contact") or {})

    quote = payload.get("quote")
    if quote:
        quoted_text = (quote.get("text") or {}).get("body")
        record["quote"] = {
            "author": index.recipient_ref(quote.get("authorId")),
            "targetSentTimestamp": quote.get("targetSentTimestamp"),
            "targetSentIso": iso_timestamp(quote.get("targetSentTimestamp")),
            "type": quote.get("type", "NORMAL"),
            "body": quoted_text,
        }

    reactions = payload.get("reactions") or []
    if reactions:
        record["reactions"] = [
            {
                "emoji": reaction.get("emoji"),
                "author": index.recipient_ref(reaction.get("authorId")),
                "sentTimestamp": reaction.get("sentTimestamp"),
                "sentIso": iso_timestamp(reaction.get("sentTimestamp")),
            }
            for reaction in sorted(reactions, key=lambda r: r.get("sortOrder", 0))
        ]

    previews = payload.get("linkPreview") or []
    if previews:
        record["linkPreviews"] = [
            {key: preview.get(key) for key in ("url", "title", "description") if preview.get(key)}
            for preview in previews
        ]

    attachments = collect_attachments(item_kind, payload)
    if attachments:
        record["attachments"] = [
            ref.to_json(archive, include_keys=include_keys) for ref in attachments
        ]

    for key in ("expiresInMs", "expireStartDate"):
        if item.get(key):
            record[key] = item[key]
    if item.get("sms"):
        record["sms"] = True
    if item.get("pinDetails"):
        record["pinned"] = True

    revisions = item.get("revisions") or []
    if revisions:
        record["revisions"] = [
            message_to_json(revision, index, schema, archive=archive,
                            include_raw=include_raw, include_keys=include_keys)
            for revision in revisions
        ]
        record["edited"] = True

    if include_raw:
        record["raw"] = item

    return {key: value for key, value in record.items() if value is not None}


def _poll_json(poll: dict[str, Any], index: Index) -> dict[str, Any]:
    return {
        "question": poll.get("question"),
        "allowMultiple": bool(poll.get("allowMultiple")),
        "ended": bool(poll.get("hasEnded")),
        "options": [
            {
                "text": option.get("option"),
                "votes": [
                    index.recipient_ref(vote.get("voterId"))
                    for vote in option.get("votes", [])
                ],
            }
            for option in poll.get("options", [])
        ],
    }


def _contact_card_json(contact: dict[str, Any]) -> dict[str, Any]:
    name = contact.get("name") or {}
    return {
        "name": _join_name(name.get("givenName"), name.get("familyName")) or name.get("nickname"),
        "organization": contact.get("organization"),
        "phones": [entry.get("value") for entry in contact.get("number", [])],
        "emails": [entry.get("value") for entry in contact.get("email", [])],
    }


def message_attachments(item: dict[str, Any], schema: Schema) -> list[AttachmentRef]:
    """The attachment refs for a chat item, in the same order as ``to_json``."""
    item_kind, payload = pick_oneof(item, schema.oneof_fields("signal.backup.ChatItem", "item"))
    return collect_attachments(item_kind, payload if isinstance(payload, dict) else {})


def referenced_recipient_ids(record: Any) -> set[int]:
    """Every recipient id a rendered message record refers to, at any depth.

    Recipient references are rendered in one shape by :meth:`Index.recipient_ref`
    - a dict carrying an integer ``id`` and a string ``name`` - so collecting
    them structurally picks up reaction authors, quote authors, send-status
    recipients, poll voters, ``deletedBy`` and edit revisions alike, including
    any nested reference added later. The message record itself is keyed by
    ``chatId``, so it is never mistaken for one.
    """
    found: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            identifier = node.get("id")
            if isinstance(identifier, int) and isinstance(node.get("name"), str):
                found.add(identifier)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(record)
    return found
