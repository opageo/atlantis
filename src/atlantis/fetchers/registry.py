"""Registry for flood data fetchers."""

from collections.abc import Callable
from typing import Type

from atlantis.fetchers.base import AbstractFloodFetcher

# Global registry of fetchers
fetcher_registry: dict[str, Type[AbstractFloodFetcher]] = {}


def register_fetcher(name: str) -> Callable[[Type[AbstractFloodFetcher]], Type[AbstractFloodFetcher]]:
    """Decorator to register a fetcher class with the global registry.

    Args:
        name: The name to register the fetcher under.

    Returns:
        Decorator function.

    Example:
        @register_fetcher("gfm")
        class GFMFetcher(AbstractFloodFetcher):
            ...
    """

    def decorator(cls: Type[AbstractFloodFetcher]) -> Type[AbstractFloodFetcher]:
        if name in fetcher_registry:
            raise ValueError(f"Fetcher '{name}' already registered: {fetcher_registry[name].__name__}")
        fetcher_registry[name] = cls
        cls.source_id = name
        return cls

    return decorator


def get_fetcher(name: str) -> Type[AbstractFloodFetcher]:
    """Get a fetcher class by name.

    Args:
        name: The fetcher name.

    Returns:
        The fetcher class.

    Raises:
        KeyError: If the fetcher is not registered.
    """
    if name not in fetcher_registry:
        available = list(fetcher_registry.keys())
        raise KeyError(f"Fetcher '{name}' not found. Available: {available}")
    return fetcher_registry[name]


def list_fetchers() -> list[str]:
    """List all registered fetcher names.

    Returns:
        List of registered fetcher names.
    """
    return list(fetcher_registry.keys())


def clear_registry() -> None:
    """Clear the global fetcher registry. Useful for testing."""
    fetcher_registry.clear()
