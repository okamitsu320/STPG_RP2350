# Security and private data

Use the GUI only on a trusted local machine. Its server is loopback-only by
default, with per-process token and Host/Origin checks; it is not a multi-user
or remote-access service. Do not expose it through a proxy, public bind address
or tunnel. A local process with equivalent user access may access local data;
device leases coordinate cooperating processes, not hostile applications.

The runner can drive connected hardware when explicitly enabled/confirmed.
Audit unfamiliar projects/patterns and verify electrical safety before Run.
Manifest hashes bind bytes; they do not prove an input is trustworthy or safe.
Do not weaken identity, path, timing or ownership checks to bypass a rejection.

Treat projects, compressed sources, maps, captures, results, bindings, logs,
screenshots and build metadata as potentially confidential. Do not attach them
to public issues. Reproduce problems with new synthetic input and redact ports,
serials, local paths and source-derived details. Token/secret scanning alone
does not detect confidential chip workloads.

Report vulnerabilities privately. On GitHub, go to **Security → Advisories**
and choose **Report a vulnerability** for this repository. If that option is
not available, ask the maintainer for a private reporting channel without
posting the details. Do not use a public issue for a vulnerability report
or include confidential pattern data.
No automatic telemetry or cloud upload is part of normal operation.
