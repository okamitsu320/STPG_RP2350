# Custom chunk wait length (SDK / CLI)

`chunk-custom` takes an explicit length. In the GUI, choose it as the wait
preset and type **Chunk wait length (ms)**; New project, Edit settings and Copy
keep the exact length, and another preset removes it (see
[Runner: Waits](../runner.md#waits)). A GUI older than this version does not
offer `chunk-custom`: do not edit or copy a custom project with it.

```python
from pathlib import Path
from stpg_runner import init_project, build_project

project = init_project(
    Path("samples/gui_counter_long.stil"), Path("custom-wait-project"),
    wait_preset="chunk-custom", chunk_wait_us=6500, completion="hold",
    service_us=4000, service_source="declared example, not measured",
    service_scope="non-loop", max_total_wait_us=10_000_000,
)
result = build_project(project)  # offline only; no device is opened
```

The new project folder must not exist. Use your own validated service budget,
scope and total limit; the example is not an operating-point recommendation.
A source retaining Loop provenance requires `service_scope="loop-generic"`.

CLI, after preparing that same public sample and a new output folder:

```sh
python -m stpg_runner init samples/gui_counter_long.stil --directory custom-wait-project --wait-preset chunk-custom --chunk-wait-us 6500 --completion hold --service-us 4000 --service-source "declared example, not measured" --service-scope non-loop --max-total-wait-us 10000000
```

`chunk_wait_us` is a **positive integer number of microseconds**: 6500 means
6.5 ms. A GUI may expose milliseconds with a 0.001 ms step. The SDK rejects
fractions, booleans, missing/nonfinite/nonpositive values, nonintegral target
ticks and values exceeding uint32 ticks. It never truncates, clamps or rounds
this input. At 200 MHz the largest input is 21,474,836 us; the final rate period,
rate count, storage and other encoded limits must still fit after insertion.
`chunk_wait_us` is allowed only with `chunk-custom`, not silently ignored by
another preset. Old project normalization does not add this field.

The value goes to the existing every-boundary planner: each eligible boundary
gets the specified wait; the first boundary of a segment, forbidden WFTs and
boundaries already preceded by a sufficiently long natural chunk window are
skipped. It does **not** force a wait at every vector or override those rules.
The declared service budget is separate from the chosen wait length and is not
a measurement. Global wait limits, model acceptance, exact encoded capacity,
Loop/scope checks, CRCs and normal bundle verification remain required.
A rejected partial proposal is retained as evidence, never used as an
executable zero-wait fallback; current verification/rebind rejects it too.

This does not implement arbitrary relocation/removal of compiled automatic
waits. Existing schedule tools address chunk boundaries, not arbitrary vector
positions. For source edits already supported by the GUI, see
[Viewer: Editing a copy](../viewer.md#editing-a-copy). Remove an inserted source
wait via its edit-list item; original STIL waits and compiled waits are not
generically removable by that control. Individual Loop/Macro/Procedure
occurrences cannot be edited. Keep the original bundle immutable.

The SDK fingerprint changes in this review. After GUI integration is accepted,
install the matching core/LA version together, explicitly rebuild/rebind valid
old bundles and re-export external configs. Rebind does not repair a rejected
old wait plan. Firmware and controller sketches are unchanged; no physical
timing or hardware validation is implied.
