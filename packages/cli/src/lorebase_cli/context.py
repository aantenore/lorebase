"""State shared by the commands of one invocation: settings, output and the client."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import typer

from lorebase_cli.failures import CliError
from lorebase_cli.output import Output
from lorebase_cli.settings import ENV_DOMAIN, CliSettings
from lorebase_client import LorebaseClient

ClientFactory = Callable[[CliSettings], LorebaseClient]


def default_client(settings: CliSettings) -> LorebaseClient:
    """A client for the configured server. (Imports send idempotency keys themselves; creating
    things does not, so that creating what exists is reported as a conflict.)"""
    return LorebaseClient(
        settings.url,
        api_prefix=settings.api_prefix,
        api_key=settings.api_key,
        token=settings.token,
        timeout=settings.timeout,
        env={},
    )


@dataclass
class AppContext:
    """Per-invocation state, stored in the Typer context."""

    settings: CliSettings
    client_factory: ClientFactory = default_client
    json_mode: bool = False
    quiet: bool = False
    _client: LorebaseClient | None = field(default=None, repr=False)

    @property
    def out(self) -> Output:
        return Output(json_mode=self.json_mode, max_cell_width=self.settings.max_cell_width)

    def client(self) -> LorebaseClient:
        if self._client is None:
            self._client = self.client_factory(self.settings)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def domain(self, given: str | None) -> str:
        """The Domain to work in: `--domain`, `LOREBASE_DOMAIN`, then the config file."""
        chosen = given or self.settings.domain
        if not chosen:
            raise CliError(
                f"no Domain: pass --domain, set {ENV_DOMAIN} or `domain` in the config file"
            )
        return chosen

    def branch(self, given: str | None) -> str | None:
        """The branch named by `--branch`, `LOREBASE_BRANCH` or the config file; none means
        the Domain's default branch."""
        return given or self.settings.branch

    def take[T](
        self, items: Iterable[T], limit: int | None, everything: bool
    ) -> tuple[list[T], bool]:
        """The first items of a listing and whether more exist: `--all` takes all, `--limit`
        that many, else the `list_limit` setting."""
        if everything:
            return list(items), False
        cap = limit or self.settings.list_limit
        taken = list(itertools.islice(items, cap + 1))
        return taken[:cap], len(taken) > cap

    def note_truncated(self, shown: int) -> None:
        """Tell the person a listing was cut (standard error, human output only)."""
        if not self.json_mode and not self.quiet:
            self.out.info(f"showing the first {shown} items; use --all or --limit for more")


def app_of(ctx: typer.Context, json_flag: bool = False) -> AppContext:
    """The invocation state; a `--json` flag given after the command turns JSON output on."""
    app = ctx.find_object(AppContext)
    if app is None:  # pragma: no cover - the root callback always sets it
        raise CliError("the command ran without its settings")
    if json_flag:
        app.json_mode = True
    return app
