# Security and privacy notes / 安全说明

**Status:** A lightweight personal NAS application prototype. **Not professionally audited or approved for public Internet exposure.**

## Already implemented (basic controls)
- Single-user password authentication. PBKDF2-HMAC-SHA256 salted hashing (320,000 iterations).
- Random session token stored as a hash, HttpOnly + SameSite=Lax cookie; Secure attribute can be enabled with `COOKIE_SECURE=1` after enabling HTTPS.
- POST actions use an authenticated CSRF token; parameterized SQLite queries; visible user text is HTML escaped.
- Simple IP-based rate limit for login attempts; anti-framing and content-type/security-related HTTP headers.
- Non-root application container, `no-new-privileges` and persistent SQLite data volume.

## Limitations and risks
1. The built-in Python HTTP server is only intended for single-user/home LAN use. **Never port-forward 8765 directly from the public Internet.** Prefer a VPN (such as Tailscale) or a carefully configured HTTPS reverse proxy.
2. Setup key is the first-install guard; use a unique unpredictable value. The public example value is rejected, but short/predictable nonexample values are not evaluated for entropy. Only trusted users should reach the setup page.
3. Login protection is per observed IP rather than against distributed attackers or reverse proxy abuse. Do not rely on it for public deployment.
4. The SQLite database contains private task content, username, credential hashes, session tokens (hashed) and financial/reward histories; **it is not encrypted at rest**.
5. Keep host backups outside this NAS. Misconfiguration, stolen administrator credentials, host compromise or accidental volume removal may expose/destroy data.
6. No formal dependency audit, code audit, penetration test, managed alerting, MFA, secure password reset or disaster recovery guarantee is included.
7. Use `COOKIE_SECURE=1` only for real HTTPS access. Avoid exposing sensitive data or setup secrets through logs, GitHub issues, screenshots, `.env` or reverse proxies.
8. Any prior sessions may remain active after a password change (existing application behavior); use private LAN and rotate admin credentials if access is suspected.

## Report vulnerabilities / 报告漏洞
Before opening a public issue containing exploit details, contact the repository maintainer privately or use GitHub private vulnerability reporting when enabled. Never post `.env`, actual DB files or raw access logs.
