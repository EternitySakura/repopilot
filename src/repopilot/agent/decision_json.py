"""Strict decision JSON and content-free diagnostics; never repair model output."""

import json
import math
from hashlib import sha256

from repopilot.agent.errors import ModelProviderError

PARSER_VERSION = "strict-json-v1"
MAX_DECISION_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32


class DecisionFormatError(ModelProviderError):
    def __init__(self, code: str, diagnostic: dict):
        super().__init__(code)
        self.diagnostic = diagnostic


def contains_credential(value, credential: str) -> bool:
    pending = [value]
    seen = set()
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if credential in item:
                return True
        elif isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            pending.extend(item)
            if isinstance(item, dict):
                pending.extend(item.values())
    return False


def response_diagnostic(response: dict) -> dict:
    """Only fixed labels, booleans, counts and hashes may leave this function."""
    result = {"version": 2, "parser_version": PARSER_VERSION}
    choices = response.get("choices")
    result["choice_count"] = len(choices) if isinstance(choices, list) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return result
    choice = choices[0]
    reason = choice.get("finish_reason")
    result["finish_reason"] = reason if reason in ("stop", "length", "tool_calls", "content_filter", None) else "other"
    message = choice.get("message")
    if not isinstance(message, dict):
        return result
    content = message.get("content")
    result["content_type"] = type(content).__name__ if type(content) in (str, dict, list, int, float, bool, type(None)) else "other"
    if not isinstance(content, str):
        return result
    result["content_chars"] = len(content)
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError:
        result["invalid_unicode"] = True
        return result
    result.update(content_bytes=len(raw), content_sha256=sha256(raw).hexdigest())
    stripped = content.lstrip(" \t\r\n")
    result.update(starts_object=stripped.startswith("{"), starts_array=stripped.startswith("["),
                  starts_fence=stripped.startswith("```"), starts_bom=stripped.startswith("\ufeff"),
                  empty=not stripped)
    return result


class _DuplicateKey(ValueError):
    pass


class _NonFinite(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonFinite
    return parsed


def _reject_constant(_value):
    raise _NonFinite


def parse_decision(content: str) -> dict:
    diagnostic = {"version": 2, "parser_version": PARSER_VERSION}

    def fail(code, kind):
        raise DecisionFormatError(code, {**diagnostic, "json_error_kind": kind})

    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError:
        fail("MODEL_DECISION_INVALID_UNICODE", "invalid_unicode")
    if len(raw) > MAX_DECISION_BYTES:
        fail("MODEL_DECISION_TOO_LARGE", "byte_limit")
    # Count containers outside quoted strings before recursive JSON decoding.
    depth = 0
    quoted = escaped = False
    for character in content:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                fail("MODEL_DECISION_TOO_DEEP", "depth_limit")
        elif character in "]}":
            depth -= 1
    try:
        decision = json.loads(content, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant, parse_float=_finite_float)
    except _DuplicateKey:
        fail("MODEL_DECISION_DUPLICATE_KEY", "duplicate_key")
    except _NonFinite:
        fail("MODEL_DECISION_NONFINITE", "nonfinite_number")
    except json.JSONDecodeError as exc:
        # Never store exc.doc, exc.msg or response excerpts; only a fixed category.
        kinds = {"Extra data": "trailing_content", "Expecting value": "expected_value",
                 "Expecting property name enclosed in double quotes": "property_name",
                 "Expecting ',' delimiter": "missing_comma", "Expecting ':' delimiter": "missing_colon"}
        kind = kinds.get(exc.msg, "invalid_json")
        for prefix, label in (("Invalid \\escape", "invalid_escape"), ("Invalid control character", "control_character"),
                              ("Unterminated string", "unterminated_string"), ("Unexpected UTF-8 BOM", "unexpected_bom")):
            if exc.msg.startswith(prefix):
                kind = label
        diagnostic.update(json_error_position=exc.pos, json_error_line=exc.lineno, json_error_column=exc.colno)
        if kind == "trailing_content":
            tail = content[exc.pos:].strip(" \t\r\n")
            labels = {"}": "closing_brace", "]": "closing_bracket", "{": "object_start", "[": "array_start"}
            diagnostic.update(json_trailing_chars=len(tail),
                json_trailing_kind="code_fence" if tail.startswith("```") else labels.get(tail[0], "other"))
        fail("MODEL_DECISION_NOT_JSON", kind)
    except RecursionError:
        fail("MODEL_DECISION_TOO_DEEP", "depth_limit")
    except ValueError:
        fail("MODEL_DECISION_NOT_JSON", "invalid_number")
    if not isinstance(decision, dict):
        fail("MODEL_DECISION_NOT_OBJECT", "non_object")
    pending = [decision]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                fail("MODEL_DECISION_INVALID_UNICODE", "invalid_unicode")
    return decision
