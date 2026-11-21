# Run evidence and bundle migration

These APIs are host tools, not physical-test authorization. No offline command
opens a port, performs recovery, or grants permission to execute a pattern.

## Compressed result records

New runs checkpoint **only `run.json.gz`**, atomically replacing one file. The
decompressed schema is the existing `stpg_runner_run_result`, version 1. No
transaction or read-slice evidence is dropped. Gzip uses level 6, no filename,
and mtime 0; equal JSON produces equal stored bytes. This is compression, not
encryption or redaction: raw evidence can contain paths, ports and USB identity.
An atomic checkpoint is not a multi-file transaction or proof of power-loss
durability. A failed final checkpoint must not be treated as persisted success.

```python
from pathlib import Path
from stpg_runner import read_run_result, run_result_path

report = read_run_result(Path("my-result-folder"))
stored_path = run_result_path(Path("my-result-folder"))
```

Both accept a folder or an explicit `run.json.gz` / legacy `run.json` path.
Both reject a folder containing both names, even if one was explicitly named.
Missing files, symlinks, duplicate JSON keys, non-finite numbers, corrupt gzip
and unsupported result kind/schema are errors. `read_run_result` optionally
accepts `expected_sha256`, which hashes stored (compressed, when applicable)
bytes. GUI and companion should use this reader rather than decompressing
independently. Do not rename an old JSON file to `.gz`.

## Retained mismatch entries

After a strictly validated DONE or HOLD terminal with a nonzero fail count,
the executor reads `min(fail_count, 341)` retained FAIL_LOG entries. It never
reads after a device ERROR or a nonterminal STATUS. Zero failures produce
`NOT_NEEDED` without any read command. There is no additional fail-summary
query, sample payload collection, resend, retry or reset.

Each page contains at most two 16-byte entries. At most **171 requests per
segment** are added. The ready response, page payload and trailer each use the
ordinary 2-second receive window: the maximum nominal sum is **1026 seconds**
per segment, not an OS/I/O-inclusive elapsed-time bound. Normal successful
pages usually take much less time; this is not a measured timing guarantee.
The offline plan exposes these limits in `fail_log_collection`. Reading while
HOLD **extends the time before the next segment**, with the existing held
levels; applications must disclose this before physical admission.

The terminal is persisted first, then the existing after-terminal accessory
hook runs, then the log is read before any next upload or successful release.
Every validated page is checkpointed. A bad/partial reply, cancellation or
checkpoint failure stops subsequent requests, including release; finally only
the existing once-close occurs. Closing a port is not electrical release.
Progress events `fail_log/start`, `fail_log/progress`, and `fail_log/done` name
the segment and count validated retained entries, never waveform progress.

`segments[].comparison` records the observed comparison separately from the
invocation outcome. A log read failure can therefore be `outcome=REJECT` and
`comparison=MISMATCH`; it does not erase the validated mismatching terminal.
Each segment's `fail_log` contains:

- `status`: `NOT_NEEDED`, `PENDING`, `READ`, `READ_ERROR`, or `NOT_COLLECTED_ERROR`;
- `reported_fail_count`, `entries_read`, `overflow`, and retained `entries`;
- entries with `event_index`, `actual`, `expected`, and `mask` (unsigned words).

Overflow describes the device's 341-entry storage limit, not missing USB data.
`PENDING` can survive an interruption or an accessory-hook failure before the
read. Complete log retrieval is not proof of all samples, DUT correctness,
waveform timing, absence of glitches or electrical continuity.

## Offline STIL description

```python
from stpg_runner import describe_result

view = describe_result(bundle_dir, manifest_sha256=manifest_sha256,
                       result=result_folder_or_loaded_dict)
```

CLI: `python -m stpg_runner describe BUNDLE --manifest-sha256 SHA256 --result RESULT_FOLDER`.
Replace the uppercase arguments with the selected paths and exact manifest hash.
The bundle is verified with the current validators and rehashed after mapping.
The result must identify that exact manifest and the exact pattern bytes.
Description never queries a device or silently rebinds a result to another bundle.

By default, completed matching is reused **within the same process** for the
same result snapshot, exact bundle manifest, bundle location and installed SDK.
Each call still hashes every bundle file, checks missing/extra files and rejects
symlinks. Changes are not inferred from timestamps or sizes. A changed result
or SDK cannot reuse its previous matching. Only successful descriptions and
manifest bytes are cached (at most 8 entries and 8 MiB total); raw journals,
maps and binaries are not retained in that cache. Returned objects are independent.
Legacy bundles can use this offline reuse too; it does not grant permission to
run them with a different SDK. No receipt is loaded from disk.

Use `reuse=False` for fresh semantic verification and matching. Optional
`on_progress=callback` and `cancel=CancellationToken()` follow the host-workflow
contract: stages `result_description` (cold) or `result_description_reuse`
(warm), with elapsed-time heartbeats for slow work, not fabricated percentages.
Final inventory hashing follows those mapping stages. Cancellation is cooperative,
not an interrupt of a decoder. Failed/cancelled work is not cached. Concurrent
cold calls may duplicate work; hashes are not an atomic snapshot against an
uncooperative concurrent writer. Restarting the process discards all reuse.

The returned `stpg_result_description` has per-segment source ranges, observed
terminal/counts, fail-log status and structured entries with source and failed
signal names/bit values. Vector occurrences are **zero-based global STIL
vectors**, lines are **one-based**. Runtime events are not vector indices:
mapping uses the compiler's rate groups, repeats, compressed loops and raw-event
ranges. Retained legacy results without entries say `UNAVAILABLE_LEGACY`;
the tool does not fabricate or fetch missing failures. Invalid maps are errors.

Descriptions omit input-file paths, USB identity, ports and raw error text.
Pattern, label and signal names may themselves be confidential. A description
is not an automatic privacy clearance. Raw maps in a bundle may retain absolute
build paths; compression and rebind do not sanitize them for sharing.

## Explicit no-recompile migration

```python
from stpg_runner import rebind_bundle

updated = rebind_bundle(old_bundle_dir, manifest_sha256=old_manifest_sha256)
```

CLI: `python -m stpg_runner rebind OLD_BUNDLE --manifest-sha256 SHA256`.
Default output is a new content-addressed sibling, normally under the project's
`build/`. `output_dir` / `--output-dir` selects a new exclusive folder instead.
The old bundle is never overwritten. An existing output or failed prefix is not
reused. A hash-pinned, completely verified bundle already bound to this SDK is
a no-op, not a second copy. Progress/cancellation use the normal SDK callbacks.

Migration requires the old bundle to pass **current complete semantic
verification**, not just hashing. The new bundle must also pass current semantic
verification and execution planning. Incompatible schemas, patterns, policies,
HOLD contracts, tampered files or unavailable source snapshots reject; a full
rebuild may still be necessary. Ordinary execution keeps rejecting an old SDK
fingerprint. There is no auto-migration or run fallback.

Source, binary, map, wait and package artifacts remain byte-identical. Only the
runner binding/cache/build-state and a hash-bound copy of the origin manifest
change. `binding.rebind` records the original compile SDK/Python, the previous
manifest/hash/cache/SDK/Python, and the warning. The new binding's Python is the
current verifier's version; it is **not** the original compiler environment.
Origin chains are validated and bounded to 32 migrations, then require rebuild.

**Compiled with an older SDK; later compiler fixes are not included.**
Current validation does not re-run STIL compilation, prove source/binary
equivalence, incorporate compiler bug fixes, or constitute physical validation.
The caller must display this warning and provenance, not label the artifact as
freshly compiled. Re-export accessory configs after any bundle/environment
change; never manually repin a manifest, command or interpreter hash.
