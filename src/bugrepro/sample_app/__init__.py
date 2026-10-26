"""Sample application package (demo fixture only, not part of the browser core)."""

from .server import create_server, serve

__all__ = ["create_server", "serve"]