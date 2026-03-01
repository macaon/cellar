"""Centralised HTTP session factory.

Every outbound HTTP(S) request in Cellar goes through a
:func:`make_session`-created ``requests.Session`` so that User-Agent,
bearer-token auth, and SSL configuration are applied uniformly.
"""

from __future__ import annotations

import requests

#: Browser-like User-Agent string.  Avoids CDN/WAF bot-protection rules
#: that block Python's default ``User-Agent: python-requests/…``.
USER_AGENT = "Mozilla/5.0 (compatible; Cellar/1.0)"

#: Default per-request timeout: (connect_seconds, read_seconds).
DEFAULT_TIMEOUT = (10, 30)


def make_session(
    *,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> requests.Session:
    """Return a pre-configured :class:`requests.Session`.

    Parameters
    ----------
    token:
        Optional bearer token — added as an ``Authorization`` header.
    ssl_verify:
        Set to ``False`` to skip certificate verification entirely.
    ca_cert:
        Path to a CA bundle file.  Overrides the default trust store.
    """
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    if ca_cert:
        s.verify = ca_cert
    elif not ssl_verify:
        s.verify = False
    return s
