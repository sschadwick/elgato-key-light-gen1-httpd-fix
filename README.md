# Elgato Key Light (Gen 1) - HTTP server hang: root cause + firmware fix

Reverse-engineering of the author's own Elgato Key Light (Gen 1) to find and fix the
local HTTP control server (port 9123) becoming unresponsive under normal use, requiring a power
cycle to recover. A client-side workaround (e.g. changing how a controller polls the device) was
explicitly out of scope; this documents a fix to the device's own firmware.

Device: Key Light Gen 1, hardwareBoardType 53. Platform: Realtek RTL8195A (Ameba).
`192.168.1.50` below is a placeholder; set `KEYLIGHT_HOST` to your own device's address before
running anything in `tools/`.

## Root cause

The device's HTTP accept loop (`SimpleHTTPD_Socket_Accept`, found by disassembling the firmware)
is a **strict single-threaded loop**: accept, set four socket options, handle the request
**synchronously and blocking**, close, repeat. It never returns to `accept()` until the current
client is fully handled.

The four `setsockopt` calls after `accept()` are `SO_KEEPALIVE=1`, `TCP_KEEPIDLE`, `TCP_KEEPCNT`,
`TCP_KEEPINTVL`, in that order. **`SO_RCVTIMEO` (receive timeout) is never set.** TCP keepalive
only fires on *total* silence; any traffic at all (even a stray byte every few seconds) resets its
idle timer. So a client that's slow-but-not-silent (flaky WiFi retries, a partial/chunked request,
anything that trickles bytes without finishing) can occupy the single processing slot indefinitely,
blocking every other client. Additional connections queue silently in the TCP backlog; once it's
full, further clients get an immediate kernel-level RST.

**Verified live** against a real device: a single connection trickling data (no burst, no volume,
just "not finishing") reliably blocked concurrent requests for as long as the trickle continued.
Reproduction harness: `tools/repro_httpd_hang.py` (slowloris / close-storm / window-starve attack
phases, run against a continuous canary request to measure impact).

## The fix

Add a receive timeout, repurposing the 4th existing `setsockopt` call (`TCP_KEEPCNT`) in place:

```c
setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &ten_seconds, sizeof(int));
```

This platform's lwIP accepts `optlen==4` (an int, milliseconds) for `SO_RCVTIMEO`. The replacement
is small enough (26 bytes of Thumb-2 + 2 NOPs = 30 bytes) to fit exactly in the space of the call it
replaces: an in-place patch, no code cave or relocation needed. `tools/patch_rcvtimeo.py` applies
it; `tools/patch.s` is the assembly source for the replacement bytes.

**Validated**: stock firmware wedges under a single stalled client; the patched firmware stays
responsive under ten concurrent stalling connections in the same harness (brief, self-recovering
hiccups only).

## What this repo does not cover

Installing a patched image on a real device requires going through the vendor's own firmware
update mechanism, which is out of scope here. This repo documents the defect and the code-level
fix and lets you verify both against your own device; it is not a flashing tool.

## Usage

1. Extract your own copy of Elgato's firmware from their public Control Center installer:
   `python3 tools/extract_firmware.py ControlCenter_<version>_x64.msi outdir/`
2. Apply the patch: `python3 tools/patch_rcvtimeo.py outdir/Firmware_Key_Light.bin patched.bin`
3. Reproduce the bug / verify the fix against a live device:
   `python3 tools/repro_httpd_hang.py --host 192.168.1.50`

Nothing here is bundled or auto-downloaded; you extract your own firmware from Elgato's own public
installer.

## Repo layout

```
tools/
  extract_firmware.py   pull your own firmware from Elgato's Control Center installer
  patch_rcvtimeo.py      apply the SO_RCVTIMEO fix (30-byte in-place patch)
  patch.s                assembly source for the patch bytes
  repro_httpd_hang.py    reproduce the hang against a live device (slowloris / close-storm /
                         window-starve), to verify stock-vs-patched behavior yourself
```

## Authorization & safety

No firmware is bundled here or sourced from unverified third parties; you extract your own copy
from Elgato's own public installer. Nothing in this repo probes Elgato/Corsair's cloud infrastructure;
the reproduction harness talks only to a device IP/hostname you supply.

## License

MIT, see [`LICENSE`](LICENSE). This project is independent research and is not affiliated with or
endorsed by Elgato or Corsair.
