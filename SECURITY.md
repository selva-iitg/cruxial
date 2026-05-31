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

## Known intentional behaviors

These are behaviors that may show up in automated supply-chain scanners
(Socket.dev, Snyk, Phylum, etc.) but are intentional design choices, not
vulnerabilities. We document them here so a security reviewer can quickly
distinguish "this is by design" from "this is something worth reporting."

### Subprocess spawning in `cruxial.adapters.mcp.import_server_stdio`

The Model Context Protocol (MCP) defines two transports: HTTP/SSE (remote)
and **stdio** (local). The stdio transport spawns the MCP server as a child
process and communicates with it over the child's stdin/stdout pipes. This
is fundamental to the protocol — any MCP client supporting stdio servers
spawns subprocesses.

**Cruxial's hardening at this boundary:**

- The actual subprocess spawn happens in the upstream `mcp` Python SDK,
  which uses `shell=False` (no shell interpolation, arguments passed
  positionally via `execvp`).
- `cruxial.adapters.mcp._validate_command()` rejects empty strings, non-string
  types, and shell metacharacters (`| ; & \` $ ( ) < > \n`) before calling the
  MCP SDK. Shell metacharacters in the command string would silently not
  behave as expected (since no shell is involved) and almost always indicate
  a confused caller — fail fast with a useful error.
- `cruxial.adapters.mcp._validate_args()` validates `args` is a list of strings
  (shell metacharacters are permitted in `args` because they are passed
  positionally via `execvp` and never reach a shell).

**Caller's responsibility:**

`command` and `args` MUST come from a trusted source — typically a hard-coded
constant, an environment-specific config file, or another trust-boundary-controlled
input. Cruxial's validation reduces footguns but does not turn untrusted user
input into trusted input. Treat `import_server_stdio(command=...)` with the
same care you would treat `subprocess.run([command, ...])` in your own code.

### Local SQLite telemetry file

`cruxial.telemetry.SQLiteSink` writes interception events to a local SQLite
database (default location: `~/.cruxial/telemetry.sqlite` or the project-local
`./.cruxial/telemetry.sqlite`). The database stores:

- Tool name, failure category, timestamps, latency in ms
- **Hashes** of the raw tool arguments (SHA-256, never the values themselves)
- Schema hash for the registered tool
- Cruxial version, Python version

Cruxial never persists raw tool-call arguments to disk. The hash is intended
for deduplication and "how often does this exact bad payload show up?" queries
without recording PII or secrets.

### No outbound network calls

Cruxial itself makes no network calls. The only network activity in the SDK
comes through:

1. The optional adapters (`cruxial.adapters.openai`, `cruxial.adapters.anthropic`)
   which call the LLM provider you have already chosen, using your own credentials.
2. `cruxial.adapters.mcp.import_server_sse(url=...)` if you explicitly call it
   with a URL.

There is no telemetry, no analytics, no auto-update check, no "phone home"
behavior. The SDK runs entirely in your process.

## Researcher acknowledgements

We're happy to publicly credit researchers who responsibly disclose
vulnerabilities. Tell us in your report whether you'd like to be acknowledged
and how (name, handle, URL).

## Thanks

For taking the time to make Cruxial — and the broader LLM tool-call ecosystem
— safer.
