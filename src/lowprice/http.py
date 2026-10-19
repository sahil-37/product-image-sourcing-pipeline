"""One pooled, retrying HTTP session for the whole process.

A full run makes ~11 requests per product (one search plus ten image
downloads). Using `requests.get`/`requests.post` directly opens a fresh TCP
connection for each and leaves it in TIME_WAIT, so a 292-row run burns through
tens of thousands of sockets. macOS's ephemeral port range is finite and the
run dies with:

    [Errno 49] Can't assign requested address

...at which point every remaining product fails instantly without ever
reaching the network. That is exactly how the first full run lost 209 rows.

A single `Session` with a sized connection pool fixes it: connections are kept
alive and reused rather than churned. The mounted `Retry` also rides out the
transient 429/500/503 responses that both Serper and image CDNs return under
sustained load, which the first run had no defence against either.
"""
from __future__ import annotations

import atexit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Wide enough that ten concurrent image hosts don't evict each other from the
# pool; small enough to stay a good citizen.
_POOL_CONNECTIONS = 32
_POOL_MAXSIZE = 32

_RETRY = Retry(
    total=3,
    connect=3,
    read=2,
    backoff_factor=0.7,  # 0.7s, 1.4s, 2.8s
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "POST"}),
    raise_on_status=False,
)

_session: requests.Session | None = None


def session() -> requests.Session:
    """The process-wide pooled session. Created once, reused everywhere."""
    global _session
    if _session is None:
        s = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=_POOL_CONNECTIONS,
            pool_maxsize=_POOL_MAXSIZE,
            max_retries=_RETRY,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        # Some CDNs (Walmart, BevMo) 403 an unrecognised client outright.
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        })
        _session = s
        atexit.register(_close)
    return _session


def _close() -> None:
    global _session
    if _session is not None:
        _session.close()
        _session = None
