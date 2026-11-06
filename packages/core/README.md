# lorebase-core

Part of [Lorebase](https://github.com/aantenore/lorebase), an open-source ontology platform: a versioned, governed knowledge graph on Postgres with git-like branching and three-way merge.

The domain logic of Lorebase: the ontology model, schema validation, the versioning engine (commits, branches and three-way merge) and the expression evaluation. It has no I/O; the other packages build on it.

```sh
pip install lorebase-core
```

It is a library of the other Lorebase packages and has no command of its own.

Documentation: [Design](https://aantenore.github.io/lorebase/design/) and the [documentation site](https://aantenore.github.io/lorebase/). License: Apache-2.0.
