"""Portable integration adapters for Hermes Operator."""

from .base import (
    AdapterCommandError,
    AdapterError,
    AdapterHealth,
    AdapterResponseError,
    AdapterUnavailableError,
    HermesAdapter,
    HermesCapabilities,
    HermesTask,
    UnsafePathError,
)
from .hermes import HermesCLIAdapter, InMemoryHermesAdapter, KANBAN_CAPABILITIES
from .obsidian import (
    DEFAULT_ENV_KEYS,
    ObsidianAdapter,
    ProjectionResult,
    VaultDocument,
    discover_vault,
    merge_frontmatter,
    parse_frontmatter,
    split_frontmatter,
)

__all__ = (
    "AdapterCommandError",
    "AdapterError",
    "AdapterHealth",
    "AdapterResponseError",
    "AdapterUnavailableError",
    "DEFAULT_ENV_KEYS",
    "HermesAdapter",
    "HermesCapabilities",
    "HermesCLIAdapter",
    "HermesTask",
    "InMemoryHermesAdapter",
    "KANBAN_CAPABILITIES",
    "ObsidianAdapter",
    "ProjectionResult",
    "UnsafePathError",
    "VaultDocument",
    "discover_vault",
    "merge_frontmatter",
    "parse_frontmatter",
    "split_frontmatter",
)

