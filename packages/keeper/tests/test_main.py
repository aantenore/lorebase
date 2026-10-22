from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lorebase_keeper import __main__ as entrypoint
from lorebase_store.auth import AuditStream, ChainHead, Chains, ChainVerification
from lorebase_store.auth.checkpoints import Checkpoint, signed_document


@pytest.fixture(autouse=True)
def no_global_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *_a, **_k: None)


def test_main_serves_the_app_on_host_and_port_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: served.update(kwargs))
    monkeypatch.setenv("LOREBASE__KEEPER__HOST", "0.0.0.0")
    monkeypatch.setenv("LOREBASE__KEEPER__AUTH__ALLOW_INSECURE", "true")
    monkeypatch.setenv("LOREBASE__KEEPER__PORT", "9191")

    entrypoint.main([])

    assert (served["host"], served["port"]) == ("0.0.0.0", 9191)
    assert served["log_config"] is None
    assert served["proxy_headers"] is False


def test_main_exits_with_the_configuration_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__KEEPER__PORT", "not-a-port")
    with pytest.raises(SystemExit) as caught:
        entrypoint.main([])
    assert "keeper.port" in str(caught.value)


def test_bootstrap_exits_with_the_reason_when_api_keys_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOREBASE__KEEPER__AUTH__MODE", "none")
    with pytest.raises(SystemExit) as caught:
        entrypoint.main(["bootstrap"])
    assert "API keys are not accepted" in str(caught.value)


@pytest.mark.parametrize(
    ("maintenance", "expected"),
    [
        (None, None),
        ("postgresql://owner:pw@db:5432/lorebase", "postgresql://owner:pw@db:5432/lorebase"),
    ],
)
def test_installation_commands_connect_as_the_maintenance_role_when_set(
    monkeypatch: pytest.MonkeyPatch, maintenance: str | None, expected: str | None
) -> None:
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", "postgresql://app:pw@db:5432/lorebase")
    if maintenance is not None:
        monkeypatch.setenv("LOREBASE__DATABASE__MAINTENANCE_DSN", maintenance)
    else:
        monkeypatch.delenv("LOREBASE__DATABASE__MAINTENANCE_DSN", raising=False)
        monkeypatch.delenv("LOREBASE_DB_MAINTENANCE_DSN", raising=False)
    used: dict[str, Any] = {}

    class _Engine:
        async def dispose(self) -> None:
            return None

    def fake_engine(database: Any, *, url: str | None = None, pooled: bool = True) -> _Engine:
        used.update(url=url, pooled=pooled)
        return _Engine()

    monkeypatch.setattr(entrypoint, "create_engine", fake_engine)
    entrypoint._store(entrypoint.load_settings())
    assert used == {"url": expected, "pooled": False}


class _Audit:
    def __init__(self, result: ChainVerification) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def verify(self, **kwargs: Any) -> ChainVerification:
        self.calls.append(kwargs)
        return self.result

    async def entry_hashes(self, positions: Any) -> dict[tuple[str | None, int], bytes]:
        return {("acme", 3): b"\x01" * 32}


def _fake_store(monkeypatch: pytest.MonkeyPatch, result: ChainVerification) -> _Audit:
    audit = _Audit(result)

    class _Unit:
        def __init__(self) -> None:
            self.audit = audit
            self.read_audit = audit

    class _Store:
        @asynccontextmanager
        async def unit(self) -> AsyncIterator[_Unit]:
            yield _Unit()

    async def dispose() -> None:
        return None

    monkeypatch.setattr(entrypoint, "_store", lambda settings: (_Store(), dispose))
    return audit


@pytest.mark.parametrize(
    ("argv", "chain"),
    [(["acme"], "acme"), (["--installation"], None), (["--all"], Chains.ALL)],
)
def test_audit_verify_selects_the_chain_and_reports_an_intact_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    chain: str | Chains | None,
) -> None:
    audit = _fake_store(monkeypatch, ChainVerification(5, chains=2, last_seq=41))
    entrypoint.main(["audit", "verify", *argv, "--after-seq", "3"])
    assert audit.calls == [{"after_seq": 3, "domain_id": chain}]
    assert capsys.readouterr().out == "audit log intact: 5 entries in 2 chain(s), last seq 41\n"


def test_audit_verify_exits_1_on_a_broken_chain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = ChainVerification(
        2, 7, "entry content changed", chains=1, last_seq=4, bad_domain_id="acme"
    )
    _fake_store(monkeypatch, broken)
    with pytest.raises(SystemExit) as caught:
        entrypoint.main(["audit", "verify", "--all", "--json"])
    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "stream": "actions",
        "chain": "all",
        "intact": False,
        "checked": 2,
        "chains": 1,
        "last_seq": 4,
        "first_broken": {"seq": 7, "domain_id": "acme", "reason": "entry content changed"},
    }


def test_audit_verify_refuses_a_negative_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_store(monkeypatch, ChainVerification(0))
    with pytest.raises(SystemExit) as caught:
        entrypoint.main(["audit", "verify", "acme", "--after-seq", "-1"])
    assert "--after-seq" in str(caught.value)


def test_audit_verify_compares_the_log_with_signed_checkpoints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "checkpoint.pub"
    public.write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    at = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    heads = (ChainHead("acme", 3, b"\x01" * 32), ChainHead("acme", 9, b"\x02" * 32))
    folder = tmp_path / "checkpoints" / "actions"
    folder.mkdir(parents=True)
    for n, head in enumerate(heads):
        checkpoint = Checkpoint(AuditStream.ACTIONS, at.replace(hour=n), "main", (head,))
        (folder / f"{n}.json").write_bytes(signed_document(checkpoint, key))
    reads = Checkpoint(AuditStream.READS, at, "main", (ChainHead("acme", 1, b"\x03" * 32),))
    (folder / "reads.json").write_bytes(signed_document(reads, key))  # other stream: ignored
    forged = Checkpoint(AuditStream.ACTIONS, at, "main", ())
    (folder / "forged.json").write_bytes(signed_document(forged, Ed25519PrivateKey.generate()))
    audit = _fake_store(monkeypatch, ChainVerification(5, chains=1, last_seq=8))
    argv = ["audit", "verify", "acme", "--json", "--checkpoints", str(tmp_path / "checkpoints")]

    with pytest.raises(SystemExit) as refused:
        entrypoint.main(argv)
    assert "--checkpoint-key" in str(refused.value)

    with pytest.raises(SystemExit) as caught:
        entrypoint.main([*argv, "--checkpoint-key", str(public)])
    assert caught.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["intact"] is False
    assert report["checkpoints"]["checked"] == 2
    assert [f["seq"] for f in report["checkpoints"]["findings"]] == [9]  # cut from the tail
    assert report["checkpoints"]["findings"][0]["problem"] == "missing"
    [invalid] = report["checkpoints"]["invalid"]
    assert "forged.json" in invalid
    assert audit.calls == [{"after_seq": 0, "domain_id": "acme"}]
