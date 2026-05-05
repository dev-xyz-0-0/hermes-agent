#!/usr/bin/env python3
"""
domain_intel.py

Passive domain reconnaissance using Python stdlib only.

Capabilities:
- Subdomain discovery via crt.sh certificate transparency logs
- SSL/TLS certificate inspection
- WHOIS lookup via direct TCP
- DNS records via system DNS + Google DNS-over-HTTPS
- Domain availability heuristic
- Bulk multi-domain analysis with bounded parallelism

No third-party dependencies.
No API keys.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "0.1.0"

GOOGLE_DOH_URL = "https://dns.google/resolve"

MAX_BULK_DOMAINS = 20
DEFAULT_TIMEOUT = 8
DEFAULT_WORKERS = 6
DEFAULT_MAX_SUBDOMAINS = 200


WHOIS_SERVERS: Dict[str, str] = {
    # Generic / common
    "com": "whois.verisign-grs.com",
    "net": "whois.verisign-grs.com",
    "org": "whois.pir.org",
    "info": "whois.afilias.net",
    "biz": "whois.biz",
    "name": "whois.nic.name",
    "pro": "whois.registrypro.pro",
    "mobi": "whois.dotmobiregistry.net",
    "app": "whois.nic.google",
    "dev": "whois.nic.google",
    "page": "whois.nic.google",
    "new": "whois.nic.google",
    "xyz": "whois.nic.xyz",
    "io": "whois.nic.io",
    "ai": "whois.nic.ai",
    "co": "whois.nic.co",
    "me": "whois.nic.me",
    "tv": "whois.nic.tv",
    "cc": "whois.nic.cc",
    "cloud": "whois.nic.cloud",
    "online": "whois.nic.online",
    "site": "whois.nic.site",
    "store": "whois.nic.store",
    "tech": "whois.nic.tech",
    "space": "whois.nic.space",
    "website": "whois.nic.website",
    "shop": "whois.nic.shop",
    "blog": "whois.nic.blog",
    "club": "whois.nic.club",
    "live": "whois.nic.live",
    "world": "whois.nic.world",
    "today": "whois.nic.today",
    "email": "whois.nic.email",
    "solutions": "whois.nic.solutions",
    "systems": "whois.nic.systems",
    "network": "whois.nic.network",
    "company": "whois.nic.company",
    "digital": "whois.nic.digital",
    "finance": "whois.nic.finance",
    "capital": "whois.nic.capital",
    "exchange": "whois.nic.exchange",
    "fund": "whois.nic.fund",
    "ventures": "whois.nic.ventures",
    "holdings": "whois.nic.holdings",
    "agency": "whois.nic.agency",
    "media": "whois.nic.media",
    "social": "whois.nic.social",
    "software": "whois.nic.software",
    "tools": "whois.nic.tools",
    "run": "whois.nic.run",
    "zone": "whois.nic.zone",
    "link": "whois.nic.link",
    "one": "whois.nic.one",
    "finance": "whois.nic.finance",

    # Country-code / regional
    "sg": "whois.sgnic.sg",
    "com.sg": "whois.sgnic.sg",
    "net.sg": "whois.sgnic.sg",
    "org.sg": "whois.sgnic.sg",
    "my": "whois.mynic.my",
    "com.my": "whois.mynic.my",
    "id": "whois.id",
    "co.id": "whois.id",
    "th": "whois.thnic.co.th",
    "in": "whois.registry.in",
    "co.in": "whois.registry.in",
    "cn": "whois.cnnic.cn",
    "com.cn": "whois.cnnic.cn",
    "hk": "whois.hkirc.hk",
    "tw": "whois.twnic.net.tw",
    "jp": "whois.jprs.jp",
    "kr": "whois.kr",
    "au": "whois.auda.org.au",
    "com.au": "whois.auda.org.au",
    "nz": "whois.irs.net.nz",
    "uk": "whois.nic.uk",
    "co.uk": "whois.nic.uk",
    "org.uk": "whois.nic.uk",
    "de": "whois.denic.de",
    "fr": "whois.nic.fr",
    "it": "whois.nic.it",
    "nl": "whois.domain-registry.nl",
    "se": "whois.iis.se",
    "no": "whois.norid.no",
    "fi": "whois.fi",
    "dk": "whois.dk-hostmaster.dk",
    "es": "whois.nic.es",
    "pt": "whois.dns.pt",
    "ch": "whois.nic.ch",
    "at": "whois.nic.at",
    "be": "whois.dns.be",
    "pl": "whois.dns.pl",
    "cz": "whois.nic.cz",
    "ru": "whois.tcinet.ru",
    "ua": "whois.ua",
    "ca": "whois.cira.ca",
    "us": "whois.nic.us",
    "br": "whois.registro.br",
    "mx": "whois.mx",
    "za": "whois.registry.net.za",
}


WHOIS_NEGATIVE_MARKERS = [
    "no match",
    "not found",
    "no data found",
    "not registered",
    "domain not found",
    "no entries found",
    "status: free",
    "available for registration",
    "the queried object does not exist",
    "nothing found",
    "no such domain",
    "is available",
]


WHOIS_POSITIVE_MARKERS = [
    "domain name:",
    "registrar:",
    "creation date:",
    "created on:",
    "updated date:",
    "expiry date:",
    "expiration date:",
    "registry expiry date:",
    "name server:",
    "nserver:",
    "status:",
]


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def error_obj(message: str, error_type: str = "error") -> Dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "type": error_type,
            "message": message,
        },
    }


def normalize_domain(domain: str) -> str:
    """
    Normalize and validate a domain.

    Accepts:
    - example.com
    - https://example.com/path
    - *.example.com

    Returns IDNA ASCII domain.
    """
    if not domain:
        raise ValueError("empty domain")

    raw = domain.strip().lower()

    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        raw = parsed.hostname or ""

    raw = raw.strip().strip(".")
    raw = raw.removeprefix("*.")

    if "/" in raw:
        raw = raw.split("/", 1)[0]

    if ":" in raw and not raw.startswith("["):
        raw = raw.split(":", 1)[0]

    if not raw:
        raise ValueError("empty domain after normalization")

    try:
        ipaddress.ip_address(raw)
        raise ValueError("expected domain, got IP address")
    except ValueError as exc:
        if "expected domain" in str(exc):
            raise

    try:
        ascii_domain = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid IDNA domain: {exc}") from exc

    if len(ascii_domain) > 253:
        raise ValueError("domain too long")

    label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    labels = ascii_domain.split(".")
    if len(labels) < 2:
        raise ValueError("domain must include a TLD")

    for label in labels:
        if not label_re.match(label):
            raise ValueError(f"invalid domain label: {label}")

    return ascii_domain


def root_domain_guess(domain: str) -> str:
    """
    Lightweight root-domain guess.

    This is intentionally stdlib-only and does not use the Public Suffix List.
    For WHOIS server selection, we try longest matching TLD suffix first.
    """
    labels = domain.split(".")
    if len(labels) <= 2:
        return domain
    return ".".join(labels[-2:])


def tld_candidates(domain: str) -> List[str]:
    labels = domain.split(".")
    out = []
    for i in range(len(labels)):
        suffix = ".".join(labels[i:])
        out.append(suffix)
    return sorted(out, key=lambda x: x.count("."), reverse=True)


def http_json(url: str, timeout: int, debug: bool = False) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"domain-intel/{VERSION} stdlib",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    if debug:
        print(f"[debug] GET {url}", file=sys.stderr)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
        return json.loads(text)


def doh_query(domain: str, record_type: str, timeout: int, debug: bool = False) -> Dict[str, Any]:
    query = urllib.parse.urlencode({"name": domain, "type": record_type})
    url = f"{GOOGLE_DOH_URL}?{query}"

    try:
        data = http_json(url, timeout=timeout, debug=debug)
    except Exception as exc:
        return {
            "ok": False,
            "type": record_type,
            "answers": [],
            "error": str(exc),
        }

    answers = []
    for answer in data.get("Answer", []) or []:
        answers.append(
            {
                "name": answer.get("name"),
                "type": answer.get("type"),
                "ttl": answer.get("TTL"),
                "data": answer.get("data"),
            }
        )

    return {
        "ok": True,
        "type": record_type,
        "status": data.get("Status"),
        "answers": answers,
    }


def system_dns_records(domain: str, family: socket.AddressFamily, timeout: int) -> List[str]:
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        records = set()
        try:
            infos = socket.getaddrinfo(domain, None, family, socket.SOCK_STREAM)
        except socket.gaierror:
            return []
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                records.add(sockaddr[0])
        return sorted(records)
    finally:
        socket.setdefaulttimeout(old_timeout)


def dns_lookup(domain: str, timeout: int, debug: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": True,
        "domain": domain,
        "a": [],
        "aaaa": [],
        "mx": [],
        "ns": [],
        "txt": [],
        "cname": [],
        "errors": [],
    }

    try:
        result["a"] = system_dns_records(domain, socket.AF_INET, timeout)
    except Exception as exc:
        result["errors"].append({"record": "A", "message": str(exc)})

    try:
        result["aaaa"] = system_dns_records(domain, socket.AF_INET6, timeout)
    except Exception as exc:
        result["errors"].append({"record": "AAAA", "message": str(exc)})

    for rtype, key in [
        ("MX", "mx"),
        ("NS", "ns"),
        ("TXT", "txt"),
        ("CNAME", "cname"),
    ]:
        resp = doh_query(domain, rtype, timeout=timeout, debug=debug)
        if resp.get("ok"):
            result[key] = resp.get("answers", [])
        else:
            result["errors"].append({"record": rtype, "message": resp.get("error")})

    return result


def parse_ssl_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        ts = ssl.cert_time_to_seconds(value)
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
    except Exception:
        return None


def ssl_inspect(domain: str, timeout: int, port: int = 443) -> Dict[str, Any]:
    started = time.time()
    try:
        context = ssl.create_default_context()
        with socket.create_connection((domain, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
                cipher = ssock.cipher()
                tls_version = ssock.version()

        sans = []
        for typ, val in cert.get("subjectAltName", []) or []:
            if typ.lower() == "dns":
                sans.append(val)

        not_before = parse_ssl_time(cert.get("notBefore"))
        not_after = parse_ssl_time(cert.get("notAfter"))

        expires_in_days = None
        if not_after:
            expiry = _dt.datetime.fromisoformat(not_after)
            expires_in_days = int((expiry - _dt.datetime.now(_dt.timezone.utc)).total_seconds() // 86400)

        subject = {}
        for part in cert.get("subject", []) or []:
            for key, val in part:
                subject[key] = val

        issuer = {}
        for part in cert.get("issuer", []) or []:
            for key, val in part:
                issuer[key] = val

        return {
            "ok": True,
            "domain": domain,
            "port": port,
            "tls_version": tls_version,
            "cipher": {
                "name": cipher[0] if cipher else None,
                "protocol": cipher[1] if cipher else None,
                "bits": cipher[2] if cipher else None,
            },
            "subject": subject,
            "issuer": issuer,
            "serial_number": cert.get("serialNumber"),
            "not_before": not_before,
            "not_after": not_after,
            "expires_in_days": expires_in_days,
            "san_count": len(sans),
            "sans": sorted(set(sans)),
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    except Exception as exc:
        return {
            "ok": False,
            "domain": domain,
            "port": port,
            "error": str(exc),
            "elapsed_ms": int((time.time() - started) * 1000),
        }


def whois_tcp(server: str, query: str, timeout: int, debug: bool = False) -> str:
    if debug:
        print(f"[debug] WHOIS {server}:43 query={query}", file=sys.stderr)

    with socket.create_connection((server, 43), timeout=timeout) as sock:
        sock.settimeout(timeout)
        payload = (query + "\r\n").encode("utf-8", errors="replace")
        sock.sendall(payload)

        chunks = []
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)

    return b"".join(chunks).decode("utf-8", errors="replace")


def iana_whois_server(tld: str, timeout: int, debug: bool = False) -> Optional[str]:
    try:
        text = whois_tcp("whois.iana.org", tld, timeout=timeout, debug=debug)
    except Exception:
        return None

    for line in text.splitlines():
        if line.lower().startswith("whois:"):
            server = line.split(":", 1)[1].strip()
            return server or None
    return None


def select_whois_server(domain: str, timeout: int, debug: bool = False) -> Tuple[Optional[str], Optional[str]]:
    candidates = tld_candidates(domain)

    for suffix in candidates:
        if suffix in WHOIS_SERVERS:
            return WHOIS_SERVERS[suffix], suffix

    tld = domain.rsplit(".", 1)[-1]
    server = iana_whois_server(tld, timeout=timeout, debug=debug)
    if server:
        return server, tld

    return None, None


def parse_whois_summary(raw: str) -> Dict[str, Any]:
    lower = raw.lower()

    negative = any(marker in lower for marker in WHOIS_NEGATIVE_MARKERS)
    positive = any(marker in lower for marker in WHOIS_POSITIVE_MARKERS)

    fields: Dict[str, List[str]] = {}
    wanted = {
        "domain name": "domain_name",
        "registrar": "registrar",
        "creation date": "creation_date",
        "created on": "creation_date",
        "updated date": "updated_date",
        "expiry date": "expiry_date",
        "expiration date": "expiry_date",
        "registry expiry date": "expiry_date",
        "name server": "name_servers",
        "nserver": "name_servers",
        "status": "status",
    }

    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key_clean = key.strip().lower()
        val_clean = val.strip()
        if not val_clean:
            continue
        mapped = wanted.get(key_clean)
        if mapped:
            fields.setdefault(mapped, [])
            if val_clean not in fields[mapped]:
                fields[mapped].append(val_clean)

    compact_fields: Dict[str, Any] = {}
    for key, vals in fields.items():
        if key in {"name_servers", "status"}:
            compact_fields[key] = vals[:20]
        else:
            compact_fields[key] = vals[0] if vals else None

    return {
        "registered_signal": bool(positive and not negative),
        "available_signal": bool(negative and not positive),
        "positive_markers_found": positive,
        "negative_markers_found": negative,
        "fields": compact_fields,
    }


def whois_lookup(domain: str, timeout: int, debug: bool = False) -> Dict[str, Any]:
    started = time.time()

    server, matched_suffix = select_whois_server(domain, timeout=timeout, debug=debug)
    if not server:
        return {
            "ok": False,
            "domain": domain,
            "error": "no WHOIS server found",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    queries = [domain]

    # Verisign returns thin WHOIS for com/net and may include registrar WHOIS.
    try:
        raw = whois_tcp(server, domain, timeout=timeout, debug=debug)
    except Exception as exc:
        return {
            "ok": False,
            "domain": domain,
            "server": server,
            "matched_suffix": matched_suffix,
            "error": str(exc),
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    referral_server = None
    for line in raw.splitlines():
        if line.lower().startswith("registrar whois server:"):
            referral_server = line.split(":", 1)[1].strip()
            break

    referral_raw = None
    if referral_server and referral_server.lower() != server.lower():
        try:
            referral_raw = whois_tcp(referral_server, domain, timeout=timeout, debug=debug)
        except Exception:
            referral_raw = None

    final_raw = referral_raw or raw
    summary = parse_whois_summary(final_raw)

    return {
        "ok": True,
        "domain": domain,
        "server": server,
        "matched_suffix": matched_suffix,
        "referral_server": referral_server,
        "queried": queries,
        "summary": summary,
        "raw_excerpt": final_raw[:4000],
        "raw_length": len(final_raw),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def crtsh_subdomains(domain: str, timeout: int, max_subdomains: int, debug: bool = False) -> Dict[str, Any]:
    started = time.time()
    query = f"%.{domain}"
    url = "https://crt.sh/?" + urllib.parse.urlencode({"q": query, "output": "json"})

    try:
        data = http_json(url, timeout=timeout, debug=debug)
    except Exception as exc:
        return {
            "ok": False,
            "domain": domain,
            "source": "crt.sh",
            "error": str(exc),
            "subdomains": [],
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    names = set()
    cert_count = 0

    if isinstance(data, list):
        cert_count = len(data)
        for row in data:
            if not isinstance(row, dict):
                continue

            raw_name = row.get("name_value") or row.get("common_name") or ""
            for item in str(raw_name).splitlines():
                item = item.strip().lower().strip(".")
                item = item.removeprefix("*.")
                if not item:
                    continue
                if item == domain or item.endswith("." + domain):
                    try:
                        names.add(normalize_domain(item))
                    except Exception:
                        pass

    subdomains = sorted(names)
    truncated = False
    if len(subdomains) > max_subdomains:
        subdomains = subdomains[:max_subdomains]
        truncated = True

    return {
        "ok": True,
        "domain": domain,
        "source": "crt.sh",
        "cert_rows_seen": cert_count,
        "count": len(subdomains),
        "truncated": truncated,
        "subdomains": subdomains,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def availability_check(domain: str, timeout: int, debug: bool = False) -> Dict[str, Any]:
    """
    Heuristic only.

    A domain is likely registered if:
    - DNS returns A/AAAA/NS/MX, or
    - WHOIS has positive registration markers, or
    - SSL handshake works.

    A domain is likely available if:
    - DNS has no useful records,
    - WHOIS has negative markers,
    - SSL does not work.

    Otherwise result is unknown.
    """
    dns = dns_lookup(domain, timeout=timeout, debug=debug)
    whois = whois_lookup(domain, timeout=timeout, debug=debug)
    ssl_result = ssl_inspect(domain, timeout=timeout)

    dns_positive = bool(
        dns.get("a")
        or dns.get("aaaa")
        or dns.get("ns")
        or dns.get("mx")
    )

    whois_summary = whois.get("summary", {}) if whois.get("ok") else {}
    whois_registered = bool(whois_summary.get("registered_signal"))
    whois_available = bool(whois_summary.get("available_signal"))
    ssl_positive = bool(ssl_result.get("ok"))

    signals = {
        "dns_positive": dns_positive,
        "whois_registered_signal": whois_registered,
        "whois_available_signal": whois_available,
        "ssl_positive": ssl_positive,
    }

    if dns_positive or whois_registered or ssl_positive:
        verdict = "likely_registered"
        available = False
        confidence = "medium"
    elif whois_available and not dns_positive and not ssl_positive:
        verdict = "likely_available"
        available = True
        confidence = "medium"
    else:
        verdict = "unknown"
        available = None
        confidence = "low"

    return {
        "ok": True,
        "domain": domain,
        "verdict": verdict,
        "available": available,
        "confidence": confidence,
        "signals": signals,
        "dns_summary": {
            "a_count": len(dns.get("a", [])),
            "aaaa_count": len(dns.get("aaaa", [])),
            "mx_count": len(dns.get("mx", [])),
            "ns_count": len(dns.get("ns", [])),
        },
        "whois_summary": whois_summary,
        "ssl_summary": {
            "ok": ssl_result.get("ok"),
            "expires_in_days": ssl_result.get("expires_in_days"),
            "error": ssl_result.get("error"),
        },
    }


def analyze_domain(
    domain: str,
    mode: str,
    timeout: int,
    max_subdomains: int,
    debug: bool = False,
) -> Dict[str, Any]:
    started = time.time()

    try:
        normalized = normalize_domain(domain)
    except Exception as exc:
        return {
            "ok": False,
            "input": domain,
            "error": {
                "type": "invalid_domain",
                "message": str(exc),
            },
            "timestamp": utc_now(),
        }

    result: Dict[str, Any] = {
        "ok": True,
        "skill": "domain-intel",
        "version": VERSION,
        "input": domain,
        "domain": normalized,
        "mode": mode,
        "timestamp": utc_now(),
        "data": {},
        "elapsed_ms": None,
    }

    try:
        if mode in {"dns", "all"}:
            result["data"]["dns"] = dns_lookup(normalized, timeout=timeout, debug=debug)

        if mode in {"ssl", "all"}:
            result["data"]["ssl"] = ssl_inspect(normalized, timeout=timeout)

        if mode in {"whois", "all"}:
            result["data"]["whois"] = whois_lookup(normalized, timeout=timeout, debug=debug)

        if mode in {"subdomains", "all"}:
            result["data"]["subdomains"] = crtsh_subdomains(
                normalized,
                timeout=timeout,
                max_subdomains=max_subdomains,
                debug=debug,
            )

        if mode in {"availability", "all"}:
            result["data"]["availability"] = availability_check(
                normalized,
                timeout=timeout,
                debug=debug,
            )

    except Exception as exc:
        result["ok"] = False
        result["error"] = {
            "type": "analysis_error",
            "message": str(exc),
        }

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def parse_domains(args: argparse.Namespace) -> List[str]:
    domains: List[str] = []

    if args.domain:
        domains.append(args.domain)

    if args.domains:
        for item in args.domains.split(","):
            item = item.strip()
            if item:
                domains.append(item)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            for line in fh:
                item = line.strip()
                if not item or item.startswith("#"):
                    continue
                domains.append(item)

    deduped = []
    seen = set()
    for item in domains:
        key = item.strip().lower()
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-intel",
        description="Passive domain intelligence using Python stdlib only.",
    )

    parser.add_argument("--domain", help="Single domain to analyze, e.g. example.com")
    parser.add_argument("--domains", help="Comma-separated domains, max 20")
    parser.add_argument("--file", help="File containing domains, one per line, max 20")
    parser.add_argument(
        "--mode",
        choices=["all", "dns", "ssl", "whois", "subdomains", "availability"],
        default="all",
        help="Analysis mode",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Network timeout seconds, default {DEFAULT_TIMEOUT}",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers for bulk mode, default {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--max-subdomains",
        type=int,
        default=DEFAULT_MAX_SUBDOMAINS,
        help=f"Maximum subdomains returned per domain, default {DEFAULT_MAX_SUBDOMAINS}",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug logs to stderr",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print version JSON and exit",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(json.dumps({"ok": True, "skill": "domain-intel", "version": VERSION}))
        return 0

    domains = parse_domains(args)

    if not domains:
        out = error_obj("provide --domain, --domains, or --file", "usage_error")
        print(json.dumps(out, indent=2 if args.pretty else None, sort_keys=True))
        return 2

    if len(domains) > MAX_BULK_DOMAINS:
        out = error_obj(
            f"too many domains: {len(domains)}; max allowed is {MAX_BULK_DOMAINS}",
            "limit_error",
        )
        print(json.dumps(out, indent=2 if args.pretty else None, sort_keys=True))
        return 2

    timeout = max(1, min(args.timeout, 30))
    workers = max(1, min(args.workers, MAX_BULK_DOMAINS))
    max_subdomains = max(1, min(args.max_subdomains, 5000))

    if len(domains) == 1:
        out = analyze_domain(
            domains[0],
            mode=args.mode,
            timeout=timeout,
            max_subdomains=max_subdomains,
            debug=args.debug,
        )
    else:
        started = time.time()
        results = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    analyze_domain,
                    domain,
                    args.mode,
                    timeout,
                    max_subdomains,
                    args.debug,
                )
                for domain in domains
            ]

            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(
                        {
                            "ok": False,
                            "error": {
                                "type": "worker_error",
                                "message": str(exc),
                            },
                            "timestamp": utc_now(),
                        }
                    )

        results.sort(key=lambda x: str(x.get("domain") or x.get("input") or ""))

        out = {
            "ok": True,
            "skill": "domain-intel",
            "version": VERSION,
            "mode": args.mode,
            "timestamp": utc_now(),
            "count": len(results),
            "results": results,
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    print(json.dumps(out, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())