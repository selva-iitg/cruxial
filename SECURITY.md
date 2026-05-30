# Security Policy

Cruxial sits in the runtime path of every LLM tool call. Security issues here
have outsized impact, so we take them seriously and respond fast.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✓         |

Once a newer minor or major version ships, the previous one continues to receive
security fixes for **90 days** to give consumers time to upgrade.

## Reporting a vulnerability

**Please email `hello@cruxial.ai` with the subject line starting with `[SECURITY]`** —
do **not** open a public GitHub issue.

Include:

- A description of the issue and why it's a security concern
- Steps to reproduce (or a proof-of-concept)
- Affected version(s) and environment
- Any potential impact you've identified
- Whether you've disclosed this anywhere else

You will get an acknowledgement within **48 hours**.

For confirmed critical issues we will:

1. Triage and reproduce within 72 hours
2. Develop and validate a fix
3. Ship a patched release within 7 days
4. Publicly credit you in the release notes if you'd like (optional)

We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure) —
we'll work with you to time public disclosure after a fix is available and
consumers have had a reasonable window to upgrade.

## Scope

### In scope

- The `cruxial` Python package on PyPI and this repository
- The Cruxial CLI (`cruxial stats`, `cruxial diagnostic`)
- All adapters (`cruxial.adapters.openai`, `cruxial.adapters.anthropic`,
  `cruxial.adapters.mcp`)
- The `cruxial.lint` module

### Out of scope

- Third-party dependencies (please report directly to their maintainers)
- The cruxial.ai marketing site (not a security risk for the SDK itself)
- Issues that require an attacker already having local execution on a user's
  machine
- Social engineering against Cruxial maintainers
- Best-practice suggestions that aren't tied to a specific vulnerability

## What we already know

The [`DEFENSIVE.md`](DEFENSIVE.md) file documents the security and reliability
contract of the SDK in detail — what Cruxial guarantees, what its documented
limitations are, and where the boundary of responsibility lies between the
interceptor and the host application. Worth reading before you report — your
finding may already be a documented limitation rather than a vulnerability.

## Researcher acknowledgements

We're happy to publicly credit researchers who responsibly disclose
vulnerabilities. Tell us in your report whether you'd like to be acknowledged
and how (name, handle, URL).

## Thanks

For taking the time to make Cruxial — and the broader LLM tool-call ecosystem
— safer.
