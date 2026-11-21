#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include "hardware/flash.h"
#include "hardware/gpio.h"
#include "stpg_app.h"
#include "stpg_board.h"
#include "stpg_common.h"
static bool initialized[30], output[30], output_level[30], pullup[30], pulldown[30], pulls_disabled[30], input_enabled[30];
static uint32_t function[30], cs1_gpio = UINT32_MAX, cs1_size;
void gpio_init(uint32_t g) { assert(g<30); initialized[g]=true; input_enabled[g]=true; }
void gpio_set_dir(uint32_t g, bool d) { output[g]=d; }
void gpio_put(uint32_t g, bool value) { output_level[g]=value; }
void gpio_disable_pulls(uint32_t g) { pulls_disabled[g]=true; pullup[g]=false; pulldown[g]=false; }
void gpio_pull_down(uint32_t g) { pulldown[g]=true; }
void gpio_set_input_enabled(uint32_t g, bool e) { input_enabled[g]=e; }
void gpio_set_function(uint32_t g, uint32_t f) { assert(g<30); function[g]=f; }
void flash_devinfo_set_cs_size(uint32_t c, uint32_t s) { assert(c==1u); cs1_size=s; }
void flash_devinfo_set_cs_gpio(uint32_t c, uint32_t g) { assert(c==1u); cs1_gpio=g; }
int main(void) {
    stpg_board_init_platform_gpio();
    assert(initialized[25] && output[25] && !output_level[25] && pulls_disabled[25]);
    stpg_board_init_psram_cs1();
    assert(pulls_disabled[19] && !pulldown[19]);
    assert(function[19] == GPIO_FUNC_XIP_CS1 && cs1_gpio == 19u && cs1_size == FLASH_DEVINFO_SIZE_8M);
    assert(!initialized[8] && function[8] == 0u); /* CS1 setup never probes a DUT GPIO. */
    size_t candidate_count = 0u;
    const uint32_t *candidates = stpg_board_psram_cs1_candidates(&candidate_count);
    assert(candidates != NULL && candidate_count == 1u && candidates[0] == 19u);
    assert(!stpg_board_gpio_is_dut_pin(candidates[0], STPG_ACTIVE_PINS_V11));
    /* Both CS1 mux entry paths must repair stale pull state rather than rely
     * on the one-time startup initialization remaining untouched. */
    pullup[19]=true; pulldown[19]=true; pulls_disabled[19]=false;
    stpg_board_psram_configure_cs1(19u);
    assert(pulls_disabled[19] && !pullup[19] && !pulldown[19]);
    pullup[19]=true; pulldown[19]=true; pulls_disabled[19]=false;
    stpg_board_psram_restore_cs1_mux(19u);
    assert(pulls_disabled[19] && !pullup[19] && !pulldown[19]);
    assert(function[19] == GPIO_FUNC_XIP_CS1);
    stpg_board_init_safe_gpio();
    for(uint32_t i=0; i<24; ++i) {
        const uint32_t g=stpg_board_gpio_for_dut_pin(i);
        assert(initialized[g] && !output[g] && pulldown[g] && !input_enabled[g]);
    }
    for(uint32_t g=0; g<2; ++g) assert(!initialized[g]); /* Do not steal SWD/probe pins. */
    /* Board/peripheral pins are not part of RESET's DUT release policy. */
    assert(!initialized[19] && function[19] == GPIO_FUNC_XIP_CS1 && pulls_disabled[19]); /* RESET/STOP does not disturb CS1. */
    assert(!initialized[24] && !pulldown[24]); /* VBUS sense remains untouched. */
    assert(initialized[25] && output[25] && !output_level[25] && pulls_disabled[25] && !pulldown[25]); /* LED survives safe GPIO init. */
    assert(!initialized[29] && !pulldown[29]); /* VSYS sense remains untouched. */
    return 0;
}
