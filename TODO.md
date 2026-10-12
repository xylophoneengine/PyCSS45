# TODO

## Long commands over USB HID (manual integration time 1.28 s / 2.56 s)

Status: open. Owner hunts this down.

**Problem.** Over USB HID the driver sends commands in 8-char output reports
(`_HID_CHUNK = 8`), and the CSS-45 runs each report as its own command.
Rig 2026-10-12 (serial 000000000051726, fw 1.58): `hi2560000` set 0.256 s
(`Ihi 256000_0`), then the leftover `0` was acked with `*`. Since the fix
merged in 42f6f77 the driver refuses any HID command longer than 8 chars
(Css45Error -25100), so the max fixed integration time over HID is 0.64 s
(`hi640000`, `IntegrationTime.MS640`).

**Why it is likely the driver, not the device.** In auto mode the device
reaches 2.56 s by itself (`Ihi 2560000_0`), and the vendor library handled
commands up to 43 chars. The framing it uses for multi-report commands
(continuation marker, length byte, bigger report size, ...) is unknown.

**How to find it.**
1. Windows PC with the Gigahertz-Optik vendor software and the CSS-45 on USB.
2. Capture USB traffic with Wireshark + USBPcap while the vendor software sets
   a manual integration time of 2.56 s (and, for comparison, 0.64 s).
3. Compare the output reports of the 9-char command with the 8-char one.
4. Implement that framing in `_HidTransport.write`, drop the -25100 refusal,
   and re-test on the rig: `hi2560000` must give `Ihi 2560000_0`.
