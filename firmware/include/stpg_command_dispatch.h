#ifndef STPG_COMMAND_DISPATCH_H
#define STPG_COMMAND_DISPATCH_H

#include "stpg_protocol.h"

void stpg_dispatch_command(const StpgCommand *cmd, StpgStatusResponse *rsp);

#endif
