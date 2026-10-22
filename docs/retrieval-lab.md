# Retrieval lab

The retrieval lab (web app, **Retrieval lab** in the side navigation,
`/{domain}/retrieval-lab?q=<question>&budget=<tokens>`) shows what an agent would find for a
question, and how the answer changes with the search parameters. It calls Keeper's existing
read endpoints with the signed-in user's credentials; nothing in it calls a model.

![Retrieval lab](images/retrieval-lab.png)

## What it shows

- **Two parameter sets side by side** (A and B): mode (`lexical`, `semantic`, `hybrid`),
  `lexical_weight`, `semantic_weight`, `rrf_k`, `trigram_threshold`, `trigram_weight`,
  `typo_candidates`, `candidates`, the result limit and a Kind filter. Each column lists its
  hits with score and snippet (shown as text), the typo corrections applied, and how each hit
  moved compared with the other set (new, gone, rank up or down).
- **The context pack** an agent would receive for the question (`POST /context` with
  `search: <question>` and the budget you give, or Keeper's default): seeds, neighbors and
  Links, each with why it is in the pack (matched the search; reached at depth N through
  Relation R from a seed or neighbor), the tokens used against the budget per section, and what
  trimming removed.
- **The involved Records on the Canvas**: hits of both sets and the Records of the pack, with
  the pack's Links, highlighted.

The parameter sets are sent as `tuning` on `POST /search`, which replaces the `search`
settings for that one request:

```json
{"q": "bearing", "kinds": ["Part"], "limit": 10,
 "tuning": {"lexical_weight": 1.0, "semantic_weight": 2.0, "rrf_k": 30,
            "trigram_threshold": 0.4, "trigram_weight": 0.5, "typo_candidates": 2,
            "candidates": 100}}
```

Unset knobs keep their configured value. `candidates`, `typo_candidates` and
`trigram_threshold` bound the work of a search, so they may only make it smaller: the first two
lower, the threshold higher (otherwise `422`, pointer `/body/tuning/<name>`). Authorization, masking and limits are those of any search.

## Query vectors

Semantic and hybrid search need the query's embedding in the model and dimension the Domain
is configured with (`search.semantic.model`, `store.embeddings.dims`). Keeper never computes
it. The lab gets one in two ways:

- **Paste a vector** (a JSON array of numbers), for example the output of
  `lorebase-inference embed --query "your question"`.
- **[Guide](guide.md), called by the browser**: set `guideUrl` in the web app's runtime
  configuration (`/config.js`, or `VITE_GUIDE_URL` at build time). The lab then sends
  `POST {guideUrl}/embed` with `{"text": "<question>", "mode": "query"}` and the user's own
  Keeper credential (the bearer token, or the session cookie and CSRF header in session mode),
  and reads `{"vector": [...], "model": "...", "dims": 384}`. Guide must list the web app's
  origin (`guide.http.allowed_origins`), and the web app's Content-Security-Policy must allow
  it in `connect-src` (the web image adds the origin of `VITE_GUIDE_URL`). Every question asked
  in the lab, including one in a shared lab URL, is sent to Guide.

Without either, the lab searches lexically and says so; a set left on semantic or hybrid is
searched lexically with a warning.

When `guideUrl` is set, the lab also reserves a place for an **Explore** panel (an optional
read-only exploration service working with the caller's own credentials); without `guideUrl`
nothing is shown.

## Related

- [Agent trace](agent-trace.md): what an agent actually asked and touched, step by step.
- [Agents](agents.md#1-context-packs): context packs, budgets and trimming.
- Search settings: `search` in `config/lorebase.example.yaml`
  ([reference](reference/configuration.md)).
