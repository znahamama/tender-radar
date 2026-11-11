"""Extractor implementations. Both return raw, schema-valid data; grounding happens later."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import anthropic
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import ValidationError

from .budget import CallMeter, is_priced
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, Notice
from .schema import ExtractionData

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
RETRY_MESSAGE = (
    "Your previous output failed schema validation:\n{errors}\n"
    "Return the complete JSON again, fixed. Same rules as before."
)
# Errors that will fail every notice the same way: stop the run instead of marking each failed.
FATAL_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError)


@dataclass
class ExtractionResult:
    status: str  # "ok" | "failed"
    data: ExtractionData | None = None
    error: str = ""
    flags: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, data: ExtractionData) -> ExtractionResult:
        return cls("ok", data)

    @classmethod
    def failed(cls, error: str, flags: list[str] | None = None) -> ExtractionResult:
        return cls("failed", None, error, flags or [])


class Extractor(Protocol):
    model_id: str

    def extract(self, notice: Notice, meter: CallMeter, tender=None) -> ExtractionResult: ...


def describe_validation_error(e: ValidationError, limit: int = 20) -> str:
    """Field paths and messages only, never the offending input (it may quote the notice)."""
    errs = e.errors(include_input=False, include_url=False)
    lines = [f"- {'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}" for err in errs[:limit]]
    if len(errs) > limit:
        lines.append(f"- ... and {len(errs) - limit} more")
    return "\n".join(lines)


class FakeExtractor:
    """Returns saved JSON fixtures: ``<reference>.json`` if present, else ``default.json``."""

    model_id = "fake"

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR):
        self.fixtures_dir = Path(fixtures_dir)

    def extract(self, notice: Notice, meter: CallMeter, tender=None) -> ExtractionResult:
        meter.check()
        meter.count_fake()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", notice.reference)
        path = self.fixtures_dir / f"{name}.json"
        if not path.is_file():
            path = self.fixtures_dir / "default.json"
        try:
            return ExtractionResult.ok(ExtractionData.model_validate_json(path.read_text()))
        except ValidationError as e:
            return ExtractionResult.failed(f"schema_invalid:\n{describe_validation_error(e)}")


def _default_client() -> anthropic.Anthropic:
    # Credentials come from ANTHROPIC_API_KEY in the environment; never passed explicitly here.
    return anthropic.Anthropic(timeout=settings.LLM_TIMEOUT_S, max_retries=settings.LLM_MAX_RETRIES)


class AnthropicExtractor:
    """Messages API with native structured outputs (``output_config.format`` json_schema)."""

    def __init__(self, client=None, *, model_id: str | None = None,
                 max_tokens: int | None = None, effort: str | None = None):
        self.model_id = model_id or settings.LLM_MODEL
        if not is_priced(self.model_id):
            raise ImproperlyConfigured(
                f"No price for {self.model_id!r} in tenders.extraction.budget.PRICES; "
                "the cost cap can't work without one.")
        self.client = client if client is not None else _default_client()
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self.output_config = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(ExtractionData)},
            "effort": effort or settings.LLM_EFFORT,
        }

    def _record(self, meter, tender, status, t0, resp=None, request_id=""):
        usage = getattr(resp, "usage", None)
        meter.record(
            tender=tender, model_id=self.model_id, prompt_version=PROMPT_VERSION,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - t0) * 1000), status=status,
            request_id=(getattr(resp, "_request_id", None) or request_id or ""),
        )

    def extract(self, notice: Notice, meter: CallMeter, tender=None) -> ExtractionResult:
        messages: list[dict] = [{"role": "user", "content": notice.text}]
        for attempt in (1, 2):
            meter.check()
            t0 = time.monotonic()
            try:
                resp = self.client.messages.create(
                    model=self.model_id,
                    max_tokens=self.max_tokens,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    output_config=self.output_config,
                )
            except anthropic.APIError as e:
                self._record(meter, tender, "error", t0,
                             request_id=getattr(e, "request_id", None) or "")
                if isinstance(e, FATAL_ERRORS):
                    raise
                code = getattr(e, "status_code", None)
                return ExtractionResult.failed(
                    f"api_error: {type(e).__name__}" + (f" ({code})" if code else ""),
                    flags=["api_error"])

            if resp.stop_reason == "refusal":
                self._record(meter, tender, "refusal", t0, resp)
                return ExtractionResult.failed("refusal", flags=["refusal"])

            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                data = ExtractionData.model_validate_json(text)
            except ValidationError as e:
                self._record(meter, tender, "invalid", t0, resp)
                errors = describe_validation_error(e)
                if resp.stop_reason == "max_tokens":
                    errors = "- output was cut off at max_tokens; be more concise\n" + errors
                if attempt == 2:
                    return ExtractionResult.failed(f"schema_invalid after retry:\n{errors}",
                                                   flags=["schema_invalid"])
                messages = [
                    *messages,
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": RETRY_MESSAGE.format(errors=errors)},
                ]
                continue

            self._record(meter, tender, "ok", t0, resp)
            result = ExtractionResult.ok(data)
            if attempt == 2:
                result.flags.append("validation_retry")
            return result
        raise AssertionError("unreachable")
