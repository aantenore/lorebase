# Process agent example

An external agent that reads and acts on the `supply` Domain through [Herald](../../docs/mcp.md),
Lorebase's MCP server. It shows how a third-party agent plugs in: it is a single script
(`agent.py`) that imports no Lorebase package, and it is not part of the product.

What it does:

- connects to Herald over stdio (starts `lorebase-herald`) or Streamable HTTP;
- turns Herald's MCP tools into chat tool definitions for any OpenAI-compatible endpoint;
- runs a bounded loop (steps, tokens, wall-clock time);
- prints every step (tool, argument summary, result summary) and ends with an answer, the Records
  it cites (checked against what it actually read) and the Proposals it opened;
- posts its reasoning to the agent-trace notes endpoint when Keeper has one (skipped otherwise).

## Prerequisites

- Python 3.12 and `pip install -r requirements.txt` (`mcp`, `httpx`), or run it from the
  repository with `uv run`.
- The stack of the [quickstart](../../docs/quickstart.md) (or `examples/five_day_demo/stack.sh up`,
  see [demo](../../docs/demo.md)) with the `supply` Domain loaded: Keeper on
  `http://localhost:8080`.
- An API key for the agent: a service principal with role `agent-reader` and a grant on the
  Operations it may run (`flag_supplier_risk`, `approve_contract`), plus `editor` on `agent/*` so
  it can open task branches. [demo, day 5](../../docs/demo.md) creates exactly this. Never give an
  agent an admin key.
- For stdio, `lorebase-herald` on the `PATH` (`uv sync --all-packages` in the repository). For
  HTTP, a Herald running with `LOREBASE__HERALD__TRANSPORT=streamable_http`.
- A chat model that supports tool calls.

## Run it against the compose stack

```bash
export LOREBASE_URL=http://localhost:8080
echo "lbk_xxxxxxxxxx_yyyyyyyy" > ~/.config/lorebase/agent.key && chmod 600 ~/.config/lorebase/agent.key

# Herald started by the agent over stdio, Ollama on localhost
ollama pull qwen2.5:7b
python examples/agent/agent.py --task single-source-risk --mcp-key-file ~/.config/lorebase/agent.key

# or against a shared Herald over Streamable HTTP
python examples/agent/agent.py --task what-if S-1007 \
  --mcp-url http://127.0.0.1:8090/mcp --mcp-key-file ~/.config/lorebase/agent.key
```

Tasks (`--task NAME [ARG]`):

| Task | What it does | Writes |
|------|--------------|--------|
| `single-source-risk` | Parts that depend on one supplier whose contract ends within 90 days, and the risk behind them | none |
| `flag-critical` | Raises critical risk flags with `flag_supplier_risk`; each becomes a Proposal | Proposals |
| `approve-contract C-3025` | Checks the draft contract and its supplier, then runs `approve_contract` | Proposal if above the review threshold |
| `what-if S-1007` | Impact if the supplier stops: parts, single-sourced parts, orders, plants | none |

`--prompt "..."` runs a free-form read-only task; add `--allow-operation NAME` to let it run
that Operation (still through Proposals).

## Chat providers

The endpoint is `--base-url` (env `AGENT_BASE_URL`) and the model `--model` (`AGENT_MODEL`).
The defaults are Ollama at `http://localhost:11434/v1` and `qwen2.5:7b`. Small local models
follow multi-step tasks less reliably than hosted ones; raise `--max-steps` if one stops early.

Hosted provider, key and workspace id read from files (the values never appear on the command
line, in the output or in logs):

```bash
python examples/agent/agent.py --task flag-critical \
  --base-url https://api.anthropic.com/v1/ --model claude-sonnet-5-5 \
  --api-key-file ~/.config/lorebase/anthropic.key \
  --header-file anthropic-workspace-id=~/.config/lorebase/anthropic.workspace \
  --temperature none --extra-body '{"thinking": {"type": "between_tools"}}' \
  --max-total-tokens 150000 \
  --mcp-key-file ~/.config/lorebase/agent.key
```

Secrets come from `--api-key-file PATH` or `--api-key-env NAME` (sent as
`Authorization: Bearer`), and extra headers from `--header NAME=VALUE` (not secret),
`--header-file NAME=PATH` or `--header-env NAME=VAR`. The Lorebase key comes from
`--mcp-key-file` or the `LOREBASE_API_KEY` variable.

`--temperature` (env `AGENT_TEMPERATURE`, default `0`) takes a number or `none`, which leaves
the field out: current Claude models answer HTTP 400 to any `temperature`. `--extra-body`
(env `AGENT_EXTRA_BODY`) is a JSON object merged into every chat request for server-specific
options; it cannot replace `model`, `messages`, `tools`, `tool_choice`, `max_tokens`,
`temperature` or `stream`. Thinking settings per Claude model, as checked on 2026-10-22
([validation](../../docs/validation.md#live-model-runs)):

| Model | `--extra-body` |
|-------|----------------|
| `claude-haiku-5-5` | `{"thinking": {"type": "disabled"}}` |
| `claude-sonnet-5-5` | `{"thinking": {"type": "between_tools"}}` (`disabled` is refused) |
| `claude-opus-5-5` | `{"reasoning_effort": "low"}` (thinking cannot be turned off) |

Every step resends the conversation with Herald's tool catalogue, so a hosted run of a
ready-made task used 45,000 to 90,000 tokens in five or six steps: with the default
`--max-total-tokens` (60000) most runs ended on the token bound and answered from what they
had read. Use 150000 for hosted models.

## Expected output

```text
agent: domain=supply model=qwen2.5:7b chat=http://localhost:11434/v1 herald=stdio writes=off
[step 1] lorebase_schema {"detail":"summary","domain":"supply"}
          -> 41 lines, 2310 chars: schema supply@main rev 97ce9a2ce272...
[step 2] lorebase_query {"query":{"kind":"Part","where":{"single_sourced":true}},...}
          -> 22 lines, 1904 chars: 18 rows ...
...
=== Answer ===
Three single-sourced parts depend on suppliers with contracts ending before ...
- Part/P-10028 -> Supplier/S-1001, Contract/C-3001 ends 2026-03-02, risk_score 90 ...

=== Summary ===
stop: answer; steps: 7; tokens: 9412
Records cited: Contract/C-3001, Part/P-10028, Supplier/S-1001
trace: skipped (HTTP 404)
```

With `flag-critical` the summary adds `Proposal opened: prp_... (open) (waiting for a reviewer)`.
The exit status is 0 when the model answered, 1 when a bound stopped it first (the agent then
asks for a final answer from what it read), 2 when the chat API failed.

## Safety notes

- **Writes are governed.** The only write tool is `lorebase_execute`. The agent exposes it only
  for tasks that name Operations, refuses any other Operation, always names a `task` and an
  `intent` on committing calls (Keeper then opens a Proposal on a branch `agent/{principal}/...`
  when the change needs review), sets an `idempotency_key`, never passes `auto_merge`, and caps
  committing calls per run. A person approves and merges. Keeper enforces authorization,
  review policy and rate limits regardless of this script.
- **Graph data is untrusted.** Every tool result reaches the model between random per-run
  markers, and the system prompt says to treat it as data only. This lowers the risk of prompt
  injection through Record values; it does not remove it. Use a key with the narrowest grants.
- **Cited Records are checked** against the tool results; unread ones are reported as a warning.
- **Secrets stay out of the output.** Keys are read from files or variables, held in memory
  and scrubbed from error messages. The trace notes carry reasoning and tool names with their
  argument names, not argument values or tool results.
- **Bounded.** `--max-steps` (12), `--max-total-tokens` (60000), `--max-completion-tokens`
  (2000), `--timeout` (300 s) and `--max-result-chars` (8000, cut and marked).

## Agent trace

With a Lorebase key, the agent traces its run as one [agent trace](../../docs/agent-trace.md)
session (`--trace-session`, default `agent-<random>`). It sends the session id in
`--trace-session-header` (default `X-Lorebase-Session`) with `X-Lorebase-Trace: on` to Herald,
which forwards both to Keeper on every tool call (stdio: through `herald.trace.session_id` and
`herald.trace.opt_in`), and posts its thoughts, tool calls and final answer as notes to
`--trace-url` (default `{keeper-url}/api/v1/trace/sessions/{session}/notes`, with the Domain
as `domain_id`). Keeper records them when `keeper.trace.enabled` is on; the summary prints the
timeline URL (`/api/v1/trace/sessions/{session}`, web app
`/{domain}/agent-activity?session={session}`). Any failure of a note, such as a `404` from a
Keeper without tracing, turns the notes off for the run. `--no-trace` disables both.

## Test

```bash
uv run pytest examples/agent -q
```

A local HTTP stub plays the chat model from a script and Herald runs in memory over Herald's
fake Keeper. The test asserts the tool calls, that a critical flag becomes a task write (the
Proposal path) without `auto_merge`, that other Operations are refused and that read-only tasks
have no write tool. No Docker or model is needed; the module carries the `integration` marker
only because the stub listens on loopback.
