"""Signed checkpoints of the audit chains, kept outside the database.

A table owner can delete the newest entries of a chain, or recompute a whole chain, and the
chain still verifies. A checkpoint records the head (`seq`, `entry_hash`) of every chain of a
stream at a point in time; Scribe publishes one periodically to a sink the database cannot
reach (`scribe.audit_checkpoints`), and `audit verify --checkpoints` compares the log with
them: a checkpointed entry that is missing reveals a truncation, one whose hash differs a
rewrite. Entries newer than the latest checkpoint are not covered.

A checkpoint is a JSON document signed with Ed25519 over the RFC 8785 form of every member
but `signature`, so a verifier needs only the public key and a writer to the sink cannot forge
one::

    {"format": "lorebase.audit.checkpoint/1", "stream": "actions",
     "at": "2026-10-25T09:00:00.000000+00:00", "key_id": "main",
     "chains": [{"domain_id": "acme", "seq": 42, "entry_hash": "<hex>"}, ...],
     "signature": "<base64>"}
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from lorebase_core.canonical import jcs_bytes
from lorebase_store.auth.audit import AuditRepository, AuditStream, ChainHead

FORMAT = "lorebase.audit.checkpoint/1"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


class CheckpointError(ValueError):
    """A document is not a valid checkpoint or its signature does not verify."""


@dataclass(frozen=True)
class Checkpoint:
    """The heads of every chain of a stream at `at`."""

    stream: AuditStream
    at: datetime
    key_id: str
    chains: tuple[ChainHead, ...]

    def body(self) -> dict[str, Any]:
        """Every member but the signature."""
        return {
            "format": FORMAT,
            "stream": self.stream.value,
            "at": self.at.astimezone(UTC).isoformat(timespec="microseconds"),
            "key_id": self.key_id,
            "chains": [
                {"domain_id": h.domain_id, "seq": h.seq, "entry_hash": h.entry_hash.hex()}
                for h in self.chains
            ],
        }

    def name(self, prefix: str = "") -> str:
        """Object name of the checkpoint under `prefix`: by stream, then sortable time."""
        stamp = self.at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{prefix}{self.stream.value}/{stamp}.json"


@dataclass(frozen=True)
class CheckpointFinding:
    """A checkpointed entry the log no longer holds as it was."""

    checkpoint_at: datetime
    domain_id: str | None
    seq: int
    problem: Literal["missing", "changed"]


def load_private_key(pem: str) -> Ed25519PrivateKey:
    """An Ed25519 private key from PEM (PKCS #8, as `openssl genpkey -algorithm ed25519`)."""
    try:
        key = load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        # The parser's message is left out: it never needs to describe secret material.
        raise CheckpointError("not a PEM private key (unencrypted PKCS #8)") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise CheckpointError("the checkpoint signing key must be an Ed25519 key")
    return key


def load_public_key(pem: str) -> Ed25519PublicKey:
    """An Ed25519 public key from PEM (`openssl pkey -pubout`)."""
    try:
        key = load_pem_public_key(pem.encode())
    except (ValueError, TypeError) as error:
        raise CheckpointError(f"not a PEM public key: {error}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise CheckpointError("the checkpoint key must be an Ed25519 key")
    return key


def signed_document(checkpoint: Checkpoint, key: Ed25519PrivateKey) -> bytes:
    """The checkpoint as signed JSON."""
    body = checkpoint.body()
    signature = key.sign(jcs_bytes(body))
    document = {**body, "signature": base64.b64encode(signature).decode("ascii")}
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def _head(item: Any) -> ChainHead:
    if not isinstance(item, Mapping):
        raise CheckpointError("a chain must be an object")
    domain_id, seq, digest = item.get("domain_id"), item.get("seq"), item.get("entry_hash")
    if domain_id is not None and not isinstance(domain_id, str):
        raise CheckpointError("chain domain_id must be a string or null")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise CheckpointError("chain seq must be a positive integer")
    try:
        entry_hash = bytes.fromhex(digest) if isinstance(digest, str) else b""
    except ValueError:
        entry_hash = b""
    if len(entry_hash) != 32:
        raise CheckpointError("chain entry_hash must be 64 hex digits")
    return ChainHead(domain_id, seq, entry_hash)


def parse_document(raw: bytes, public_key: Ed25519PublicKey) -> Checkpoint:
    """A checkpoint from signed JSON; `CheckpointError` unless well formed and signed by
    `public_key`."""
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise CheckpointError("checkpoint document too large")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise CheckpointError(f"not JSON: {error}") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise CheckpointError(f"not a {FORMAT} document")
    encoded = document.pop("signature", None)
    if not isinstance(encoded, str):
        raise CheckpointError("signature missing")
    try:
        signature = base64.b64decode(encoded, validate=True)
        public_key.verify(signature, jcs_bytes(document))
    except (binascii.Error, InvalidSignature):
        raise CheckpointError("signature does not verify") from None
    try:
        stream = AuditStream(str(document.get("stream")))
        at = datetime.fromisoformat(str(document.get("at")))
    except ValueError as error:
        raise CheckpointError(str(error)) from None
    chains = document.get("chains")
    if not isinstance(chains, list):
        raise CheckpointError("chains must be a list")
    if at.tzinfo is None:
        raise CheckpointError("at must carry a time zone")
    return Checkpoint(stream, at, str(document.get("key_id", "")), tuple(map(_head, chains)))


async def compare(
    repository: AuditRepository,
    checkpoints: Iterable[Checkpoint],
    *,
    domain_ids: Sequence[str | None] | None = None,
) -> list[CheckpointFinding]:
    """Checkpointed heads the log does not hold with the same hash, oldest checkpoint first.
    `domain_ids` limits the comparison to some chains (None: every chain)."""
    findings: list[CheckpointFinding] = []
    for checkpoint in sorted(checkpoints, key=lambda c: c.at):
        heads = [h for h in checkpoint.chains if domain_ids is None or h.domain_id in domain_ids]
        stored = await repository.entry_hashes([(h.domain_id, h.seq) for h in heads])
        for head in heads:
            found = stored.get((head.domain_id, head.seq))
            if found is None:
                findings.append(
                    CheckpointFinding(checkpoint.at, head.domain_id, head.seq, "missing")
                )
            elif found != head.entry_hash:
                findings.append(
                    CheckpointFinding(checkpoint.at, head.domain_id, head.seq, "changed")
                )
    return findings
