"""MkDocs hooks for the documentation site.

Two jobs, so that the Markdown files stay exactly as they read on GitHub:

* the changelog, the contributing guide and the security policy live in the repository root;
  they are added to the site as pages under `project/`
* a relative link that leaves the docs tree (`../examples/README.md`, `LICENSE`, a file of the
  launch kit that the site excludes) is pointed at the repository on GitHub, and a link to one of
  the root pages above is pointed at its page on the site
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).resolve().parents[2]
BRANCH = "main"

# Repository path of a root file -> path of its page in the site.
ROOT_PAGES = {
    "README.md": "index.md",
    "CHANGELOG.md": "project/changelog.md",
    "CONTRIBUTING.md": "project/contributing.md",
    "SECURITY.md": "project/security.md",
}
GENERATED = {site: repo for repo, site in ROOT_PAGES.items() if repo != "README.md"}

INLINE_LINK = re.compile(r"(?<=\])\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
HTML_LINK = re.compile(r"(?P<attr>\b(?:href|src)=)(?P<quote>[\"'])(?P<target>[^\"']+)(?P=quote)")
CODE_SPAN = re.compile(r"(`[^`]*`)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def on_files(files: Files, config: Any) -> Files:
    for site_path, repo_path in GENERATED.items():
        page = File.generated(config, site_path, content=(ROOT / repo_path).read_text())
        page.edit_uri = None  # there is nothing to edit under docs/
        files.append(page)
    return files


def on_page_markdown(markdown: str, page: Any, config: Any, files: Files) -> str:
    src_uri = page.file.src_uri
    base = "" if src_uri in GENERATED else posixpath.join("docs", posixpath.dirname(src_uri))
    repo_url = str(config["repo_url"]).rstrip("/")

    def rewrite(target: str) -> str:
        if not target or target.startswith(("#", "/")) or SCHEME.match(target):
            return target
        path, hash_mark, fragment = target.partition("#")
        repo_path = posixpath.normpath(posixpath.join(base, path))
        if repo_path.startswith(".."):
            return target
        site_path = _site_path(repo_path, files)
        if site_path is not None:
            relative = posixpath.relpath(site_path, posixpath.dirname(src_uri) or ".")
            return relative + hash_mark + fragment
        on_disk = ROOT / repo_path
        if on_disk.is_dir():
            kind = "tree"
        elif on_disk.is_file():
            kind = "blob"
        else:
            return target  # broken link: left for `mkdocs build --strict` to report
        return f"{repo_url}/{kind}/{BRANCH}/{repo_path}{hash_mark}{fragment}"

    out: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            line = "".join(
                part if index % 2 else _rewrite_links(part, rewrite)
                for index, part in enumerate(CODE_SPAN.split(line))
            )
        out.append(line)
    return "\n".join(out)


def _rewrite_links(text: str, rewrite: Any) -> str:
    text = INLINE_LINK.sub(lambda m: f"({rewrite(m['target'])}{m['title'] or ''})", text)
    return HTML_LINK.sub(
        lambda m: f"{m['attr']}{m['quote']}{rewrite(m['target'])}{m['quote']}", text
    )


def _site_path(repo_path: str, files: Files) -> str | None:
    """The site file that `repo_path` is or becomes, or None when the site has no such file."""
    if repo_path in ROOT_PAGES:
        return ROOT_PAGES[repo_path]
    if repo_path.startswith("docs/"):
        inside = repo_path.removeprefix("docs/")
        found = files.get_file_from_path(inside)
        if found is not None and not found.inclusion.is_excluded():
            return inside
    return None
