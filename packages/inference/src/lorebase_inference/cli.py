"""`lorebase-inference check` and `lorebase-inference embed`.

`check` probes the configured chat and embedding providers: it sends one tiny prompt to the
chat model and one short text to the embedding model, and prints the model, latency and vector
size. Exit status 0 means every probed provider answered correctly.

`embed TEXT` prints the vector of `TEXT` from the configured embedding model as JSON
(`{"model", "dims", "vector"}`): the query vector of semantic and hybrid search, computed on
the caller's side because Keeper and Herald never call a model (for example
`lorebase-inference embed "late deliveries" | lorebase search "late deliveries" --mode hybrid
--vector-file -`).

These are the only code paths of the package meant to reach the network on demand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lorebase_inference.chat import EndpointChatModel
from lorebase_inference.errors import InferenceError
from lorebase_inference.interfaces import ChatOptions, Message
from lorebase_inference.registry import create_chat_model, create_embedding_model
from lorebase_inference.settings import InferenceSettings, inference_settings
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, load_settings

SERVICE_NAME = "inference"
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
PROBE_MESSAGES = (
    Message("system", "You are a connectivity probe. Output JSON only."),
    Message("user", 'Set "ok" to true.'),
)
PROBE_TEXT = "connectivity probe"


@dataclass(slots=True)
class CheckResult:
    """Outcome of probing one component."""

    component: str
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "ok": self.ok,
            "details": self.details,
            "error": self.error,
        }

    def line(self) -> str:
        status = "ok" if self.ok else "FAILED"
        details = " ".join(f"{k}={v}" for k, v in self.details.items())
        return f"{self.component:<11}{status:<7}{details if self.ok else self.error}".rstrip()


async def check_chat(settings: InferenceSettings, task: str | None = None) -> CheckResult:
    """Ask the configured chat model for a trivial JSON answer."""
    llm = settings.llm.for_task(task)
    details: dict[str, Any] = {"provider": llm.provider, "model": llm.model}
    model = create_chat_model(settings, task)
    started = time.perf_counter()
    try:
        result = await model.complete_json(PROBE_MESSAGES, PROBE_SCHEMA, ChatOptions(task=task))
    except (InferenceError, ValueError) as exc:
        return CheckResult("chat", False, details, f"{type(exc).__name__}: {exc}")
    finally:
        if isinstance(model, EndpointChatModel):
            await model.aclose()
    details.update(
        model=result.model,
        latency_ms=round((time.perf_counter() - started) * 1000),
        tokens=f"{result.usage.input_tokens}/{result.usage.output_tokens}",
    )
    if isinstance(model, EndpointChatModel):
        details["structured_output"] = model.output_mode
    return CheckResult("chat", True, details)


async def check_embeddings(settings: InferenceSettings) -> CheckResult:
    """Embed a short text and compare the vector size with `embeddings.dims`."""
    embeddings = settings.embeddings
    details: dict[str, Any] = {"provider": embeddings.provider, "model": embeddings.model}
    model = create_embedding_model(settings)
    started = time.perf_counter()
    try:
        vectors = await model.embed([PROBE_TEXT])
    except (InferenceError, ValueError) as exc:
        return CheckResult("embeddings", False, details, f"{type(exc).__name__}: {exc}")
    finally:
        close = getattr(model, "aclose", None)
        if close is not None:
            await close()
    dims = len(vectors[0]) if vectors else 0
    details.update(dims=dims, latency_ms=round((time.perf_counter() - started) * 1000))
    if dims != embeddings.dims:
        return CheckResult(
            "embeddings",
            False,
            details,
            f"returned {dims} dimensions, embeddings.dims is {embeddings.dims}",
        )
    return CheckResult("embeddings", True, details)


async def run_checks(
    settings: InferenceSettings,
    *,
    task: str | None = None,
    chat: bool = True,
    embeddings: bool = True,
) -> list[CheckResult]:
    """Probe the selected components; a failure to even create a provider is a failed result."""
    results: list[CheckResult] = []
    probes = []
    if chat:
        probes.append(("chat", check_chat(settings, task)))
    if embeddings:
        probes.append(("embeddings", check_embeddings(settings)))
    for component, probe in probes:
        try:
            results.append(await probe)
        except (InferenceError, ValueError) as exc:
            results.append(CheckResult(component, False, error=f"{type(exc).__name__}: {exc}"))
    return results


async def embed_query(settings: InferenceSettings, text: str) -> dict[str, Any]:
    """`{"model", "dims", "vector"}` of `text`; `dims` is checked against `embeddings.dims`."""
    model = create_embedding_model(settings)
    try:
        vectors = await model.embed([text])
    finally:
        close = getattr(model, "aclose", None)
        if close is not None:
            await close()
    vector = vectors[0] if vectors else []
    if len(vector) != settings.embeddings.dims:
        raise InferenceError(
            f"returned {len(vector)} dimensions, embeddings.dims is {settings.embeddings.dims}"
        )
    return {"model": model.name, "dims": len(vector), "vector": vector}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lorebase-inference", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check", help="call the configured chat and embedding providers with a tiny prompt"
    )
    check.add_argument("--task", help="probe the chat model configured for this task name")
    check.add_argument("--no-chat", action="store_true", help="skip the chat probe")
    check.add_argument("--no-embeddings", action="store_true", help="skip the embeddings probe")
    check.add_argument("--json", action="store_true", help="print the results as JSON")
    embed = commands.add_parser(
        "embed", help="print the query vector of a text from the configured embedding model"
    )
    embed.add_argument("text", help="text to embed")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point; exits with status 1 when a probe fails or the configuration is invalid."""
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        inference = inference_settings(settings)
    except ConfigError as exc:
        sys.exit(str(exc))
    configure_logging(settings.logging, service=SERVICE_NAME)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request is noise here
    if args.command == "embed":
        try:
            print(json.dumps(asyncio.run(embed_query(inference, args.text))))
        except (InferenceError, ValueError) as exc:
            sys.exit(f"{type(exc).__name__}: {exc}")
        sys.exit(0)
    results = asyncio.run(
        run_checks(
            inference, task=args.task, chat=not args.no_chat, embeddings=not args.no_embeddings
        )
    )
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        for result in results:
            print(result.line())
    sys.exit(0 if results and all(r.ok for r in results) else 1)
