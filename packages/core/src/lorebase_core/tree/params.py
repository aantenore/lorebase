"""Tree parameters fixed per Domain (design 6.2, AC-11)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BITS_PER_BYTE", "DEFAULT_LEAF_SIZE", "FANOUT", "NODE_FORMAT_VERSION", "TreeParams"]

BITS_PER_BYTE = 8

FANOUT = 1 << BITS_PER_BYTE
"""Children per branch node: one per value of the key-hash byte at the node depth."""

DEFAULT_LEAF_SIZE = 512
"""Default leaf threshold ``L``: a node is a leaf iff its subtree holds at most ``L`` entries."""

NODE_FORMAT_VERSION = 1
"""Version byte written in every node header."""


@dataclass(frozen=True, slots=True)
class TreeParams:
    """Parameters that are part of the hashing contract of a Domain's trees.

    They are fixed when the Domain is created: changing them changes every
    root hash of the same content.
    """

    leaf_size: int = DEFAULT_LEAF_SIZE

    def __post_init__(self) -> None:
        if self.leaf_size < 1:
            raise ValueError("leaf_size must be at least 1")
