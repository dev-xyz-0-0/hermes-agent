---
name: domain-intel
description: Passive domain reconnaissance using Python stdlib. Use this skill for subdomain discovery, SSL certificate inspection, WHOIS lookups, DNS records, domain availability checks, and bulk multi-domain analysis. No API keys required. Triggers on requests like "find subdomains", "check ssl cert", "whois lookup", "is this domain available", "bulk check these domains".
license: MIT
---

# domain-intel

Passive domain intelligence using only Python stdlib and public data sources.

Zero dependencies.  
Zero API keys.  
Works out of the box.

## When to use

Use this skill when the user asks to:

- Find subdomains for a domain
- Check SSL/TLS certificate expiry
- Inspect certificate SANs, issuer, cipher, and TLS version
- Run WHOIS lookup
- Check DNS records
- Check whether a domain is likely available
- Analyze many domains at once

Example user requests:

- "find subdomains for example.com"
- "check ssl cert for example.com"
- "whois lookup openai.com"
- "is mynewdomain123.com available?"
- "bulk check these domains"
- "show DNS records for example.com"

## Capabilities

- Subdomain discovery via crt.sh certificate transparency logs
- Live SSL/TLS certificate inspection
- WHOIS lookup through direct TCP queries
- DNS records:
  - A
  - AAAA
  - MX
  - NS
  - TXT
  - CNAME
- Domain availability check using DNS, WHOIS, and SSL signals
- Bulk multi-domain analysis in parallel, up to 20 domains

## Data sources

- `crt.sh` certificate transparency logs
- WHOIS servers through direct TCP port 43 queries
- Google DNS-over-HTTPS for MX, NS, TXT, and CNAME
- System DNS resolver for A and AAAA records

## Safety and scope

This is a passive reconnaissance skill.

It does not:

- Scan ports
- Brute force subdomains
- Send exploit payloads
- Attempt login
- Crawl private infrastructure
- Bypass access controls
- Use API keys
- Store secrets

Network activity is limited to public DNS, WHOIS, HTTPS certificate transparency lookup, and SSL certificate inspection.

## Install layout

Expected skill layout:

```text
skills/domain-intel/
  SKILL.md
  src/
    domain_intel.py
````

Make the script executable:

```bash
chmod +x skills/domain-intel/src/domain_intel.py
```

## Usage

From the skill directory or repo root:

```bash
python skills/domain-intel/src/domain_intel.py --domain example.com --mode all --pretty
```

With `uv`:

```bash
uv run python skills/domain-intel/src/domain_intel.py --domain example.com --mode all --pretty
```

## Modes

### Full analysis

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domain example.com \
  --mode all \
  --pretty
```

### DNS records

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domain example.com \
  --mode dns \
  --pretty
```

### SSL certificate inspection

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domain example.com \
  --mode ssl \
  --pretty
```

### WHOIS lookup

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domain example.com \
  --mode whois \
  --pretty
```

### Subdomain discovery

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domain example.com \
  --mode subdomains \
  --max-subdomains 200 \
  --pretty
```

### Domain availability check

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domain example.com \
  --mode availability \
  --pretty
```

### Bulk check

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --domains example.com,openai.com,github.com \
  --mode availability \
  --workers 6 \
  --pretty
```

### Bulk check from file

```bash
uv run python skills/domain-intel/src/domain_intel.py \
  --file domains.txt \
  --mode all \
  --workers 6 \
  --pretty
```

`domains.txt` format:

```text
example.com
openai.com
github.com
```

## CLI arguments

| Argument           | Description                                                       |
| ------------------ | ----------------------------------------------------------------- |
| `--domain`         | Single domain to analyze                                          |
| `--domains`        | Comma-separated list of domains                                   |
| `--file`           | File containing one domain per line                               |
| `--mode`           | One of `all`, `dns`, `ssl`, `whois`, `subdomains`, `availability` |
| `--timeout`        | Network timeout in seconds, default `8`, max `30`                 |
| `--workers`        | Parallel workers for bulk mode, default `6`, max `20`             |
| `--max-subdomains` | Maximum subdomains returned per domain, default `200`, max `5000` |
| `--pretty`         | Pretty-print JSON                                                 |
| `--debug`          | Print debug logs to stderr                                        |
| `--version`        | Print version JSON                                                |

## Output schema

Single-domain output:

```json
{
  "ok": true,
  "skill": "domain-intel",
  "version": "0.1.0",
  "input": "example.com",
  "domain": "example.com",
  "mode": "all",
  "timestamp": "2026-05-05T12:00:00+00:00",
  "data": {
    "dns": {},
    "ssl": {},
    "whois": {},
    "subdomains": {},
    "availability": {}
  },
  "elapsed_ms": 1234
}
```

Bulk output:

```json
{
  "ok": true,
  "skill": "domain-intel",
  "version": "0.1.0",
  "mode": "availability",
  "timestamp": "2026-05-05T12:00:00+00:00",
  "count": 3,
  "results": [],
  "elapsed_ms": 2345
}
```

Error output:

```json
{
  "ok": false,
  "error": {
    "type": "usage_error",
    "message": "provide --domain, --domains, or --file"
  }
}
```

## DNS output

```json
{
  "ok": true,
  "domain": "example.com",
  "a": ["93.184.216.34"],
  "aaaa": [],
  "mx": [],
  "ns": [],
  "txt": [],
  "cname": [],
  "errors": []
}
```

## SSL output

```json
{
  "ok": true,
  "domain": "example.com",
  "port": 443,
  "tls_version": "TLSv1.3",
  "cipher": {
    "name": "TLS_AES_256_GCM_SHA384",
    "protocol": "TLSv1.3",
    "bits": 256
  },
  "subject": {},
  "issuer": {},
  "serial_number": "ABC123",
  "not_before": "2026-01-01T00:00:00+00:00",
  "not_after": "2026-04-01T00:00:00+00:00",
  "expires_in_days": 42,
  "san_count": 2,
  "sans": ["example.com", "www.example.com"],
  "elapsed_ms": 100
}
```

## WHOIS output

```json
{
  "ok": true,
  "domain": "example.com",
  "server": "whois.verisign-grs.com",
  "matched_suffix": "com",
  "referral_server": "whois.example-registrar.com",
  "summary": {
    "registered_signal": true,
    "available_signal": false,
    "positive_markers_found": true,
    "negative_markers_found": false,
    "fields": {
      "domain_name": "EXAMPLE.COM",
      "registrar": "Example Registrar",
      "creation_date": "1995-08-14T04:00:00Z",
      "expiry_date": "2026-08-13T04:00:00Z",
      "name_servers": [],
      "status": []
    }
  },
  "raw_excerpt": "...",
  "raw_length": 5000,
  "elapsed_ms": 300
}
```

## Availability output

```json
{
  "ok": true,
  "domain": "example.com",
  "verdict": "likely_registered",
  "available": false,
  "confidence": "medium",
  "signals": {
    "dns_positive": true,
    "whois_registered_signal": true,
    "whois_available_signal": false,
    "ssl_positive": true
  },
  "dns_summary": {
    "a_count": 1,
    "aaaa_count": 0,
    "mx_count": 0,
    "ns_count": 2
  },
  "whois_summary": {},
  "ssl_summary": {
    "ok": true,
    "expires_in_days": 42,
    "error": null
  }
}
```

## Notes

WHOIS availability is not perfectly deterministic across all TLDs. Some registries rate-limit, redact, or return non-standard responses.

The availability mode should be treated as a best-effort heuristic:

* `likely_registered`
* `likely_available`
* `unknown`

For production use, confirm domain purchases through a registrar before taking action.

## Recommended Hermes behavior

When the user asks for a narrow task, use the narrowest mode:

* Subdomains only: `--mode subdomains`
* SSL only: `--mode ssl`
* WHOIS only: `--mode whois`
* DNS only: `--mode dns`
* Availability only: `--mode availability`

Use `--mode all` only when the user asks for full reconnaissance or broad domain intelligence.

## Example Hermes command mapping

User:

```text
check ssl cert for example.com
```

Run:

```bash
uv run python skills/domain-intel/src/domain_intel.py --domain example.com --mode ssl --pretty
```

User:

```text
find subdomains for example.com
```

Run:

```bash
uv run python skills/domain-intel/src/domain_intel.py --domain example.com --mode subdomains --pretty
```

User:

```text
is example123domain.com available?
```

Run:

```bash
uv run python skills/domain-intel/src/domain_intel.py --domain example123domain.com --mode availability --pretty
```

User:

```text
bulk check example.com, openai.com, github.com
```

Run:

```bash
uv run python skills/domain-intel/src/domain_intel.py --domains example.com,openai.com,github.com --mode all --pretty
```
