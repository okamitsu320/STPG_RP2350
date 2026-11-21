# Contributing

Use public synthetic reproductions for issues and pull requests. Never submit
private STIL, customer identifiers, real device bindings, raw bench captures or
generated artifacts containing source data. Keep changes small and identify
the exact source and imported SDK used for testing.

Run the portable Python/C tests and relevant CLI/GUI/browser/package checks.
Do not replace missing evidence with PASS or broadly skip safety tests. A
behavior/fingerprint change needs updated compatibility documentation and
negative tests, not merely an updated expected hash.

The core is distributed under the MIT license. Identify all copied, translated,
adapted or third-party material in changes and retain its notices. The separate
logic-analyzer tool has its own license and provenance notices; keep it out of
the core distribution.

Hardware work is never implied by review, tests, a demo flag or an earlier
experiment's approval. Do not flash, enumerate/query/arm devices, reset outputs
or run a pattern without the exact operator authority and safe bench conditions.
Public release and binary uploads require a separate owner decision.
