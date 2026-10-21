---
title: REST API reference
description: Every route of the Lorebase Keeper REST API, rendered from the committed OpenAPI document.
---

# REST API reference

Keeper serves the REST API under `/api/v1`. The reference below is rendered from the
[OpenAPI document](../openapi.json) committed as `docs/openapi.json`; a test fails when it differs
from the application, and a running Keeper serves the same document at `/api/v1/openapi.json`.
The Python client and the [command line](cli.md) are checked against it.

For authentication, roles and the error format see the [design](../design.md) and the
[security review](../security-review.md). Agents that prefer tools to HTTP use the
[MCP server](../mcp.md).

<iframe class="api-frame" src="../api-viewer.html" title="Lorebase REST API reference" loading="lazy"></iframe>

[Open the reference on its own page](api-viewer.html), or download
[openapi.json](../openapi.json).
