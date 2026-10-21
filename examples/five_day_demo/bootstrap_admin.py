"""Create the first administrator of the demo stack and print a new API key for it.

Keeper in `api_key` mode accepts no request without a key, and a key can only be issued by an
administrator, so the first one is written straight to the database. stack.sh pipes this file into
the Keeper container (`python -`), where the settings and the pepper are already configured:

    docker compose exec -T keeper python - < bootstrap_admin.py

The administrator is the service principal named in `keeper.auth.installation_admins` of
lorebase.demo.yaml. Running the script again adds one more key to it. The key goes to standard
output, nothing else does.

The script also creates the service principal `agent-risk` (without a key). The Operation
`flag_supplier_risk` of the supply schema is restricted to the subjects `risk-analyst` and
`agent-risk`, and the subjects of an API key are its principal id and its groups, so day 5 issues
the agent's key on this principal. Groups cannot be created through the API yet.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.settings import keeper_settings
from lorebase_platform.settings import load_settings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.models.enums import PrincipalType
from lorebase_store.settings import DatabaseSettings

AGENT_PRINCIPAL = ("agent-risk", "agent that flags supplier risk")


async def main() -> str:
    settings = load_settings()
    auth = keeper_settings(settings).auth
    if not auth.installation_admins:
        sys.exit("keeper.auth.installation_admins is empty: nothing to bootstrap")
    admin_id = auth.installation_admins[0]
    codec = ApiKeyCodec(auth.api_keys)
    engine = create_engine(settings.section("database", DatabaseSettings), pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        async with store.unit() as unit:
            if await unit.principals.get(admin_id) is None:
                await unit.principals.add(
                    PrincipalType.SERVICE, "demo administrator", principal_id=admin_id
                )
            if await unit.principals.get(AGENT_PRINCIPAL[0]) is None:
                await unit.principals.add(
                    PrincipalType.SERVICE, AGENT_PRINCIPAL[1], principal_id=AGENT_PRINCIPAL[0]
                )
            generated = codec.generate()
            await unit.api_keys.add(
                principal_id=admin_id, prefix=generated.prefix, key_hash=generated.key_hash
            )
        return generated.token
    finally:
        await engine.dispose()


if __name__ == "__main__":
    print(asyncio.run(main()))
