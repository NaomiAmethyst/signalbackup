"""A tiny, dependency-free proto3 schema parser and wire-format decoder.

Signal's backup schema (``backup.proto``) is vendored as a text file rather than
compiled, so the tool has no build step and the schema can be refreshed by
dropping in a newer ``.proto``.  Only the subset of proto3 that Signal actually
uses is supported: messages, nested messages, nested enums, ``oneof``,
``repeated``/``optional`` fields, and ``reserved`` declarations.  There are no
imports, maps, services, or field options to worry about.
"""

from __future__ import annotations

import base64
import re
import struct
from collections.abc import Iterable
from typing import Any

__all__ = ["ProtoError", "Schema", "parse_proto", "pick_oneof"]


class ProtoError(Exception):
    """Raised for malformed schemas or undecodable wire data."""


# Scalar field types and the wire type they use when not packed.
_VARINT_TYPES = {
    "int32", "int64", "uint32", "uint64", "sint32", "sint64", "bool",
}
_FIXED32_TYPES = {"fixed32", "sfixed32", "float"}
_FIXED64_TYPES = {"fixed64", "sfixed64", "double"}
_LEN_TYPES = {"string", "bytes"}
SCALAR_TYPES = _VARINT_TYPES | _FIXED32_TYPES | _FIXED64_TYPES | _LEN_TYPES

# Scalars that may appear in a packed repeated field.
_PACKABLE = _VARINT_TYPES | _FIXED32_TYPES | _FIXED64_TYPES


class FieldDef:
    __slots__ = ("name", "number", "oneof", "optional", "repeated", "type_name")

    def __init__(self, name: str, number: int, type_name: str,
                 repeated: bool = False, optional: bool = False,
                 oneof: str | None = None) -> None:
        self.name = name
        self.number = number
        self.type_name = type_name
        self.repeated = repeated
        self.optional = optional
        self.oneof = oneof

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FieldDef {self.type_name} {self.name}={self.number}>"


class EnumType:
    __slots__ = ("by_name", "by_number", "full_name")

    def __init__(self, full_name: str) -> None:
        self.full_name = full_name
        self.by_number: dict[int, str] = {}
        self.by_name: dict[str, int] = {}


class MessageType:
    __slots__ = ("by_name", "by_number", "full_name", "oneofs")

    def __init__(self, full_name: str) -> None:
        self.full_name = full_name
        self.by_number: dict[int, FieldDef] = {}
        self.by_name: dict[str, FieldDef] = {}
        self.oneofs: dict[str, list[str]] = {}

    def add(self, field: FieldDef) -> None:
        self.by_number[field.number] = field
        self.by_name[field.name] = field
        if field.oneof is not None:
            self.oneofs.setdefault(field.oneof, []).append(field.name)


# --------------------------------------------------------------------------
# Lexing / parsing
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
      (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    | (?P<number>-?\d+)
    | (?P<string>"[^"]*"|'[^']*')
    | (?P<punct>[{}()\[\];=,<>])
""", re.VERBOSE)


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments, leaving string literals intact."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            quote = c
            j = i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:min(j + 1, n)])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(_strip_comments(text)):
        tokens.append(match.group(0))
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str:
        if self.pos >= len(self.tokens):
            raise ProtoError("unexpected end of .proto input")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, token: str) -> None:
        got = self.next()
        if got != token:
            raise ProtoError(f"expected {token!r}, got {got!r}")

    def skip_to_semicolon(self) -> None:
        while True:
            token = self.next()
            if token == ";":
                return
            if token == "{":  # e.g. an option block
                depth = 1
                while depth:
                    inner = self.next()
                    depth += (inner == "{") - (inner == "}")


def parse_proto(text: str, schema: Schema | None = None) -> Schema:
    """Parse proto3 source into a :class:`Schema`."""
    schema = schema or Schema()
    parser = _Parser(_tokenize(text))
    package = ""

    while parser.peek() is not None:
        token = parser.next()
        if token == ";":
            continue
        if token == "syntax":
            parser.skip_to_semicolon()
        elif token == "package":
            package = parser.next()
            parser.expect(";")
        elif token in ("option", "import"):
            parser.skip_to_semicolon()
        elif token == "message":
            _parse_message(parser, schema, package)
        elif token == "enum":
            _parse_enum(parser, schema, package)
        else:
            raise ProtoError(f"unsupported top-level declaration {token!r}")
    return schema


def _qualify(scope: str, name: str) -> str:
    return f"{scope}.{name}" if scope else name


def _parse_message(parser: _Parser, schema: Schema, scope: str) -> None:
    full_name = _qualify(scope, parser.next())
    message = MessageType(full_name)
    schema.messages[full_name] = message
    parser.expect("{")

    while True:
        token = parser.next()
        if token == "}":
            return
        if token == ";":
            continue
        if token == "message":
            _parse_message(parser, schema, full_name)
        elif token == "enum":
            _parse_enum(parser, schema, full_name)
        elif token == "oneof":
            oneof_name = parser.next()
            parser.expect("{")
            while True:
                inner = parser.next()
                if inner == "}":
                    break
                if inner == ";":
                    continue
                if inner == "option":
                    parser.skip_to_semicolon()
                    continue
                message.add(_parse_field(parser, inner, oneof=oneof_name))
        elif token in ("reserved", "option", "extensions"):
            parser.skip_to_semicolon()
        else:
            message.add(_parse_field(parser, token))


def _parse_field(parser: _Parser, first: str, oneof: str | None = None) -> FieldDef:
    repeated = optional = False
    type_name = first
    if first in ("repeated", "optional", "required"):
        repeated = first == "repeated"
        optional = first == "optional"
        type_name = parser.next()
    if type_name == "map":
        raise ProtoError("map fields are not supported")

    name = parser.next()
    parser.expect("=")
    number = int(parser.next())
    if parser.peek() == "[":  # field options; ignored
        while parser.next() != "]":
            pass
    parser.expect(";")
    return FieldDef(name, number, type_name, repeated, optional, oneof)


def _parse_enum(parser: _Parser, schema: Schema, scope: str) -> None:
    full_name = _qualify(scope, parser.next())
    enum = EnumType(full_name)
    schema.enums[full_name] = enum
    parser.expect("{")

    while True:
        token = parser.next()
        if token == "}":
            return
        if token == ";":
            continue
        if token in ("option", "reserved"):
            parser.skip_to_semicolon()
            continue
        parser.expect("=")
        value = int(parser.next())
        if parser.peek() == "[":
            while parser.next() != "]":
                pass
        parser.expect(";")
        enum.by_number.setdefault(value, token)
        enum.by_name[token] = value


# --------------------------------------------------------------------------
# Wire decoding
# --------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ProtoError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")


def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _as_signed(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


class Schema:
    """A collection of message and enum types, keyed by fully-qualified name."""

    def __init__(self) -> None:
        self.messages: dict[str, MessageType] = {}
        self.enums: dict[str, EnumType] = {}

    # -- type resolution ---------------------------------------------------

    def resolve(self, name: str, scope: str) -> str | None:
        """Resolve a type reference the way protoc does: innermost scope first."""
        if name.startswith("."):
            name = name[1:]
            return name if name in self.messages or name in self.enums else None
        parts = scope.split(".") if scope else []
        while True:
            candidate = _qualify(".".join(parts), name)
            if candidate in self.messages or candidate in self.enums:
                return candidate
            if not parts:
                return None
            parts.pop()

    def message(self, full_name: str) -> MessageType:
        try:
            return self.messages[full_name]
        except KeyError:
            raise ProtoError(f"unknown message type {full_name!r}") from None

    def oneof_fields(self, full_name: str, oneof: str) -> list[str]:
        return self.message(full_name).oneofs.get(oneof, [])

    # -- decoding ----------------------------------------------------------

    def decode(self, data: bytes, type_name: str, *, bytes_as: str = "base64",
               keep_unknown: bool = False) -> dict[str, Any]:
        """Decode ``data`` as an instance of ``type_name`` into a plain dict.

        Fields absent from the wire are absent from the result, so proto3
        implicit defaults are simply left out rather than materialised.
        """
        return self._decode(data, self.message(type_name), bytes_as, keep_unknown)

    def _decode(self, data: bytes, message: MessageType, bytes_as: str,
                keep_unknown: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        pos, end = 0, len(data)

        while pos < end:
            tag, pos = _read_varint(data, pos)
            number, wire_type = tag >> 3, tag & 7
            if number == 0:
                raise ProtoError("invalid field number 0")

            if wire_type == 0:
                raw, pos = _read_varint(data, pos)
            elif wire_type == 1:
                raw, pos = data[pos:pos + 8], pos + 8
            elif wire_type == 2:
                length, pos = _read_varint(data, pos)
                if pos + length > end:
                    raise ProtoError("truncated length-delimited field")
                raw, pos = data[pos:pos + length], pos + length
            elif wire_type == 5:
                raw, pos = data[pos:pos + 4], pos + 4
            else:
                raise ProtoError(f"unsupported wire type {wire_type}")

            field = message.by_number.get(number)
            if field is None:
                if keep_unknown:
                    unknown = out.setdefault("_unknown", {})
                    unknown.setdefault(str(number), []).append(
                        base64.b64encode(raw).decode() if isinstance(raw, bytes) else raw
                    )
                continue

            values = self._interpret(field, raw, wire_type, message.full_name,
                                     bytes_as, keep_unknown)
            if field.repeated:
                out.setdefault(field.name, []).extend(values)
            else:
                out[field.name] = values[-1]

        return out

    def _interpret(self, field: FieldDef, raw: Any, wire_type: int, scope: str,
                   bytes_as: str, keep_unknown: bool) -> list[Any]:
        kind = field.type_name

        # A packed repeated scalar arrives as one length-delimited blob.
        if field.repeated and kind in _PACKABLE and wire_type == 2:
            return self._unpack(kind, raw)

        if kind in _VARINT_TYPES:
            if kind == "bool":
                return [bool(raw)]
            if kind in ("sint32", "sint64"):
                return [_zigzag(raw)]
            if kind in ("int32", "int64"):
                return [_as_signed(raw)]
            return [raw]
        if kind in _FIXED32_TYPES:
            fmt = {"fixed32": "<I", "sfixed32": "<i", "float": "<f"}[kind]
            return [struct.unpack(fmt, raw)[0]]
        if kind in _FIXED64_TYPES:
            fmt = {"fixed64": "<Q", "sfixed64": "<q", "double": "<d"}[kind]
            return [struct.unpack(fmt, raw)[0]]
        if kind == "string":
            return [raw.decode("utf-8", errors="replace")]
        if kind == "bytes":
            return [_encode_bytes(raw, bytes_as)]

        resolved = self.resolve(kind, scope)
        if resolved is None:
            raise ProtoError(f"unknown type {kind!r} referenced from {scope!r}")
        if resolved in self.enums:
            enum = self.enums[resolved]
            return [enum.by_number.get(raw, raw)]
        return [self._decode(raw, self.messages[resolved], bytes_as, keep_unknown)]

    def _unpack(self, kind: str, blob: bytes) -> list[Any]:
        values: list[Any] = []
        if kind in _VARINT_TYPES:
            pos = 0
            while pos < len(blob):
                raw, pos = _read_varint(blob, pos)
                if kind == "bool":
                    values.append(bool(raw))
                elif kind in ("sint32", "sint64"):
                    values.append(_zigzag(raw))
                elif kind in ("int32", "int64"):
                    values.append(_as_signed(raw))
                else:
                    values.append(raw)
        elif kind in _FIXED32_TYPES:
            fmt = {"fixed32": "<I", "sfixed32": "<i", "float": "<f"}[kind]
            values = [struct.unpack_from(fmt, blob, i)[0] for i in range(0, len(blob), 4)]
        else:
            fmt = {"fixed64": "<Q", "sfixed64": "<q", "double": "<d"}[kind]
            values = [struct.unpack_from(fmt, blob, i)[0] for i in range(0, len(blob), 8)]
        return values


def _encode_bytes(raw: bytes, bytes_as: str) -> str:
    if bytes_as == "hex":
        return raw.hex()
    if bytes_as == "base64":
        return base64.b64encode(raw).decode("ascii")
    raise ProtoError(f"unknown bytes encoding {bytes_as!r}")


def pick_oneof(value: dict[str, Any], names: Iterable[str]) -> tuple[str, Any] | tuple[None, None]:
    """Return the (name, value) of whichever member of a oneof is present."""
    for name in names:
        if name in value:
            return name, value[name]
    return None, None


# --------------------------------------------------------------------------
# Wire encoding
#
# Only needed to build test fixtures and to round-trip decoded frames, but it
# keeps the schema honest: anything this module can decode it can also rebuild.
# --------------------------------------------------------------------------

def _write_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _zigzag_encode(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _decode_bytes(value: Any, bytes_as: str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if bytes_as == "hex":
        return bytes.fromhex(value)
    return base64.b64decode(value)


def _encode_field(schema: Schema, field: FieldDef, value: Any, scope: str,
                  bytes_as: str) -> bytes:
    kind = field.type_name
    tag_num = field.number

    def tagged(wire_type: int, payload: bytes) -> bytes:
        return _write_varint((tag_num << 3) | wire_type) + payload

    if kind in _VARINT_TYPES:
        if kind == "bool":
            return tagged(0, _write_varint(1 if value else 0))
        if kind in ("sint32", "sint64"):
            return tagged(0, _write_varint(_zigzag_encode(int(value))))
        return tagged(0, _write_varint(int(value)))
    if kind in _FIXED32_TYPES:
        fmt = {"fixed32": "<I", "sfixed32": "<i", "float": "<f"}[kind]
        return tagged(5, struct.pack(fmt, value))
    if kind in _FIXED64_TYPES:
        fmt = {"fixed64": "<Q", "sfixed64": "<q", "double": "<d"}[kind]
        return tagged(1, struct.pack(fmt, value))
    if kind == "string":
        payload = str(value).encode("utf-8")
        return tagged(2, _write_varint(len(payload)) + payload)
    if kind == "bytes":
        payload = _decode_bytes(value, bytes_as)
        return tagged(2, _write_varint(len(payload)) + payload)

    resolved = schema.resolve(kind, scope)
    if resolved is None:
        raise ProtoError(f"unknown type {kind!r} referenced from {scope!r}")
    if resolved in schema.enums:
        enum = schema.enums[resolved]
        number = value if isinstance(value, int) else enum.by_name.get(value)
        if number is None:
            raise ProtoError(f"unknown enum value {value!r} for {resolved}")
        return tagged(0, _write_varint(number))
    payload = schema.encode(value, resolved, bytes_as=bytes_as)
    return tagged(2, _write_varint(len(payload)) + payload)


def _schema_encode(self: Schema, value: dict[str, Any], type_name: str, *,
                   bytes_as: str = "base64") -> bytes:
    """Serialise a dict (as produced by :meth:`Schema.decode`) back to wire bytes."""
    message = self.message(type_name)
    out = bytearray()
    for name, field_value in value.items():
        if name == "_unknown":
            continue
        field = message.by_name.get(name)
        if field is None:
            raise ProtoError(f"{type_name} has no field {name!r}")
        items = field_value if field.repeated else [field_value]
        for item in items:
            out += _encode_field(self, field, item, message.full_name, bytes_as)
    return bytes(out)


Schema.encode = _schema_encode  # type: ignore[attr-defined]


def length_delimited(records: Iterable[bytes]) -> bytes:
    """Frame a sequence of protobuf messages the way Signal's streams do."""
    out = bytearray()
    for record in records:
        out += _write_varint(len(record)) + record
    return bytes(out)
