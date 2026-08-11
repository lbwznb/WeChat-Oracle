"""Opt-in, read-only WeChat 4 local database helpers.

The service only snapshots source files and imports explicitly authorized
groups into the application's SQLite archive. Database keys remain in memory.
"""

from .crypto import key_fingerprint, verify_page1

__all__ = ["key_fingerprint", "verify_page1"]
