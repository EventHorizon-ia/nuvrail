# Security Policy

Nuvrail is a security tool: it sits between an AI agent and a real mailbox and
holds upstream email credentials. We treat vulnerabilities in it accordingly.
Thank you for helping keep Nuvrail and the people who self-host it safe.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**
Public issues disclose the problem to attackers before a fix is available.

Report privately through either channel:

1. **GitHub private vulnerability reporting (preferred).** Go to the
   repository's **Security** tab → **Report a vulnerability**
   (`https://github.com/agens-field/nuvrail/security/advisories/new`). This
   opens a private advisory visible only to you and the maintainers, keeps the
   whole exchange in one place, and lets us issue a coordinated advisory + CVE
   when the fix ships.
2. **Email.** `security@agensfield.com` — for reporters who cannot use GitHub
   advisories. If you want to encrypt, say so in a first low-detail message and
   we will exchange a key before you send specifics.

Please include, to the extent you can:

- the affected version / commit (see [Supported Versions](#supported-versions)),
- the component (IMAP proxy, SMTP proxy, gateway/API, web PWA, deployment
  tooling),
- a description of the issue and its security impact,
- reproduction steps or a proof of concept, and
- any suggested remediation.

Do **not** include live credentials, real mailbox contents, or personal data in
your report. A redacted reproduction is always sufficient — if it is not, tell
us and we will arrange a safe channel.

## Our Commitment (Disclosure Timeline)

We follow a coordinated-disclosure model. Timelines are targets, measured in
business days from our acknowledgement:

| Stage | Target |
|-------|--------|
| Acknowledge receipt | within **3 business days** |
| Initial assessment + severity (CVSS) | within **7 business days** |
| Fix or concrete mitigation plan | within **30 business days** (critical issues are expedited) |
| Coordinated public disclosure | within **90 days** of the report, or on fix release — whichever comes first, agreed with the reporter |

If we cannot meet a target we will tell you why and give a revised date rather
than going silent. If we go quiet past these windows, you are free to disclose
on your own timeline — but we would appreciate a heads-up first.

We will keep you updated through the process, credit you in the advisory and
release notes unless you ask us not to, and coordinate the disclosure date with
you.

## Supported Versions

Nuvrail is pre-1.0 (see the
[latest release](https://github.com/agens-field/nuvrail/releases/latest)) and moves fast. Security fixes are made
against the **latest release and `main`**; there are no long-term-support
branches yet.

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| Latest tagged release | ✅ |
| Older tagged releases | ❌ — please upgrade |

If you self-host, track the latest release. When we ship a security fix we will
note it in the changelog / release notes and, for anything material, publish
a GitHub Security Advisory.

## Scope

**In scope** — vulnerabilities in the code in this repository, including:

- **Auth & access control** — account tokens, per-agent isolation
  (one agent reaching another agent's mailbox), privilege escalation.
- **The approval boundary** — any way to make a write/send operation reach the
  real provider *without* an explicit approval decision (bypassing staging), or
  to defeat the unconditional EXPUNGE / permanent-deletion block.
- **Credential handling** — anything that discloses or weakens the AES-256-GCM
  encryption of upstream passwords / OAuth2 tokens at rest, or leaks
  `NUVRAIL_MASTER_KEY`.
- **Data exposure** — plaintext email metadata or bodies leaking where the
  design says they should not (e.g. in push payloads, logs, or error output).
- **Injection / proxy correctness** — IMAP/SMTP command injection, request
  smuggling, or parser flaws that let an agent escape the intended AI lane.
- **Web PWA** — XSS, CSRF, CSP bypass, session handling.
- **Deployment defaults** — insecure defaults in the shipped `docker-compose`
  / nginx / entrypoint configuration.

**Out of scope:**

- Issues that require a compromised host, root on the deployment machine, or a
  malicious operator — the operator is already trusted with the master key.
- Findings only reproducible in a modified/forked build, not on a clean release.
- Vulnerabilities in third-party dependencies with no exploitable path in
  Nuvrail (report those upstream; tell us if Nuvrail's usage makes them
  exploitable here).
- Missing hardening headers or best-practice recommendations with no concrete
  exploit. These are welcome as regular issues or PRs, not security reports.
- Reports from automated scanners without a demonstrated, Nuvrail-specific
  impact.
- Social engineering, physical attacks, and denial of service against hosted
  infrastructure you do not run.
- The commercial/enterprise package (rules engine, entitlements) is **not** in
  this repository and is out of scope here.

## Safe Harbor

We will not pursue or support legal action against researchers who:

- make a good-faith effort to follow this policy,
- test only against their **own** self-hosted instance (never a third party's
  mailbox or someone else's deployment),
- avoid privacy violations, data destruction, and service degradation, and
- give us reasonable time to remediate before public disclosure.

If in doubt about whether an action is authorized, ask first at
`security@agensfield.com`.

---

*This policy applies to the Nuvrail open-source core. Copyright © 2026 Agens
Field. Licensed under AGPL-3.0 — see [LICENSE](LICENSE).*
