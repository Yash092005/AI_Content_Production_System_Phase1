"""Safe fetching of a URL typed by a user. The server must never be tricked into reading internal addresses (SSRF).

Defences, in order: only http/https on ports 80/443, no credentials in the URL, the host must resolve ONLY to public
addresses, redirects are followed by hand and every hop is checked again, and the address the socket actually
connects to is checked too (so a DNS answer that changes between the check and the connection is still caught)."""
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

ALLOWED_PORTS = {None, 80, 443}
MAX_URL_CHARS = 2000
BLOCKED_NAMES = {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata"}


class UnsafeUrl(ValueError):
    """The URL is not allowed. The message is written for the person who typed it."""


def is_public_ip(ip):
    try:
        addr = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_global and not addr.is_multicast)


def _domain_matches(host, domains):
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (x.lower().strip().lstrip(".") for x in domains or []) if d)


def validate_url(url, allowed_domains=(), blocked_domains=(), allow_private=False, resolver=socket.getaddrinfo):
    """Return the cleaned URL, or raise UnsafeUrl with a plain-language reason."""
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeUrl("Enter a website address.")
    if len(raw) > MAX_URL_CHARS:
        raise UnsafeUrl("That address is too long.")
    if "://" not in raw:
        if re.match(r"^(javascript|data|file|mailto|tel|ftp|vbscript|about|blob|view-source):", raw, re.I):
            raise UnsafeUrl("Only http and https addresses are allowed.")
        raw = "https://" + raw                       # people type "example.com"
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Only http and https addresses are allowed.")
    if parts.username or parts.password:
        raise UnsafeUrl("Addresses with a username or password are not allowed.")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeUrl("That address has no website name.")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeUrl("That address has an invalid port.")
    if port not in ALLOWED_PORTS and not allow_private:
        raise UnsafeUrl("Only the standard web ports (80 and 443) are allowed.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UnsafeUrl("That website name is not valid.")
    if host in BLOCKED_NAMES or host.endswith((".local", ".internal", ".localhost")):
        if not allow_private:
            raise UnsafeUrl("Internal addresses are not allowed.")
    if blocked_domains and _domain_matches(host, blocked_domains):
        raise UnsafeUrl("This website is on the blocked list in the system configuration.")
    if allowed_domains and not _domain_matches(host, allowed_domains):
        raise UnsafeUrl("Only the websites on the allowed list in the system configuration can be used.")
    if not allow_private:
        try:
            infos = resolver(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError):
            raise UnsafeUrl("That website name could not be found.")
        ips = {i[4][0] for i in infos}
        if not ips or not all(is_public_ip(ip) for ip in ips):
            raise UnsafeUrl("That address points to a private or internal network, which is not allowed.")
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


# ---- the connection itself: check the address the socket REALLY connected to ----
def _check_peer(sock):
    ip = sock.getpeername()[0]
    if not is_public_ip(ip):
        sock.close()
        raise UnsafeUrl("That address points to a private or internal network, which is not allowed.")


class _SafeHTTPConnection(HTTPConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _SafeHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _SafeHTTPPool(HTTPConnectionPool):
    ConnectionCls = _SafeHTTPConnection


class _SafeHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _SafeHTTPSConnection


class SafeAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _SafeHTTPPool, "https": _SafeHTTPSPool}


def make_session(allow_private=False):
    """A requests session that refuses private peers. Environment proxies are ignored on purpose (a proxy's address would
    hide the real destination from the check)."""
    s = requests.Session()
    s.trust_env = False
    if not allow_private:
        s.mount("http://", SafeAdapter())
        s.mount("https://", SafeAdapter())
    return s
