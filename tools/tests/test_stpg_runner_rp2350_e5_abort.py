from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "firmware/src/stpg_runner.c"


def _function(source: str, signature: str, next_signature: str) -> str:
    return source.split(signature, 1)[1].split(next_signature, 1)[0]


def test_rp2350_e5_disables_all_runner_dma_channels_before_first_abort() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    abort = _function(
        source,
        "static void stpg_runner_abort_dma(void)",
        "static void stpg_runner_finish_hw_hold_outputs",
    )

    assert "RP2350-E5" in abort
    assert "dma_set_irq0_channel_mask_enabled(claimed_mask, false)" in abort
    for channel in (
        "g_runner.output_dma_channel",
        "g_runner.output_dma_spare_channel",
        "g_runner.sample_dma_channel",
        "g_runner.sample_sched_dma_channel",
    ):
        marker = f"stpg_runner_disable_dma_channel_before_abort({channel})"
        assert marker in abort
        assert abort.index(marker) < abort.index("dma_channel_abort(")


def test_rp2350_e5_helper_clears_channel_enable_bit() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    helper = _function(
        source,
        "static void stpg_runner_disable_dma_channel_before_abort",
        "static void stpg_runner_abort_dma",
    )
    assert "hw_clear_bits(" in helper
    assert "DMA_CH0_CTRL_TRIG_EN_BITS" in helper


def test_dma_irq_mask_is_restored_only_after_all_aborts() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    abort = _function(
        source,
        "static void stpg_runner_abort_dma(void)",
        "static void stpg_runner_finish_hw_hold_outputs",
    )
    restore = "dma_set_irq0_channel_mask_enabled(irq0_enabled_mask, true)"
    assert restore in abort
    assert abort.index(restore) > abort.rindex("dma_channel_abort(")


def test_pio_dreq_sources_stop_before_dma_abort() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    for signature, next_signature in (
        (
            "static void stpg_runner_finish_hw_hold_outputs(void)",
            "static stpg_err_t stpg_runner_start_prepared",
        ),
        (
            "static void stpg_runner_disable_hw_safe(void)",
            "static stpg_err_t stpg_runner_abort_stream_startup",
        ),
    ):
        body = _function(source, signature, next_signature)
        assert body.index("pio_sm_set_enabled(pio, out_sm, false)") < body.index(
            "stpg_runner_abort_dma()"
        )
        assert body.index("pio_sm_set_enabled(pio, sample_sm, false)") < body.index(
            "stpg_runner_abort_dma()"
        )
