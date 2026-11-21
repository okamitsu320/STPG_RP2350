# GPIO controller (optional)

An RP2040 board with the controller sketch can drive the STPG and analyzer RUN (reset) lines and general pins. You do
not need one. A controller you have not selected is never touched, and one that is selected but missing or invalid is
refused rather than skipped.

| Pin | Role | During a pattern run |
| --- | --- | --- |
| GPIO0 | STPG RUN | never (manual Reset STPG only) |
| GPIO1 | Analyzer RUN | never (manual Reset LA only) |
| GPIO2 | bench-specific (for example an enable) | listed actions only |
| GPIO3–8, 14, 15, 26 | general | listed actions only |

## Set up

The sketch is [controller/firmware/rp2040_zero_gpio_toggle.ino](../controller/firmware/rp2040_zero_gpio_toggle.ino).
Compile/upload it separately for your controller board before binding it here.

**Controllers… → Add…**: port and USB identity (from **Scan ports**), the protocol, and the pins allowed to go HIGH.
Other pins can only be LOW or Hi-Z. The protocol is `rp2040-gpio-v1`, or `legacy-3` for the old three-channel sketch
with its pin numbers.

**Identify** opens the port and sends only the identify request. It does open and close the port, so it is not free of
electrical effects. Its record is saved before the port is touched.

## Actions during a run

In **3 · Run**, **GPIO actions during this run** lists segment, timing (before RUN, or after a valid DONE/HOLD), pin and
state. Plans can be saved and loaded and are bound to the bundle. With an external accessory at the same segment, the
order is GPIO action, then the accessory's ARMED, then RUN.

After a failed or cancelled action nothing further is sent and nothing is restored. "acknowledged" is the controller's
software state, not a measured voltage.

If the STPG refuses the run before its first upload (for example at its start check), the result shows the controller
as **not started**: no GPIO request was sent and its port was not opened. That says nothing about the controller.

When the controller's identification is refused, the result lists the lines the controller sent, from `control.json`,
with how the SDK read each one (a known start-up line, a match, or refused). It shows what was received, not why. A
record from an earlier version has no such lines. The lines can contain the controller's identity, so they are shown
only on this computer and are not added to STIL details text.

If a controller step fails after an accepted upload, the last STPG reply reports READY (or HOLD_AWAITING_NEXT for a
later segment), with no subsequent STPG command recorded for that upload. While the device remains in that state,
another run is refused at its start check. A saved record is not a live check of device state or pin levels; it does
not establish that outputs are safe or released. Check the wiring and cause before deciding on an explicit reset.

## Manual operations

- **Reset STPG…** and **Reset LA…**: lease the target without opening its port, drive LOW for the given time, go Hi-Z,
  close. A failure can leave RUN low. Nothing is run or identified afterwards.
- **Set GPIO…**: one pin LOW, Hi-Z or HIGH. No state is preselected. GPIO0 and GPIO1 need the target's binding; HIGH
  needs the binding's permission and a one-time acknowledgement.
