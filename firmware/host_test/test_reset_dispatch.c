/* Exercise the real RESET handler; the runner/GPIO boundary is a stub here.
 * Unused command handlers are discarded by section GC, not hardware mocks. */
#include <assert.h>
#include <string.h>
#include "../src/command_dispatch.c"

static stpg_state_t test_state;
static stpg_err_t test_error;
static unsigned stop_count, reset_count;
static bool active, draining, holding, pending;
static stpg_err_t stop_error;
bool stpg_runner_is_active(void) { return active; }
bool stpg_runner_is_draining(void) { return draining; }
bool stpg_runner_hold_awaiting_next(void) { return holding; }
bool stpg_runner_start_pending(void) { return pending; }
stpg_err_t stpg_runner_stop(void) { ++stop_count; return stop_error; }
void stpg_buffer_reset(void) { ++reset_count; }
void stpg_samples_reset(void) {}
void stpg_compare_reset(void) {}
void stpg_status_set_state(stpg_state_t s) { test_state = s; }
void stpg_status_set_error(stpg_err_t e) { test_error = e; }
void stpg_status_clear_error(void) { test_error = ERR_NONE; }
void stpg_status_clear_boot_error(void) {}
void stpg_status_reset_pattern_info(void) {}
void stpg_status_reset_result_info(void) {}
void stpg_status_build(uint16_t r, uint32_t seq, StpgStatusResponse *out) {
    memset(out, 0, sizeof(*out)); out->rsp = r; out->seq = seq;
    out->state = test_state; out->error_code = test_error;
}
int main(void) {
    StpgCommand cmd = {.cmd=CMD_RESET, .seq=7};
    StpgStatusResponse rsp;
    const stpg_state_t states[] = {STATE_IDLE, STATE_DONE, STATE_ERROR, STATE_HOLD_AWAITING_NEXT};
    for (unsigned i=0; i<sizeof(states)/sizeof(states[0]); ++i) {
        test_state=states[i]; stop_count=reset_count=0;
        holding=states[i]==STATE_HOLD_AWAITING_NEXT;
        cmd.arg0=STPG_RESET_FLAG_RELEASE_OUTPUTS;
        dispatch_reset(&cmd, &rsp);
        assert(stop_count==1 && reset_count==1 && rsp.rsp==RSP_OK && rsp.state==STATE_IDLE);
    }
    holding=false; test_state=STATE_DONE; stop_count=0; cmd.arg0=0;
    dispatch_reset(&cmd, &rsp);
    assert(stop_count==0 && rsp.rsp==RSP_OK); /* Plain DONE reset must hold. */
    for (unsigned i=0; i<4; ++i) {
        active=i==0; draining=i==1; holding=i==2; pending=i==3;
        stop_count=reset_count=0;
        dispatch_reset(&cmd, &rsp);
        assert(stop_count==1 && reset_count==1 && rsp.rsp==RSP_OK);
    }
    active=draining=holding=pending=false;
    for (unsigned i=0; i<4; ++i) {
        cmd.arg0=0; cmd.arg1=0; cmd.arg2=0; cmd.arg3=0;
        if(i==0) cmd.arg0=2;
        if(i==1) cmd.arg1=1;
        if(i==2) cmd.arg2=1;
        if(i==3) cmd.arg3=1;
        stop_count=reset_count=0;
        dispatch_reset(&cmd, &rsp);
        assert(rsp.rsp==RSP_ERROR && rsp.error_code==ERR_UNSUPPORTED && stop_count==0 && reset_count==0);
    }
    memset(&cmd, 0, sizeof(cmd)); cmd.arg0=1; stop_error=ERR_BAD_STATE;
    reset_count=0; dispatch_reset(&cmd, &rsp);
    assert(rsp.rsp==RSP_ERROR && reset_count==0);
    return 0;
}
