"""Allowlisted content-free metadata; response bodies are never diagnostic fields."""

from typing import Literal

from pydantic import Field, model_validator

from repopilot.contracts.base import Contract

DIAGNOSTIC_POLICY = "metadata-only-v2"
DIAGNOSTIC_VERSIONS = {"metadata-only-v1": 1, "metadata-only-v2": 2}


class ResponseDiagnostic(Contract):
    version: Literal[1, 2]
    parser_version: Literal["strict-json-v1"]
    choice_count: int | None = Field(default=None, ge=0)
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "other"] | None = None
    content_type: Literal["str", "dict", "list", "int", "float", "bool", "NoneType", "other"] | None = None
    content_chars: int | None = Field(default=None, ge=0)
    content_bytes: int | None = Field(default=None, ge=0)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    invalid_unicode: bool | None = None
    starts_object: bool | None = None
    starts_array: bool | None = None
    starts_fence: bool | None = None
    starts_bom: bool | None = None
    empty: bool | None = None
    json_error_position: int | None = Field(default=None, ge=0)
    json_error_line: int | None = Field(default=None, ge=1)
    json_error_column: int | None = Field(default=None, ge=1)
    json_error_kind: Literal[
        "invalid_unicode", "byte_limit", "depth_limit", "duplicate_key", "nonfinite_number",
        "trailing_content", "expected_value", "property_name", "missing_comma", "missing_colon",
        "invalid_json", "invalid_escape", "control_character", "unterminated_string", "unexpected_bom",
        "invalid_number", "non_object",
    ] | None = None
    json_trailing_chars: int | None = Field(default=None, ge=1)
    json_trailing_kind: Literal[
        "closing_brace", "closing_bracket", "object_start", "array_start", "code_fence", "other",
    ] | None = None

    @model_validator(mode="after")
    def versioned_tail_metadata(self):
        if self.version == 1 and (self.json_trailing_chars is not None or self.json_trailing_kind is not None):
            raise ValueError("Trailing-content metadata requires diagnostic version 2")
        return self
