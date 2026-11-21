/* Compiles the actual two runner functions extracted by the pytest harness.
 * This is an ordering/latch model, NOT an electrical or cycle-accurate model. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

typedef unsigned int uint;
typedef uint PIO;
typedef struct { uint base, span; } pio_sm_config;
enum { STPG_ACTIVE_PINS_V01 = 16, STPG_ACTIVE_PINS_V11 = 24, ERR_PIO_ERROR = 12 };
static struct {
    bool reduced_density_streaming;
    uint output27_reduced_pio_offset, sample_delayed27_pio_offset;
    uint output_pio27_offset, sample_pio27_offset;
    uint output_reduced_pio_offset, sample_delayed_pio_offset;
    uint output_pio_offset, sample_pio_offset;
} g_runner;
static uint pins, can_add_calls, fail_at, init_count, clear_calls, mux_calls, error;
static bool enabled[2], muxed[30], preserve;
static uint32_t pio_oe, pio_value, initial_oe, initial_value;
static const uint gpios[] = {2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,20,21,22,23,26,27,28};
static PIO stpg_runner_pio(void) { return 0; }
static uint stpg_runner_sm(void) { return 0; }
static uint stpg_sample_sm(void) { return 1; }
static uint stpg_buffer_active_pins(void) { return pins; }
static uint stpg_board_out_pin_base(uint active) { (void)active; return 2; }
static uint stpg_board_out_pin_span(uint active) { return active == 24 ? 27 : 16; }
static uint stpg_board_gpio_for_dut_pin(uint bit) { assert(bit < pins); return gpios[bit]; }
static void gpio_disable_pulls(uint gpio) { assert(gpio < 30 && gpio != 19 && gpio != 24 && gpio != 25); }
static void gpio_set_input_enabled(uint gpio, bool on) { assert(gpio < 30 && on); }
static void pio_gpio_init(PIO pio, uint gpio) {
    (void)pio;
    assert(!enabled[0] && !enabled[1]);
    assert(init_count == 2); /* No pad handoff until both SMs are configured. */
    assert(gpio != 19 && gpio != 24 && gpio != 25);
    if (preserve) {
        assert(muxed[gpio] && pio_oe == initial_oe && pio_value == initial_value);
    } else {
        /* SIO's release does not erase PIO latches. Re-exposing OE here is
         * wrong even if that particular stale value happens to be LOW. */
        assert((pio_oe & (1u << gpio)) == 0);
    }
    muxed[gpio] = true;
    mux_calls++;
}
static void pio_sm_set_enabled(PIO pio, uint sm, bool on) { (void)pio; assert(sm < 2 && !on); enabled[sm] = on; }
static void pio_sm_clear_fifos(PIO pio, uint sm) { (void)pio; (void)sm; }
static void pio_sm_restart(PIO pio, uint sm) { (void)pio; (void)sm; /* Latches survive restart. */ }
static void pio_clear_instruction_memory(PIO pio) { (void)pio; }
static bool pio_can_add_program(PIO pio, const uint *program) {
    (void)pio; (void)program; return ++can_add_calls != fail_at;
}
static uint pio_add_program(PIO pio, const uint *program) { (void)pio; (void)program; return 0; }
static void stpg_status_set_error(uint code) { error = code; }
static void sm_config_set_clkdiv_int_frac(pio_sm_config *cfg, uint a, uint b) { (void)cfg; assert(a == 1 && b == 0); }
static void sm_config_set_out_pins(pio_sm_config *cfg, uint base, uint span) { cfg->base = base; cfg->span = span; }
static void sm_config_set_out_shift(pio_sm_config *cfg, bool a, bool b, uint n) { (void)cfg; assert(a && !b && n == 32); }
static void sm_config_set_in_pins(pio_sm_config *cfg, uint base) { cfg->base = base; }
static void sm_config_set_in_shift(pio_sm_config *cfg, bool a, bool b, uint n) { (void)cfg; assert(!a && b && n == (pins == 24 ? 27u : 16u)); }
static void pio_sm_init(PIO pio, uint sm, uint offset, const pio_sm_config *cfg) {
    (void)pio; (void)offset; assert(sm < 2); enabled[sm] = false; init_count++;
    if (sm == 0) assert(cfg->base == 2 && cfg->span == stpg_board_out_pin_span(pins));
    /* SDK init/restart/config do not clear OE or the output latch. */
}
static void pio_sm_set_consecutive_pindirs(PIO pio, uint sm, uint base, uint span, bool on) {
    (void)pio; assert(sm == 0 && !on && !preserve && !enabled[0] && !enabled[1]);
    assert(init_count == 1 && mux_calls == 0);
    pio_oe &= ~(((1u << span) - 1) << base);
    clear_calls++;
}
#define STPG_DUT_PIN_BASE 2u
#define PROGRAM(name) static const uint name##_program = 0; \
    static pio_sm_config name##_program_get_default_config(uint offset) { (void)offset; return (pio_sm_config){0}; }
PROGRAM(stpg_runner27_reduced)
PROGRAM(stpg_sample_delayed_in27)
PROGRAM(stpg_runner27)
PROGRAM(stpg_sample_in27)
PROGRAM(stpg_runner_reduced)
PROGRAM(stpg_sample_delayed_in)
PROGRAM(stpg_runner)
PROGRAM(stpg_sample_in)
#define STPG_SAMPLE_DELAYED_IN27_PROGRAM stpg_sample_delayed_in27_program
#define STPG_SAMPLE_DELAYED_IN_PROGRAM stpg_sample_delayed_in_program
#define STPG_SAMPLE_DELAYED_IN27_PROGRAM_GET_DEFAULT_CONFIG stpg_sample_delayed_in27_program_get_default_config
#define STPG_SAMPLE_DELAYED_IN_PROGRAM_GET_DEFAULT_CONFIG stpg_sample_delayed_in_program_get_default_config

#include "subject.inc"

int main(void) {
    for (uint width = 0; width < 2; width++)
    for (uint reduced = 0; reduced < 2; reduced++)
    for (uint held = 0; held < 2; held++)
    for (uint failure = 0; failure < 3; failure++)
    for (uint level = 0; level < 2; level++) {
        pins = width ? 24 : 16;
        preserve = held;
        fail_at = failure;
        g_runner.reduced_density_streaming = reduced;
        can_add_calls = init_count = clear_calls = mux_calls = error = 0;
        memset(muxed, 0, sizeof(muxed));
        /* Previous run left output latches/directions set. Release RESET
         * selects SIO input, but HOLD continuation leaves PIO selected. */
        pio_oe = initial_oe = 0;
        for (uint bit = 0; bit < pins; bit++) {
            pio_oe |= 1u << gpios[bit];
            muxed[gpios[bit]] = preserve;
        }
        initial_oe = pio_oe;
        pio_value = initial_value = level ? pio_oe : 0;
        enabled[0] = enabled[1] = true; /* Preparation must disable both. */
        stpg_runner_prepare_output_sm(preserve);
        assert(!enabled[0] && !enabled[1] && pio_value == initial_value);
        if (failure) {
            assert(error == ERR_PIO_ERROR && mux_calls == 0 && init_count == 0);
            assert(pio_oe == initial_oe); /* No partially committed pad handoff. */
        } else {
            assert(error == 0 && init_count == 2 && mux_calls == pins);
            assert(clear_calls == (preserve ? 0u : 1u));
            assert(pio_oe == (preserve ? initial_oe : 0));
        }
        assert(!muxed[19] && !muxed[24] && !muxed[25]);
    }
    return 0;
}
