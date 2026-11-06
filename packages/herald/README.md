# lorebase-herald

mcp-name: io.github.aantenore/lorebase-herald

Part of [Lorebase](https://github.com/aantenore/lorebase), an open-source ontology platform: a versioned, governed knowledge graph on Postgres with git-like branching and three-way merge.

Herald, the MCP server of Lorebase: it exposes a Keeper's governed knowledge graph to MCP clients as tools, resources and prompts, over stdio or Streamable HTTP.

```sh
uv tool install lorebase-herald    # or: pip install lorebase-herald
LOREBASE_URL=http://127.0.0.1:8080 LOREBASE_DOMAIN=supply lorebase-herald   # stdio: the MCP host starts this command
```

Documentation: [MCP](https://aantenore.github.io/lorebase/mcp/) and the [documentation site](https://aantenore.github.io/lorebase/). License: Apache-2.0.
