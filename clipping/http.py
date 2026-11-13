"""HTTP helpers: browser-like session, retries, bot-challenge detection."""
from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,es;q=0.8,en;q=0.7",
}
NO_CACHE_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}

_local = threading.local()


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str = "") -> None:
        self.url = url
        self.status = status
        super().__init__(f"{status or 'ERR'} {url} {message}".strip())


def session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        retry = Retry(
            total=2,
            connect=0,          # connection failures: our callers retry once themselves
            read=1,
            status=2,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


def get(url: str, timeout: float = 25, no_cache: bool = False) -> requests.Response:
    headers = dict(NO_CACHE_HEADERS) if no_cache else None
    try:
        r = session().get(url, timeout=timeout, headers=headers, allow_redirects=True)
    except requests.RequestException as e:
        raise FetchError(url, None, type(e).__name__) from e
    if r.status_code >= 400:
        raise FetchError(url, r.status_code)
    return r


_CHALLENGE_MARKERS = (
    "just a moment...", "cf-chl", "attention required! | cloudflare", "px-captcha",
    "perimeterx", "access denied", "are you a robot", "captcha-delivery", "datadome",
)


def looks_like_challenge(html: str) -> bool:
    head = html[:6000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS) and len(html) < 60000
