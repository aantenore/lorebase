"""Lorebase Guide: optional exploration assistant and query embeddings over Keeper.

Importing the package registers the `guide` settings section with `lorebase_platform`.
"""

from lorebase_guide.settings import GuideSettings, guide_settings

__version__ = "0.2.1"

__all__ = ["GuideSettings", "guide_settings"]
