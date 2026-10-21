"""Markdown reference of every setting, generated from `config/lorebase.example.yaml`.

The example file is the source of truth: it holds each default and, in comments, what the setting
does. This script reads the comments back (they are not part of the YAML data) and lays them out
as one table per group of settings. After parsing it checks its own reading against PyYAML: every
setting must exist in the loaded document with the default that the page prints, so a construct
the reader does not understand fails the generation instead of producing a wrong page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "config" / "lorebase.example.yaml"
TARGET = ROOT / "docs" / "reference" / "configuration.md"
SOURCE_URL = "https://github.com/aantenore/lorebase/blob/main/config/lorebase.example.yaml"

# A comment that continues the description of the key above starts at least this many columns
# right of that key; a comment at or near the indentation of keys describes the next key.
CONTINUATION_OFFSET = 4
HEADING_DEPTH = 3  # groups are named by at most this many path segments

KEY_LINE = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#:'"-][^:#]*?)\s*:(?:\s+(?P<rest>.*))?$""")
CODE_SPAN = re.compile(r"(`[^`]*`)")


@dataclass
class Setting:
    path: tuple[str, ...]
    value: str | None  # None: a mapping whose children follow
    notes: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return " ".join(note for note in self.notes if note).strip()


@dataclass
class Block:
    """Comment lines on their own, between blank lines: an example or a remark."""

    lines: list[str]


def _split_comment(text: str) -> tuple[str, str]:
    """`value  # comment` -> (`value`, `comment`), ignoring a hash inside quotes."""
    single = double = False
    for index, char in enumerate(text):
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == "#" and not single and not double and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip(), text[index + 1 :].strip()
    return text.rstrip(), ""


def _depth(text: str) -> int:
    """Open brackets of a flow value, outside quotes."""
    depth = 0
    single = double = False
    for char in text:
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif not single and not double:
            depth += char in "[{"
            depth -= char in "]}"
    return depth


def parse(text: str) -> tuple[list[str], list[Setting | Block]]:
    """The header comment lines and, in file order, the settings and the free comment blocks."""
    lines = text.split("\n")
    header: list[str] = []
    items: list[Setting | Block] = []
    stack: list[tuple[int, str]] = []  # (indent, key) of the mappings that are open
    pending: list[str] = []  # comment lines that precede the next key
    last: Setting | None = None
    last_indent = 0
    index = 0

    def flush_pending() -> None:
        nonlocal pending
        if pending:
            if not header and not items:
                header.extend(pending)
            else:
                items.append(Block(pending))
            pending = []

    while index < len(lines):
        raw = lines[index]
        index += 1
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if not stripped:
            flush_pending()
            last = None
        elif stripped.startswith("#"):
            body = stripped[1:]
            body = body.removeprefix(" ")
            if last is not None and last.notes and indent > last_indent + CONTINUATION_OFFSET:
                last.notes.append(body.strip())
            else:
                pending.append(body)
        elif stripped.startswith("- "):
            if last is None or (last.value is not None and not last.value.startswith("[")):
                raise ValueError(f"list item without a key: {raw!r}")
            if last.value is None:  # the key held a list, not a mapping
                stack.pop()
                last.value = "[]"
            item, _ = _split_comment(stripped[2:])
            last.value = last.value[:-1] + (", " if last.value != "[]" else "") + item + "]"
        else:
            match = KEY_LINE.match(stripped)
            if match is None:
                raise ValueError(f"cannot read line {index}: {raw!r}")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path = (*(key for _, key in stack), match["key"].strip("'\""))
            value_text, comment = _split_comment(match["rest"] or "")
            comments = [comment] if comment else []
            while _depth(value_text) > 0 and index < len(lines):  # a flow value over more lines
                more, more_comment = _split_comment(lines[index].strip())
                value_text = f"{value_text} {more}"
                comments.append(more_comment)
                index += 1
            notes = [line.strip() for line in pending] + [c for c in comments if c]
            pending = []
            setting = Setting(path, value_text or None, notes)
            items.append(setting)
            last, last_indent = setting, indent
            if not value_text:
                stack.append((indent, path[-1]))
    flush_pending()
    return header, items


def _check(items: list[Setting | Block], document: dict[str, Any]) -> None:
    """Every setting read from the comments exists in the YAML document with the same default."""
    for item in items:
        if not isinstance(item, Setting):
            continue
        node: Any = document
        for key in item.path:
            if not isinstance(node, dict) or key not in node:
                raise ValueError(f"{'.'.join(item.path)} is missing from the loaded document")
            node = node[key]
        if item.value is None:
            if not isinstance(node, dict):
                raise ValueError(f"{'.'.join(item.path)} should be a mapping")
        elif yaml.safe_load(item.value) != node:
            raise ValueError(f"{'.'.join(item.path)}: {item.value!r} differs from the YAML value")


def _text(text: str) -> str:
    """Markdown-safe text: pipes, angle brackets and stars outside code spans are escaped."""
    parts = CODE_SPAN.split(text)
    for number in range(0, len(parts), 2):
        parts[number] = (
            parts[number]
            .replace("|", "\\|")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("*", "\\*")
        )
    for number in range(1, len(parts), 2):
        parts[number] = parts[number].replace("|", "\\|")
    return "".join(parts)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _code(value: str) -> str:
    return "`" + value.replace("|", "\\|") + "`" if value else ""


def _group(setting: Setting) -> tuple[str, ...]:
    return setting.path[:-1][:HEADING_DEPTH]


def render() -> str:
    """The reference page as text."""
    text = SOURCE.read_text()
    header, items = parse(text)
    _check(items, yaml.safe_load(text))
    intros = {
        item.path: item
        for item in items
        if isinstance(item, Setting) and item.value is None and len(item.path) <= HEADING_DEPTH
    }
    out = [
        "---\n"
        "title: Configuration reference\n"
        "description: Every setting of the Lorebase services with its default, generated from "
        "the example configuration file.\n"
        "---\n\n"
        "<!-- Generated by scripts/docs/config_reference.py (make docs); do not edit. -->\n\n"
        "# Configuration reference\n\n"
        f"Every setting, with its default, as in [`config/lorebase.example.yaml`]({SOURCE_URL}). "
        "The services read one YAML file (`LOREBASE_CONFIG`), and an environment variable "
        "`LOREBASE__SECTION__KEY` overrides the key `section.key`; nested keys use more double "
        "underscores. Every key is optional.\n\n"
        "```text\n" + "\n".join(header) + "\n```\n"
    ]
    current: tuple[str, ...] | None = None
    open_table = False
    emitted: set[tuple[str, ...]] = set()

    def heading(group: tuple[str, ...]) -> None:
        """Headings of the group and of its parents that were not printed yet."""
        for size in range(1, len(group) + 1):
            part = group[:size]
            if part in emitted and size < len(group):
                continue
            again = " (continued)" if part in emitted else ""
            out.append(f"\n{'#' * (size + 1)} `{'.'.join(part)}`{again}\n")
            node = intros.get(part)
            if node is not None and node.description and part not in emitted:
                out.append(f"\n{_text(_capital(node.description))}\n")
            emitted.add(part)

    for item in items:
        if isinstance(item, Block):
            if current is not None:
                out.append("\nExample:\n\n```yaml\n" + _comment_lines(item.lines) + "\n```\n")
                open_table = False
            continue
        if item.value is None and (len(item.path) <= HEADING_DEPTH or not item.description):
            continue  # a group: its description is printed under its heading
        group = _group(item)
        if group != current:
            current = group
            heading(group)
            open_table = False
        if not open_table:
            out.append(
                "\n| Setting | Default | Description |\n|---------|---------|-------------|\n"
            )
            open_table = True
        out.append(
            f"| `{'.'.join(item.path)}` | {_code(item.value or '')} | {_text(item.description)} |\n"
        )
    return "".join(out).rstrip("\n") + "\n"


def _comment_lines(lines: list[str]) -> str:
    """A free comment block as it reads in the file, without its indentation."""
    return "\n".join(f"# {line}" if line else "#" for line in lines).rstrip()
