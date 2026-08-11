"""Read-only WeChat 4 local database research helpers.

Nothing in this package is imported by the production runtime.  Commands that
touch a WeChat process or copy a database require
``WO_EXPERIMENTAL_RAW_WECHAT=1``.
"""

from .crypto import key_fingerprint, verify_page1

__all__ = ["key_fingerprint", "verify_page1"]
