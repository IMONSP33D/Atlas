"""Request/response shapes from docs/api-contract.md.

Pydantic handles types and unknown fields (SCHEMA_INVALID). Everything that has
its own catalog code is checked in validation.py so the agent gets the specific
code, not a generic schema error.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    # strict: no "60" -> 60 or "yes" -> True coercion; the agent sends typed JSON.
    model_config = ConfigDict(extra="forbid", strict=True)


class FileIn(_Strict):
    name: str
    content_b64: str
    sha256: str | None = None


class ScreenshotSpec(_Strict):
    on_complete: bool = False
    on_error: bool = False
    source: Literal["guest", "hypervisor", "auto"] = "auto"

    @property
    def requested(self) -> bool:
        return self.on_complete or self.on_error


class ExecuteRequest(_Strict):
    runtime: str
    version: str | None = None
    mode: str = "execute"
    session_id: str | None = None
    language: str
    code: str | None = None
    entrypoint: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    stdin: str | None = None
    timeout_sec: int | None = None
    network: bool | dict[str, Any] = False
    files: list[FileIn] = Field(default_factory=list)
    screenshot: ScreenshotSpec = Field(default_factory=ScreenshotSpec)
    request_id: str | None = None


class OpenSessionRequest(_Strict):
    runtime: str
    version: str | None = None
    network: bool | dict[str, Any] = False
    request_id: str | None = None


class ScreenshotRequest(_Strict):
    source: Literal["guest", "hypervisor", "auto"] = "auto"
    window_title: str | None = None
    region: list[int] | None = None


class CapeOptions(_Strict):
    package: str | None = None
    timeout_sec: int = 200
    options: str = ""
    tags: str = ""


class CapeSubmitRequest(_Strict):
    file: FileIn
    version: str | None = None
    network: bool | dict[str, Any] = False
    cape_options: CapeOptions = Field(default_factory=CapeOptions)
    callback_url: str | None = None
    request_id: str | None = None
