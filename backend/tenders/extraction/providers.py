"""Extractor implementations. Both return raw, schema-valid data; grounding happens later."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import anthropic
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import ValidationError

from .budget import CallMeter, StopRun, estimate_tokens, is_priced
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, Notice
from .schema import ExtractionData

if TYPE_CHECKING:
    from tenders.models import Tender

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
OUTPUT_SCHEMA = anthropic.transform_schema(ExtractionData)
MAX_ATTEMPTS = 2  # every API attempt and the validation retry share this budget
RETRYABLE_STATUS = frozenset({408, 409, 429})  # plus any 5xx
RETRY_MESSAGE = (
    "Your previous output failed schema validation:\n{errors}\n"
    "Return the complete JSON again, fixed. Same rules as before."
)
# Errors that will fail every notice the same way: stop the run instead.
FATAL_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError)


@dataclass
class ExtractionResult:
    status: Literal["ok", "failed"]
    data: ExtractionData | None = None
    error: str = ""
    flags: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, data: ExtractionData, flags: list[str] | None = None) -> ExtractionResult:
        return cls("ok", data, "", list(flags or []))

    @classmethod
    def failed(cls, error: str, flags: list[str] | None = None) -> ExtractionResult:
        return cls("failed", None, error, list(flags or []))


class Extractor(Protocol):
    model_id: str
    fingerprint: str  # changes whenever anything that shapes the output changes

    def extract(self, notice: Notice, meter: CallMeter,
                tender: Tender | None = None) -> ExtractionResult: ...


def config_fingerprint(**parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


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
        self.fingerprint = config_fingerprint(model=self.model_id, system=SYSTEM_PROMPT,
                                              schema=OUTPUT_SCHEMA)

    def extract(self, notice: Notice, meter: CallMeter,
                tender: Tender | None = None) -> ExtractionResult:
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
    # max_retries=0: AnthropicExtractor owns every attempt, so each one is logged and capped.
    return anthropic.Anthropic(timeout=settings.LLM_TIMEOUT_S, max_retries=0)


def _is_retryable(e: anthropic.APIStatusError) -> bool:
    return e.status_code in RETRYABLE_STATUS or e.status_code >= 500


class AnthropicExtractor:
    """Messages API with native structured outputs (``output_config.format`` json_schema)."""

    def __init__(self, client: anthropic.Anthropic | None = None, *, model_id: str | None = None,
                 max_tokens: int | None = None, effort: str | None = None,
                 system_prompt: str = SYSTEM_PROMPT, retry_wait_s: float = 2.0):
        self.model_id = model_id or settings.LLM_MODEL
        if not is_priced(self.model_id):
            raise ImproperlyConfigured(
                f"No price for {self.model_id!r} in tenders.extraction.budget.PRICES; "
                "the cost cap can't work without one.")
        self.client = client if client is not None else _default_client()
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self.system_prompt = system_prompt
        self.retry_wait_s = retry_wait_s
        effort = effort or settings.LLM_EFFORT
        self.output_config = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                              "effort": effort}
        self.fingerprint = config_fingerprint(
            model=self.model_id, system=self.system_prompt, schema=OUTPUT_SCHEMA,
            effort=effort, max_tokens=self.max_tokens)

    def _request_chars(self, messages: list[dict]) -> int:
        return (len(self.system_prompt) + len(json.dumps(OUTPUT_SCHEMA))
                + sum(len(m["content"]) for m in messages))

    def _record(self, meter: CallMeter, tender: Tender | None, status: str, t0: float, *,
                resp=None, request_id: str = "", worst_case_chars: int | None = None) -> None:
        if worst_case_chars is not None:
            # Timeout / connection error: we can't know what was billed. Assume the worst.
            input_tokens, output_tokens = estimate_tokens(worst_case_chars), self.max_tokens
        else:
            usage = getattr(resp, "usage", None)
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
        meter.record(
            tender=tender, model_id=self.model_id, prompt_version=PROMPT_VERSION,
            input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=int((time.monotonic() - t0) * 1000), status=status,
            request_id=(getattr(resp, "_request_id", None) or request_id or ""),
            estimated=worst_case_chars is not None,
        )

    def _wait(self) -> None:
        if self.retry_wait_s > 0:
            time.sleep(self.retry_wait_s)

    def extract(self, notice: Notice, meter: CallMeter,
                tender: Tender | None = None) -> ExtractionResult:
        messages: list[dict] = [{"role": "user", "content": notice.text}]
        flags: list[str] = []
        failure = ExtractionResult.failed("no attempt made")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            meter.check()
            first_call_of_run = meter.api_calls == 0
            t0 = time.monotonic()
            try:
                resp = self.client.messages.create(
                    model=self.model_id,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    messages=messages,
                    output_config=self.output_config,
                )
            except anthropic.APIConnectionError as e:  # includes APITimeoutError
                self._record(meter, tender, "error", t0,
                             worst_case_chars=self._request_chars(messages))
                failure = ExtractionResult.failed(f"api_error: {type(e).__name__}",
                                                  flags=[*flags, "api_error"])
                if not last:
                    flags.append("api_retry")
                    self._wait()
                continue
            except FATAL_ERRORS as e:
                self._record(meter, tender, "error", t0, request_id=e.request_id or "")
                raise StopRun(f"{type(e).__name__} ({e.status_code}): check the API key "
                              "and LLM_MODEL") from e
            except anthropic.BadRequestError as e:
                self._record(meter, tender, "error", t0, request_id=e.request_id or "")
                if first_call_of_run:
                    raise StopRun("BadRequestError (400) on the first call of the run: "
                                  "check the LLM_* settings") from e
                return ExtractionResult.failed("api_error: BadRequestError (400)",
                                               flags=[*flags, "api_error"])
            except anthropic.APIStatusError as e:
                self._record(meter, tender, "error", t0, request_id=e.request_id or "")
                failure = ExtractionResult.failed(
                    f"api_error: {type(e).__name__} ({e.status_code})", flags=[*flags, "api_error"])
                if _is_retryable(e) and not last:
                    flags.append("api_retry")
                    self._wait()
                    continue
                return failure
            except anthropic.APIError as e:  # e.g. a response the SDK couldn't parse
                self._record(meter, tender, "error", t0)
                return ExtractionResult.failed(f"api_error: {type(e).__name__}",
                                               flags=[*flags, "api_error"])

            if resp.stop_reason == "refusal":
                self._record(meter, tender, "refusal", t0, resp=resp)
                return ExtractionResult.failed("refusal", flags=[*flags, "refusal"])

            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                data = ExtractionData.model_validate_json(text)
            except ValidationError as e:
                self._record(meter, tender, "invalid", t0, resp=resp)
                errors = describe_validation_error(e)
                if resp.stop_reason == "max_tokens":
                    errors = "- output was cut off at max_tokens; be more concise\n" + errors
                failure = ExtractionResult.failed(f"schema_invalid:\n{errors}",
                                                  flags=[*flags, "schema_invalid"])
                if last:
                    return failure
                flags.append("validation_retry")
                note = RETRY_MESSAGE.format(errors=errors)
                if text.strip():
                    # Replay only the text: never thinking blocks.
                    messages = [messages[0], {"role": "assistant", "content": text},
                                {"role": "user", "content": note}]
                else:
                    # Nothing but thinking came back: start fresh with the error note.
                    messages = [{"role": "user", "content": f"{notice.text}\n\n{note}"}]
                continue

            self._record(meter, tender, "ok", t0, resp=resp)
            return ExtractionResult.ok(data, flags)
        return failure
