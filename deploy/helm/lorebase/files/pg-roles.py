"""Roles of the in-cluster database, declared before the migration of a `helm upgrade`.

The CloudNativePG operator creates the roles listed in `spec.managed.roles` of the Cluster, and
Helm applies the upgraded Cluster only after the pre-upgrade hooks, so a role added by an upgrade
would not exist yet when the migration Job (and its `db-roles` init container) runs. This hook Job
runs before the migration and, through the Kubernetes API with its own ServiceAccount:

- adds the roles of `postgresql.cluster.roles` to the Cluster, keeping any other role there;
- waits until the operator reports the application role as reconciled.

A role whose password Secret does not exist yet (the release creates it after the hooks) is
declared without `passwordSecret`: the operator creates it with LOGIN and no password, and sets the
password once Helm applies the Cluster and the Secret, before the new pods connect with it.

Idempotent. Without a Cluster (nothing was installed yet) it does nothing: the operator then
creates the roles together with the Cluster.

Environment: CLUSTER, APP_ROLE, ROLES (JSON list of the managed roles), ROLE_WAIT_S.
"""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NoReturn

ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
MERGE = "application/merge-patch+json"

Role = dict[str, Any]


def fail(message: str) -> NoReturn:
    sys.exit(f"pg-roles: {message}")


class Api:
    """Minimal client of the Kubernetes API with the token of the pod's ServiceAccount."""

    def __init__(self) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = (
            os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ["KUBERNETES_SERVICE_PORT"]
        )
        self.base = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
        with open(f"{ACCOUNT}/token", encoding="utf-8") as token:
            self.token = token.read().strip()
        with open(f"{ACCOUNT}/namespace", encoding="utf-8") as namespace:
            self.namespace = namespace.read().strip()
        self.tls = ssl.create_default_context(cafile=f"{ACCOUNT}/ca.crt")

    def call(self, method: str, path: str, patch: Any = None) -> dict[str, Any] | None:
        """The JSON answer, or None for 404. `patch` is sent as a JSON merge patch."""
        request = urllib.request.Request(
            f"{self.base}/{path.format(namespace=self.namespace)}",
            data=None if patch is None else json.dumps(patch).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": MERGE,
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.tls, timeout=30) as response:
                answer: dict[str, Any] = json.load(response)
                return answer
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            detail = error.read().decode(errors="replace")
            with contextlib.suppress(ValueError):
                detail = json.loads(detail).get("message", detail)
            fail(f"{method} {request.full_url}: HTTP {error.code}: {detail[:500]}")
        except urllib.error.URLError as error:
            fail(f"{method} {request.full_url}: {error.reason}")


def covers(current: Role, wanted: Role) -> bool:
    """The operator adds defaults to a role, so a declared role only needs the wanted fields."""
    return all(current.get(key) == value for key, value in wanted.items())


def declare(api: Api, cluster: dict[str, Any], path: str, wanted: list[Role]) -> None:
    current: list[Role] = ((cluster.get("spec") or {}).get("managed") or {}).get("roles") or []
    ready: list[Role] = []
    for role in wanted:
        secret = (role.get("passwordSecret") or {}).get("name")
        if secret and api.call("GET", f"api/v1/namespaces/{{namespace}}/secrets/{secret}") is None:
            print(f"pg-roles: Secret {secret!r} not there yet; role {role['name']!r} gets it later")
            role = {key: value for key, value in role.items() if key != "passwordSecret"}
        ready.append(role)
    found = {role.get("name"): role for role in current}
    if all(role["name"] in found and covers(found[role["name"]], role) for role in ready):
        return
    by_name = {role["name"]: role for role in ready}
    merged = [by_name.pop(role.get("name"), role) for role in current] + list(by_name.values())
    api.call("PATCH", path, {"spec": {"managed": {"roles": merged}}})
    print(f"pg-roles: declared {sorted(role['name'] for role in ready)} on the Cluster")


def wait_for(api: Api, path: str, role: str, wait_s: float) -> None:
    deadline = time.monotonic() + wait_s
    problems: list[str] = []
    while True:
        cluster = api.call("GET", path) or {}
        status = (cluster.get("status") or {}).get("managedRolesStatus") or {}
        if role in ((status.get("byStatus") or {}).get("reconciled") or []):
            return
        problems = (status.get("cannotReconcile") or {}).get(role) or problems
        if time.monotonic() > deadline:
            reason = "; ".join(problems) or "not reconciled yet"
            fail(f"the operator did not create role {role!r} within {wait_s:.0f}s: {reason}")
        time.sleep(3)


def main() -> None:
    role = os.environ["APP_ROLE"]
    wanted: list[Role] = json.loads(os.environ["ROLES"])
    if not any(entry.get("name") == role for entry in wanted):
        fail(f"postgresql.cluster.roles does not declare {role!r}")
    api = Api()
    path = "apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/" + os.environ["CLUSTER"]
    cluster = api.call("GET", path)
    if cluster is None:
        print("pg-roles: no Cluster yet; the operator creates the roles with it")
        return
    declare(api, cluster, path, wanted)
    wait_for(api, path, role, float(os.environ.get("ROLE_WAIT_S", "300")))
    print(f"pg-roles: role {role!r} is ready")


if __name__ == "__main__":
    main()
