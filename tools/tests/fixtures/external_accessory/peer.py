"""Independent public stdio peer. Standard library only; never touches devices."""
import hashlib
import json
from pathlib import Path
import sys
import time

scenario, _, pause = sys.argv[1].partition(":")  # "slow-arm:1.5": a slow reply after 1.5 s (default 0.4)
pause = float(pause) if pause else 0.4
opened = None
nonce = None
root = None
for sequence, raw in enumerate(sys.stdin.buffer, 1):
    assert raw.endswith(b"\n"), "partial request at EOF is not a command"
    request = json.loads(raw)
    assert request["protocol"] == "stpg_external_accessory_v1"
    assert request["seq"] == sequence
    assert request["method"] == ("open", "before_run", "after_terminal")[sequence - 1]
    if sequence == 1:
        opened = request
        nonce = request["nonce"]
        root = Path(request["output_dir"])
        for key in ("binding", "plan"):
            assert hashlib.sha256(Path(request[key + "_file"]).read_bytes()).hexdigest() == request[key + "_sha256"]
    assert request["nonce"] == nonce
    with (root / "peer-requests.jsonl").open("ab") as journal:
        journal.write(raw)
        journal.flush()
    if ((scenario == "slow-open" and sequence == 1)
            or (scenario in {"slow-arm", "arm-cancel-reply"} and sequence == 2)
            or (scenario == "slow-terminal" and sequence == 3)):
        time.sleep(pause)
    reply = {"protocol": "stpg_external_accessory_v1", "nonce": nonce, "seq": sequence,
             "method": request["method"], "status": ("READY", "ARMED", "COMPLETE")[sequence - 1],
             "result_file": None, "result_sha256": None}
    bad_phase = (1 if scenario.startswith("open-") else 2 if scenario.startswith("arm-")
                 else 3 if scenario.startswith("result-") else 0)
    action = scenario.split("-", 1)[-1] if sequence == bad_phase else "normal"
    if sequence == 3:
        result = {"kind": "stpg_external_accessory_result", "schema_version": 1,
                  "mode": opened["mode"], "outcome": "COMPLETED", "adopted": scenario != "not-adopted", "errors": []}
        if action == "wrongmode":
            result["mode"] = "device" if opened["mode"] == "simulated" else "simulated"
        payload = (json.dumps(result) + "\n").encode()
        (root / "result.json").write_bytes(payload)
        reply["result_file"] = "result.json"
        reply["result_sha256"] = hashlib.sha256(payload).hexdigest()
        if action == "escape":
            reply["result_file"] = "../outside.json"
        elif action == "collision":
            reply["result_file"] = "inputs/binding.json"
        elif action == "hash":
            reply["result_sha256"] = "0" * 64
        elif action == "missing":
            reply["result_file"] = "missing.json"
    if action == "status":
        reply["status"] = "ERROR"
    elif action == "nonce":
        reply["nonce"] = "0" * 32
    elif action == "seq":
        reply["seq"] += 1
    elif action == "version":
        reply["protocol"] = "unsupported-v2"
    elif action == "method":
        reply["method"] = "wrong"
    elif action == "unknown":
        reply["extra"] = True
    elif action == "eof":
        sys.exit(0)
    elif action in ("partial", "timeout"):
        if action == "partial":
            sys.stdout.buffer.write(b'{"protocol":')
            sys.stdout.buffer.flush()
        sys.stdin.buffer.read()  # EOF ends the failed peer; no cleanup operation.
        sys.exit(0)
    elif action == "stderr":
        sys.stderr.buffer.write(b"synthetic stderr\n" * 5000)
        sys.stderr.buffer.flush()
    encoded = json.dumps(reply).encode()
    if action == "duplicate":
        encoded = encoded[:-1] + b', "seq": ' + str(sequence).encode() + b'}'
    elif action == "nonfinite":
        encoded = encoded[:-1] + b', "seq": NaN}'
    elif action == "oversize":
        encoded = b"x" * 40000
    elif action == "invalidutf8":
        encoded = b"\xff"
    sys.stdout.buffer.write(encoded + b"\n" + (b"EXTRA" if action == "excess" else b""))
    sys.stdout.buffer.flush()
    if sequence == 3 and scenario.startswith("post-complete-"):
        if scenario == "post-complete-output":
            time.sleep(0.05)
            sys.stdout.buffer.write(b"UNSOLICITED-AFTER-COMPLETE\n")
            sys.stdout.buffer.flush()
        else:
            sys.exit(7 if scenario == "post-complete-fail" else 0)
    if action == "exit":
        sys.exit(7)
if scenario == "late-output":
    sys.stdout.buffer.write(b"UNSOLICITED-LATE\n")
    sys.stdout.buffer.flush()
if scenario == "nonzero-exit":
    sys.exit(7)
if scenario == "ignore-eof":
    time.sleep(10)
if scenario == "cleanup-slow":
    time.sleep(1.25)
