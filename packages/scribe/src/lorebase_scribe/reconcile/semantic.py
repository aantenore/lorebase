"""The semantic stage of reconciliation (design 10.6, stage d), optional and off by default.

It runs when `semantic.enabled` is set, the stage is listed, and an embedding provider is
configured (`inference.embeddings`, 9.4); otherwise nothing here is built, or the stage reviews
nothing (logged once per process). It only adds signals and filters; it never decides.

- **Competing `text` values.** The distinct values of a `contradiction` or `superseded` draft
  are embedded with the process's embedding model; the least cosine similarity of two of them
  is the `similarity` signal. At `semantic.equivalent_similarity` or more the draft is marked
  `equivalent` (near-paraphrases, ranked last); at `semantic.equivalence_threshold` or more
  (when set; null raises every draft) it is dropped and counted `filtered`.
- **Duplicates** of Kinds with an embedding template compare the Record vectors the embedding
  job stored (9.3), under the template fingerprint of the read branch's schema, without any
  model call: a pair at `semantic.duplicate_threshold` or more raises a `duplicate` with the
  `semantic` rule and its similarity.
- **Judge** (`semantic.judge`, off). The chat model (task `reconciliation`) is asked whether
  the two least similar values contradict; the answer is cached in the extraction cache (when
  `scribe.extraction.cache` is on) and the stage stops asking once it used
  `scribe.extraction.max_tokens_per_job` tokens. It adds a `judge` signal (label and score).

A model that fails or is not available gives no signal: the drafts go on unchanged and
detection is never blocked. One stage is built per job, so the token budget is a job's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Sequence
from itertools import combinations
from typing import Any

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind
from lorebase_core.reconcile import (
    CandidateDraft,
    DuplicateRule,
    ReconcileOptions,
    Signal,
    duplicate_draft,
)
from lorebase_inference import ChatModel, ChatOptions, EmbeddingModel, Message, Usage
from lorebase_scribe.embeddings import EmbeddingModels
from lorebase_scribe.extraction.tier2 import AnswerCache
from lorebase_scribe.reconcile.detector import Raised
from lorebase_store.embeddings import similar_vectors, template_fingerprints

log = logging.getLogger("lorebase.scribe.reconcile")

JUDGE_TASK = "reconciliation"
"""Task name of the judge's chat model (`inference.llm.tasks` may route it)."""

JUDGE_PROMPT = (
    "You compare two statements about the same attribute of the same record, taken from "
    "different sources. Say whether they contradict each other (both cannot be true at once) "
    "and how sure you are, from 0 to 1. Paraphrases, added detail and different wording of the "
    "same fact do not contradict."
)
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contradicts": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["contradicts", "score"],
    "additionalProperties": False,
}
_CACHE_VERSION = 1

_logged: set[str] = set()


def _log_once(key: str, message: str, *args: object) -> None:
    """Log `message` once per process for `key` (a stage that cannot run is not news twice)."""
    if key not in _logged:
        _logged.add(key)
        log.warning(message, *args)


def cosine(first: Sequence[float], second: Sequence[float]) -> float:
    """Cosine similarity of two vectors; 0 when either is null."""
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    norm = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return 0.0 if norm == 0 else dot / norm


def _texts(draft: CandidateDraft) -> list[str]:
    """The distinct text values of the draft's claims, in claim order."""
    seen: dict[str, str] = {}
    for claim in draft.claims:
        if isinstance(claim.value, str) and claim.value_hash not in seen:
            seen[claim.value_hash] = claim.value
    return list(seen.values())


def judge_key(model: str, prompt_version: str, first: str, second: str) -> bytes:
    """Extraction cache key of one judge question (the order of the texts does not matter)."""
    parts: list[Any] = [
        "reconcile-judge",
        _CACHE_VERSION,
        model,
        prompt_version,
        sorted([first, second]),
    ]
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).digest()


class EmbeddingSemanticStage:
    """Stage d over the process's embedding model and, for the judge, a chat model (module
    doc). `chat` creates the judge's model on first use; `chat_model` names it in cache keys."""

    def __init__(
        self,
        models: EmbeddingModels,
        *,
        chat: Callable[[], ChatModel] | None = None,
        chat_model: str = "",
        cache: AnswerCache | None = None,
        prompt_version: str = "",
        max_tokens: int = 0,
    ) -> None:
        self._models = models
        self._chat_factory = chat
        self._chat: ChatModel | None = None
        self._chat_model = chat_model
        self._cache = cache
        self._prompt_version = prompt_version
        self._max_tokens = max_tokens
        self.tokens = 0
        """Tokens the judge used (cached answers count none)."""

    # Models -----------------------------------------------------------------------------------

    def _instance(self) -> EmbeddingModel | None:
        try:
            return self._models.instance()
        except Exception as error:  # a provider that cannot be created gives no signal
            _log_once("embeddings", "semantic reconciliation stage off: %s", error)
            return None

    async def _embedding(self) -> EmbeddingModel | None:
        try:
            return await self._models.get()
        except Exception as error:
            _log_once("embeddings", "semantic reconciliation stage off: %s", error)
            return None

    def _judge_model(self) -> ChatModel | None:
        if self._chat is None and self._chat_factory is not None:
            try:
                self._chat = self._chat_factory()
            except Exception as error:
                _log_once("judge", "reconciliation judge off: %s", error)
                self._chat_factory = None
        return self._chat

    # Competing text values -------------------------------------------------------------------

    async def review(
        self, drafts: Sequence[CandidateDraft], options: ReconcileOptions
    ) -> list[CandidateDraft]:
        """The drafts to store, with their signals; those above
        `semantic.equivalence_threshold` left out (module doc)."""
        semantic = options.semantic
        texts = {d.fingerprint: _texts(d) for d in drafts}
        wanted = sorted({t for found in texts.values() if len(found) > 1 for t in found})
        if not wanted:
            return list(drafts)
        model = await self._embedding()
        if model is None:
            return list(drafts)
        prefix = self._models.document_prefix
        try:
            vectors = dict(
                zip(wanted, await model.embed([prefix + t for t in wanted]), strict=True)
            )
        except Exception as error:
            log.warning("semantic reconciliation: embedding failed, no signal: %s", error)
            return list(drafts)
        kept: list[CandidateDraft] = []
        for draft in drafts:
            found = texts[draft.fingerprint]
            if len(found) < 2:
                kept.append(draft)
                continue
            scored = [(cosine(vectors[a], vectors[b]), a, b) for a, b in combinations(found, 2)]
            similarity, first, second = min(scored)
            threshold = semantic.equivalence_threshold
            if threshold is not None and similarity >= threshold:
                continue
            signals: dict[str, JsonValue] = {
                **draft.signals,
                Signal.SIMILARITY.value: round(similarity, 4),
            }
            if similarity >= semantic.equivalent_similarity:
                signals[Signal.EQUIVALENT.value] = True
            if semantic.judge:
                verdict = await self._judge(first, second)
                if verdict is not None:
                    signals[Signal.JUDGE.value] = verdict
            kept.append(draft.model_copy(update={"signals": signals}))
        return kept

    async def _judge(self, first: str, second: str) -> dict[str, JsonValue] | None:
        """The judge's label and score for two texts; None when it cannot answer."""
        if self._chat_factory is None and self._chat is None:
            return None
        key = judge_key(self._chat_model, self._prompt_version, first, second)
        try:
            stored = await self._cache.get(key) if self._cache is not None else None
        except Exception as error:
            log.warning("reconciliation judge: cache read failed: %s", error)
            stored = None
        if stored is not None:
            data, _usage, model = stored
        else:
            if self._max_tokens and self.tokens >= self._max_tokens:
                _log_once(
                    f"judge-budget-{id(self)}",
                    "reconciliation judge stopped: %d tokens used",
                    self.tokens,
                )
                return None
            chat = self._judge_model()
            if chat is None:
                return None
            try:
                answer = await chat.complete_json(
                    [
                        Message("system", JUDGE_PROMPT),
                        Message("user", f"A:\n<<<\n{first}\n>>>\nB:\n<<<\n{second}\n>>>"),
                    ],
                    JUDGE_SCHEMA,
                    ChatOptions(task=JUDGE_TASK),
                )
            except Exception as error:
                log.warning("reconciliation judge failed, no signal: %s", error)
                return None
            data, model = answer.data, answer.model
            usage: Usage = answer.usage
            self.tokens += usage.input_tokens + usage.output_tokens
            if self._cache is not None:
                try:
                    await self._cache.put(key, data, usage, model)
                except Exception as error:
                    log.warning("reconciliation judge: cache write failed: %s", error)
        if not isinstance(data, dict) or not isinstance(data.get("contradicts"), bool):
            return None
        score = data.get("score")
        if not isinstance(score, int | float) or not 0 <= score <= 1:
            return None
        return {
            "label": "contradiction" if data["contradicts"] else "consistent",
            "score": round(float(score), 4),
            "model": model,
        }

    # Duplicates from stored vectors ----------------------------------------------------------

    async def duplicates(
        self,
        session: AsyncSession,
        kind: Kind,
        keys: Sequence[str],
        *,
        domain_id: str,
        read_branch: str,
        branch: str,
        threshold: float,
        limit: int,
        proposal_id: str | None = None,
    ) -> list[Raised]:
        """`duplicate` drafts for the Records of `keys` whose stored vector is at least
        `threshold` similar to another Record's of `kind` on `read_branch`, raised on `branch`;
        at most `limit` pairs. No model call: the model only names the stored vectors."""
        fingerprint = template_fingerprints([kind]).get(kind.id)
        if fingerprint is None or limit < 1 or not keys:
            return []
        model = self._instance()
        if model is None:
            return []
        hits = await similar_vectors(
            session,
            domain_id,
            read_branch,
            kind.id,
            keys,
            model=model.name,
            fingerprint=fingerprint,
            dims=model.dims,
            threshold=threshold,
            neighbours=limit,
        )
        raised: dict[tuple[str, str], Raised] = {}
        for hit in hits:
            if len(raised) >= limit:
                break
            pair = (min(hit.key, hit.other), max(hit.key, hit.other))
            raised.setdefault(
                pair,
                Raised(
                    duplicate_draft(
                        branch=branch,
                        kind_id=kind.id,
                        keys=pair,
                        rule=DuplicateRule.SEMANTIC,
                        score=hit.score,
                        proposal_id=proposal_id,
                    )
                ),
            )
        return list(raised.values())


def build_semantic_stage(
    options: ReconcileOptions,
    models: EmbeddingModels | None,
    *,
    chat: Callable[[], ChatModel] | None = None,
    chat_model: str = "",
    cache: AnswerCache | None = None,
    prompt_version: str = "",
    max_tokens: int = 0,
) -> EmbeddingSemanticStage | None:
    """The stage when the operator turned it on (`semantic.enabled`) and an embedding model is
    configured; None otherwise (logged once). `chat` is created only when the judge asks."""
    if not options.enabled or not options.semantic.enabled:
        return None
    if models is None:
        _log_once("embeddings", "semantic reconciliation stage off: no embedding model")
        return None
    return EmbeddingSemanticStage(
        models,
        chat=chat,
        chat_model=chat_model,
        cache=cache,
        prompt_version=prompt_version,
        max_tokens=max_tokens,
    )


__all__ = [
    "JUDGE_SCHEMA",
    "JUDGE_TASK",
    "EmbeddingSemanticStage",
    "build_semantic_stage",
    "cosine",
    "judge_key",
]
