# Runner

The Runner builds, checks and runs patterns. Nothing happens by itself: opening never converts or runs anything, there
is no automatic run after a build or a reload, and nothing is retried.

| Item | What it is |
| --- | --- |
| **Project** (`project.stpg-project.json`, `inputs/`) | Portable settings and inputs; no device information |
| **Bundle** (`build/<key>/`) | A build result, fixed by hashes and never changed |
| **Bindings** | Which device is which, kept in the [settings folder](settings-folder.md) on this computer |
| **Results** (`results/<time>…/`) | One new folder per run |

The command line (`stpg-runner`) does the same work; both use the same SDK and give the same results.

## 1 · Project or bundle

- **New project…** from a STIL. **Open project…** and **Open bundle…** open existing ones.
- **Edit settings…** changes a project that has no successful build yet. **Copy to a new project…** tries other settings
  on one that has.
- **Build reservations** lists earlier builds with their settings. A failed build stays as a record, and the same inputs
  and settings are refused until something changes.

### New project

| Field | Meaning |
| --- | --- |
| Input form | STIL as it is, or an ATE-generator project and pattern file (two files or one) |
| Pin map | Optional. Empty: signals are mapped in declaration order |
| Project folder | A new folder, created when you press Create |
| Wait preset | `none`, `chunk-4ms`, `chunk-12ms` or `chunk-custom` (type its length). Chunk presets show example service values; replace them with measured ones if you have them. See [Waits](runner.md#waits) |
| Scope | Chunk presets only: `loop-generic` if a Loop block remains in the compiled pattern, otherwise `non-loop`. The build refuses a mismatch and names what it found |
| Timing | **Exact ticks** (default) or **Approx**, which rounds off-grid times and lists every one |
| Completion | `hold` keeps the last levels; `release` sends RESET(1) at the end |
| Storage, Compile mode, Loop policy | `psram`, `auto`, `preserve` preselected |
| Segmentation | **automatic** (one package when it fits, otherwise segments) or **one package only** |
| Max vectors / segment | Optional upper bound per segment, not a segment count. Leave blank normally |

A pattern is split only when it exceeds a limit (pattern bytes, events, samples, compares or Loop blocks); syntax, timing
and pin errors fail the build. The bundle table shows how much of the pattern region each segment uses.

`ERR_STREAMING_POC_TIMING` means the edges of a WaveformTable are too close for streaming playback (at least 10 ticks
between drive events, 11 with compares). Only a change to the STIL timing fixes it.

Segments are separate upload and RUN operations with separate compare results. Electrical continuity across a boundary
is not guaranteed.

### Waits

**Chunk waits** are placed by Build. It puts the chosen length at each eligible chunk boundary: not at the first
boundary of a segment, not before a chunk that starts with a forbidden WFT, and not where the chunk before it already
takes long enough by itself. So a chunk wait is not at every vector or every chunk.

- `chunk-custom` takes **Chunk wait length (ms)** with up to 3 decimals, which is 1 µs steps: `6.5` is 6,500 µs.
  Nothing is rounded; a finer value, 0 or anything that is not a plain number is refused. At 200 MHz the longest is
  21,474.836 ms. A custom length equal to 4 or 12 ms builds the same pattern as `chunk-4ms` or `chunk-12ms`.
- Edit settings and Copy keep a custom length exactly. Choosing another preset removes it from the project.
- The service budget, its scope and the total wait limit are separate settings that you declare; the wait length is not
  computed from them. Whether a plan fits is decided by Build.
- **Wait plan rejected**: the SDK refused the plan, for example over the total wait limit. A rejected plan is kept in the
  build folder as a record and never used (there is no zero-wait fallback). Nothing is changed for you: change the
  length or the limits yourself and Build again.
- A bundle from an older SDK whose saved plan the installed SDK rejects is shown as REJECT. **Update for this SDK**
  cannot repair it; Build the project again.

**A wait at a vector you choose** is an edit of the STIL, not a preset: open the STIL in the viewer, select the vector,
and use **Edit → Insert wait** ([Editing a copy](viewer.md#editing-a-copy)). Remove it with × on its item in the edit
list. That works on an edited copy; the source STIL is not changed. These are not possible:

- removing a wait that is already in the source STIL, or moving or removing the chunk waits that Build places;
- editing one repetition of a Loop, Macro or Procedure.

**Forbidden WFTs** only exclude boundaries; they do not move a wait to another place.

An explicitly long **WaveformTable Period** is source timing, not a chunk wait inserted by Build. It does
not count toward **Max total wait**. The resulting segments can use either narrow or wide Rate Vector
encoding; each is checked separately.

## 2 · Build · verify · plan

None of these contacts a device. **Build** shows each stage. **Verify** checks the bundle's hashes and counts; it is not
a run or device result. **Plan** shows the segments and what each will do.

## 3 · Run

1. **Devices…** registers an STPG. **Scan ports** lists ports and selects nothing: press **Use** on the right one, then
   enter the firmware version and capability of the firmware you installed. [Registering an STPG device](stpg-device.md)
   explains each value and where it comes from.
2. Choose the device and press **Run…**. The port is compared with the USB list without opening it, and the bundle is
   checked. The first check of a bundle takes a while; later ones are quick.
3. The confirmation shows exactly what will run. Tick every statement and press **Start run**.

What you confirmed is what runs: a binding or accessory config changed afterwards is refused at Start.

If a finished rejected/cancelled record ends after an accepted upload (for example at a GPIO controller step), the
result says **Upload accepted; no subsequent STPG command recorded**. The reply reported READY, or HOLD_AWAITING_NEXT
for a later segment. If the STPG is still in that state, another run is refused at its start check. This saved record
does not establish its current state or pin levels. Earlier segments may already have run; a PENDING checkpoint does
not prove that later commands were never sent. Check the cause and wiring before deciding on an explicit reset.

While it runs, progress shows host preparation, then each segment's upload and run. "Last report … s ago" turns yellow
after 5 s.

### Response deadlines

Ordinary replies (including the initial and RESET-check STATUS and upload payload ACKs) have a 2-second receive
budget. UPLOAD_END and RUN have 20 seconds. After RUN is accepted, each observation STATUS also has 20 seconds:
firmware startup and terminal-replay preparation can delay a reply even after RUN was acknowledged. This longer
budget does not add a retry, change a pattern's waits or bypass any response checks.

Observation keeps a 0.2-second polling interval, at most 300 polls per segment and a 60-second poll-admission
deadline. A read admitted before that deadline can finish afterwards; callbacks, OS and file I/O are not hard
physical-time bounds. Twenty seconds is a finite host policy, not a guaranteed worst-case firmware response time.
On a timeout or malformed response the SDK stops further requests and closes once, without reset or recovery.

**Long periods in a streamed Rate Vector pattern:** the host checks for actual emitted gaps of at least
250 ms, including across rate changes and repetitions within a segment. If present, the live firmware
must advertise `CAP_STREAM_WAIT_WATCHDOG`; otherwise the host refuses before RESET, upload or RUN.
A device-reported `ERR_TIMEOUT` during a programmed pause is not a host response timeout. Raising the
chunk-wait limit or the response deadline does not fix the older firmware watchdog. Materialized (non-streamed)
runs do not use this watchdog. Long gaps still count toward the unchanged per-segment observation window.

Updating the SDK changes its fingerprint: rebuild bundles through **Build**, or update them without recompiling (see
[A bundle built with another SDK](runner.md#a-bundle-built-with-another-sdk)); never edit their manifests. Then regenerate any
external-accessory configuration from the new bundle. Restart the GUI in the updated environment.

SDK progress callbacks receive `upload / done` only after a valid UPLOAD_END acceptance, and `run / accepted` only
after a valid RUN acknowledgement. The latter means "RUN accepted; waiting for STATUS", not that the waveform is
active or completed. These are host notifications, not extra queries. A callback error or cancellation stops later
requests and closes once; no cleanup command is sent even if RUN was already accepted.

### After compare failures: the retained fail log

When a segment ends (DONE or HOLD) with compare failures, the SDK reads that segment's retained fail log before the
next segment or the release: at most 341 entries (the STPG keeps no more), two per request, so at most 171 requests,
each reply with the ordinary 2-second budget. The confirmation shows this, and a plan with compares has its own box to
tick: **while it reads, the levels stay driven that much longer** before the next segment (HOLD) or the release.
Successful pages usually take far less than the 1026-second nominal total; that number is not a measured time.

The live view shows the read as "Reading its retained fail log: n / m entries": the transfer of the compare log, not
RUN or waveform progress. A segment without failures reads nothing. If the read stops (no reply, an answer the SDK does
not accept, cancel), the run stops there: no further segment, no release, no retry, no reset. The comparison result
stays `MISMATCH`, the entries read so far are kept, and **Reading the retained fail log stopped** names the request.

## Results

- **Outcome** (`COMPLETED`, `DEVICE_ERROR`, `REJECT`, `CANCELLED`) and **Comparison** (`MATCH`, `MISMATCH`,
  `NOT_APPLICABLE`, `UNOBSERVED`) are separate. Neither is a device pass. A run whose fail-log read stopped is `REJECT`
  with `MISMATCH`.
- A segment that ends in `DEVICE_ERROR` shows the STPG's error code with its firmware name, for example
  `12 (ERR_TIMEOUT)`. The STPG set that code in a STATUS answer that arrived: it is not this program's response
  deadline. The code alone does not say where in the firmware it was set. For `ERR_TIMEOUT` in a streamed pattern with
  long periods, see [Response deadlines](runner.md#response-deadlines).
- Collected: STATUS summaries and, after compare failures, the retained fail log. Samples are not collected.
- New results are saved as `run.json.gz` (gzip of the same JSON); older `run.json` results still open. A folder
  with both is refused.
- With `release`, sending, answer and completion of the release are shown separately; the electrical release is not
  measured.
- **Cancel** stops further requests and closes the port. No STOP or release is sent, so outputs may stay driven.
- One device job at a time. A confirmation works once. The same USB device cannot be used by two programs at once.

### STIL details

**STIL details** (under a result, and in the Results list) matches the result with the STIL of its bundle, offline:
for each segment, where it starts and ends in the STIL (line, vector, pattern, label), its comparison and fail
count; for each fail-log entry, the vector, line and the failed signals with the expected and the observed level. The
bundle is the open one or a build of the project next to the results, chosen only by the exact manifest the result
recorded; if it is not there, open it first. Vectors are zero-based STIL vector occurrences (Loop iterations
counted), lines one-based. Results recorded before this version have no fail-log entries ("not in this result").

The first matching may take time on a large bundle. Showing the same details again
or saving their text reuses completed matching within this GUI process, after
rehashing all bundle files. A changed result or bundle is never accepted from
that cache. Reopening the GUI or cache eviction requires matching again.

**Save as text…** writes the same details to a new `.txt` file (never overwritten, never inside a bundle); **Copy
text** copies it. The text names no port, USB serial or folder of this computer, but pattern, signal and label names
may be confidential: check it before you share it.

**Show fails in the viewer** opens the bundle's copy of the STIL in the Viewer and marks the fails there (see
[Viewer: Fails of a run](viewer.md#fails-of-a-run)).

### A bundle built with another SDK

Run refuses a bundle bound to another SDK version; the Project card says **Built with another SDK version**. Either
**Build** the project again (recompiles with the installed SDK), or press **Update for this SDK without recompiling…**:
the installed SDK checks the bundle in full, copies its files unchanged into a new bundle beside it and checks that
one in full too. The old bundle stays as it is. Nothing is compiled and no device is contacted. The new bundle says
**Updated for this SDK without recompiling** — compiled with an older SDK, later compiler fixes are not included — and
which SDK compiled it is in the bundle and in the project log. If a check fails, no usable bundle is made (a started
folder stays as a failed attempt) and the project needs Build.

Each build adds lines to `stpg-view.log.jsonl` in the project folder (an update without recompiling too). It holds local folder names, and projects hold the
complete STIL: compression is not encryption. Review a project before sharing it.

## Security

The program listens on `127.0.0.1` with a random port and a token, refuses other Host headers, and accepts changes only
from its own page. `--host` with another address exposes file reading to the network and turns the Runner off; do not
use it normally.

## Start options

| Option | Effect |
| --- | --- |
| `--no-device-run` | No device is opened and no accessory is started: view, build, verify and plan only |
| `--demo-device` | Adds a simulated STPG and GPIO controller named `simulated`, and an analyzer identity for Reset LA |
| `--config-dir FOLDER` | The [settings folder](settings-folder.md) |
| `--results-dir FOLDER` | Results of bundles that are not inside a project (default `~/stpg-work/runs`) |

## Current limits

- Execution is a preview: summary results only, no STOP on cancel, no reconnection or recovery.
- Continuity across segment boundaries is not proven.
- GPIO acknowledgements are software states; voltages and USB reconnection are not measured.
- Demo results are simulated and are not timing evidence.
