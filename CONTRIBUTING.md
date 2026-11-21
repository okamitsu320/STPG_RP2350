# Contributing

This is an internal review candidate; public contribution/release procedures
are not active yet. Use public synthetic reproductions only. Never submit
private STIL, customer identifiers, real device bindings, raw bench captures or
generated artifacts containing source data. Keep changes small and identify
the exact source and imported SDK used for testing.

Run the portable Python/C tests and relevant CLI/GUI/browser/package checks.
Do not replace missing evidence with PASS or broadly skip safety tests. A
behavior/fingerprint change needs updated compatibility documentation and
negative tests, not merely an updated expected hash.

Original contributions are intended for MIT distribution, but you must identify
all copied, translated, adapted or third-party material and retain its notices.
Do not assert that an optional integration or a language rewrite avoids its
upstream obligations. LA provenance must be settled before publication.

Hardware work is never implied by review, tests, a demo flag or an earlier
experiment's approval. Do not flash, enumerate/query/arm devices, reset outputs
or run a pattern without the exact operator authority and safe bench conditions.
Public release and binary uploads require a separate owner decision.
