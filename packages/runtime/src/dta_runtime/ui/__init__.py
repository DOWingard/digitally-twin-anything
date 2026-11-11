"""The twin's browser interface.

`create_app(settings)` returns the Dash application; the runtime serves its
Flask server. It reads the twin only through the twin's HTTP API.
"""

from .app import create_app

__all__ = ["create_app"]
