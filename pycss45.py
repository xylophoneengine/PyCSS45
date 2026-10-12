"""Cross-platform driver for the Gigahertz-Optik CSS45/MSC15 spectral sensor.

Speaks the CSS45/MSC15 wire protocol directly: USB HID via hidapi
(default) or RS232/RS485 via pyserial. Short ASCII commands (echo acks,
see PROTOCOL.md), LE-float32 spectrum blocks, and the I0 status after
set commands. The wire protocol was decoded from the vendor DLLs (whose
analysis remains in PROTOCOL.md as reverse-engineering provenance) and
hardware-verified on a live CSS45 (fw 1.58, macOS); remaining unknowns
are in PROTOCOL.md.
"""

from __future__ import annotations

import enum
import functools
import operator
import re
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, TypeVar

import numpy as np

if TYPE_CHECKING:
    import hid
    import serial

N_PIXELS: int = 288
VID: int = 0x0C49  # hardware-verified on a CSS45, serial 000000000051726
PID: int = 0x0010

ERRORS: dict[int, str] = {
    0: "success",
    -500: "communication error, close python instance and retry",
    -25000: "communication problem",
    -25001: "setup file invalid for this device",
    -25002: "setup file could not be opened",
    -25004: "operating mode outside valid range (valid: 0-2)",
    -25005: "communication channel cannot be initialized",
    -25006: "firmware version too low",
    -25007: "problem sending data",
    -25008: "problem receiving data",
    -25009: "device reports an unspecified error",
    -25014: "error main data EEPROM",
    -25015: "error colour data EEPROM",
    -25016: "error correction-factor data EEPROM",
    -25017: "error dark value EEPROM",
    -25018: "error calibration factor EEPROM",
    -25020: "too much ambient light",
    -25021: "measurement aborted",
    -25024: "error user data EEPROM",
    -25025: "spectral unit reports too little signal",
    -25031: "wrong device type, method not valid",
    -25032: "wrong password supplied",
    -25041: "device type CSS-D without detector not valid for SDK",
    -25100: "value outside valid range",
    25003: "warning: no default data saved yet, so no default file exists",
    25010: "warning: battery low",
    25022: "warning: unstable signal",
    25023: "warning: spectral unit reports overload",
}


class Css45Error(Exception):
    """Wire exchange returned a negative error code."""

    def __init__(self, code: int, message: str | None = None) -> None:
        self.code: int = code
        self.message: str = message or ERRORS.get(code, "unknown error")
        super().__init__(f"CSS45 error {code}: {self.message}")


INTEGRATION_SECONDS: dict[int, float] = {
    0: 0.000012,
    1: 0.00002,
    2: 0.00004,
    3: 0.00008,
    4: 0.00016,
    5: 0.00032,
    6: 0.00064,
    7: 0.00125,
    8: 0.0025,
    9: 0.005,
    10: 0.01,
    11: 0.02,
    12: 0.04,
    13: 0.08,
    14: 0.16,
    15: 0.32,
    16: 0.64,
    17: 1.28,
    18: 2.56,
}


class IntegrationTime(enum.IntEnum):
    """Index passed to measure_manual(); seconds are in INTEGRATION_SECONDS."""

    US12 = 0
    US20 = 1
    US40 = 2
    US80 = 3
    US160 = 4
    US320 = 5
    US640 = 6
    MS1_25 = 7
    MS2_5 = 8
    MS5 = 9
    MS10 = 10
    MS20 = 11
    MS40 = 12
    MS80 = 13
    MS160 = 14
    MS320 = 15
    MS640 = 16
    S1_28 = 17
    S2_56 = 18

    @property
    def seconds(self) -> float:
        """Nominal integration time in s (PROTOCOL.md table, unverified)."""
        return INTEGRATION_SECONDS[self.value]


class RadiometricUnit(enum.IntEnum):
    """Unit code of radiometric values (getRadiometricUnit)."""

    W = 0
    W_PER_M2 = 1


@dataclass(frozen=True)
class Measurement:
    wavelengths: np.ndarray  # nm, N_PIXELS values
    spectrum: np.ndarray  # W m^-2 nm^-1 per vendor calibration
    integration_time: float  # s
    cct: float  # K
    photopic: float  # photopic integral
    par: float


# --- wire constants ----------------------------------------------------------

_HID_MAX_CMD: int = 43  # 'command too long (> 43 char)' limit
_HID_CHUNK: int = 8  # ASCII bytes per output report
_HID_REPORT_IN: int = 65  # [report id][64 data bytes]
_HID_DATA: int = 64  # payload bytes per input report
_TIMEOUT_MS: int = 6000  # SDK normal timeout (ms, PROTOCOL.md)
_TIMEOUT_SLOW_MS: int = 11000  # SDK auto-dark / pulse timeout (ms)
_SERIAL_MAX_FRAME: int = 490  # RS232/485 payload cap (chars)
_UA_FAMILY: frozenset[int] = frozenset(range(112, 120))  # live CSS45: 117
_BLOCK: int = 16  # float32 values per GB0/GB3 binary block
_TRANSPORTS: tuple[str, ...] = ("hid", "serial")

T = TypeVar("T")


def _check_transport(transport: str) -> None:
    if transport not in _TRANSPORTS:
        raise ValueError(f"transport must be 'hid' or 'serial', got {transport!r}")


def _import_hid() -> types.ModuleType:
    """hidapi package ('import hid') or a clear error."""
    try:
        import hid
    except ImportError as exc:
        raise ImportError(
            "the hid transport needs the hidapi package (pip install hidapi)",
        ) from exc
    return hid


def _import_serial() -> types.ModuleType:
    """pyserial package or a clear error."""
    try:
        import serial
    except ImportError as exc:
        raise ImportError(
            "the serial transport needs pyserial (pip install pyserial)",
        ) from exc
    return serial


def _hid_path(path: str | bytes) -> bytes:
    """hidapi device path as bytes (find() returns surrogate-escaped str)."""
    if isinstance(path, bytes):
        return path
    return path.encode("utf-8", "surrogateescape")


def _checksum_byte(payload: bytes, mode: int) -> int:
    """Checksum byte per GOcommRS485: 1 = (0x55 - sum) & 0xFF, 2 = XOR."""
    if mode == 1:
        return (0x55 - sum(payload)) & 0xFF
    return functools.reduce(operator.xor, payload, 0)


def _checksum_chars(payload: bytes, mode: int) -> bytes:
    """Checksum as two ASCII chars, low nibble first, each | 0x40 (0: b'')."""
    if mode == 0:
        return b""
    cs: int = _checksum_byte(payload, mode)
    return bytes(((cs & 0x0F) | 0x40, (cs >> 4) | 0x40))


def _check_status(code: int) -> int:
    """Negative code raises, positive warning passes through (SDK rule)."""
    if code < 0:
        raise Css45Error(code)
    return code


# --- reply parsers (raise ValueError on a malformed reply) -------------------


def _parse_ihi(reply: str) -> float:
    """Ihi reply '<value>_<rest>'; value = integration time in us
    (hardware-observed: '2560000_0', '2500_0') -> s."""
    return float(reply.split("_", 1)[0]) / 1e6


def _parse_wl_borders(reply: str) -> tuple[float, float]:
    """IgH reply '<start>_<end>' in nm; hardware-observed as two fixed
    3-char fields without separator ('360830' = 360-830 nm)."""
    if "_" in reply:
        parts: list[str] = reply.split("_")
        return float(parts[0]), float(parts[-1])
    if len(reply) >= 6:
        return float(reply[:3]), float(reply[-3:])
    raise ValueError(reply)


# --- transports --------------------------------------------------------------


class _Wire(Protocol):
    """Wire-transport surface (framing per PROTOCOL.md).

    write() takes a bare ASCII command, read_ascii() returns one stripped
    reply, read_binary() returns exactly n raw bytes.
    """

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, cmd: str) -> None: ...

    def read_ascii(self, timeout_ms: int) -> str: ...

    def read_binary(self, n: int, timeout_ms: int) -> bytes: ...


class _HidTransport:
    """USB HID transport via hidapi; frames like the vendor's GOcommUSBHID.dll.

    Out: the command is split into 8-char ASCII chunks, each written as one
    9-byte output report [0x00 report id][8 bytes, NUL-padded]; commands
    are at most 43 chars, and the device answers every non-final report
    with one input report, which write() discards. In: 65-byte input reports [report id][64 data
    bytes]; ASCII replies are NUL-padded with no LF and end at LF or at
    the first NUL (echo acks carry one leading NUL byte), binary replies
    are exact byte counts spanning reports (all hardware-observed). The
    device opens by enumerated path; find() matches the hardware-verified
    VID/PID (0x0C49/0x0010) or 'Gigahertz' device strings.
    """

    def __init__(self, path: bytes) -> None:
        self._path: bytes = path
        self._dev: hid.device | None = None

    def _require(self) -> hid.device:
        if self._dev is None:
            raise RuntimeError("not connected")
        return self._dev

    def open(self) -> None:
        dev: hid.device = _import_hid().device()
        dev.open_path(self._path)  # IOError if the path is gone
        self._dev = dev

    def close(self) -> None:
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def write(self, cmd: str) -> None:
        data: bytes = cmd.encode("ascii")
        if len(data) > _HID_MAX_CMD:
            raise Css45Error(-25100, f"command too long (> 43 char): {cmd!r}")
        dev: hid.device = self._require()
        for i in range(0, len(data), _HID_CHUNK):
            chunk: bytes = data[i : i + _HID_CHUNK].ljust(_HID_CHUNK, b"\x00")
            if dev.write(b"\x00" + chunk) < 0:
                raise Css45Error(-25007, f"problem sending {cmd!r}")
            if i + _HID_CHUNK < len(data):
                # The device replies to each non-final report on its own
                # (hardware: hi2560000 -> 'i256000', the echo of 'hi256000');
                # drop it so read_ascii gets the reply to the whole command.
                self._read_report(_TIMEOUT_MS)

    def _read_report(self, timeout_ms: int) -> bytes:
        """Data bytes of one input report (report id 0x00; hidapi keeps the
        leading 0x00 on Windows but drops it elsewhere, unverified)."""
        data: bytes = bytes(self._require().read(_HID_REPORT_IN, timeout_ms))
        if not data:
            raise TimeoutError(f"no HID input report within {timeout_ms} ms")
        if len(data) == _HID_REPORT_IN and data[0] == 0:
            return data[1:]
        return data[-_HID_DATA:]

    def read_ascii(self, timeout_ms: int) -> str:
        # A reply ends at LF or at a NUL byte inside a report (replies are
        # NUL-padded; hardware-observed: 'uA' -> '000117' + NULs, no LF);
        # a full 64 data bytes without either continues in the next report.
        # Echo replies arrive with one leading NUL byte (observed: G07200
        # -> b'\x0007200'), which is stripped first.
        data: bytes = b""
        while True:
            chunk: bytes = self._read_report(timeout_ms)
            data += chunk
            if b"\n" in chunk or b"\x00" in chunk:
                break
        if data[:1] == b"\x00":
            data = data[1:]
        data = data.split(b"\n", 1)[0].split(b"\x00", 1)[0]
        return data.decode("ascii", errors="replace").strip()

    def read_binary(self, n: int, timeout_ms: int) -> bytes:
        out: bytearray = bytearray()
        while len(out) < n:
            out += self._read_report(timeout_ms)
        return bytes(out[:n])


class _SerialTransport:
    """RS232/RS485 transport via pyserial; frames like GOcommRS485.dll.

    Frame written = [startsign] + payload + [2-char checksum] + [endsign],
    where the checksum byte goes on the wire as two ASCII chars, low nibble
    first, each | 0x40 (modes: 0 none, 1 sum (0x55 - sum(payload)) & 0xFF,
    2 XOR). Replies are read up to the endsign; the startsign and a
    verified checksum are stripped. Defaults (no startsign, LF endsign, no
    checksum, no echo, 115200 8N1) are conservative and
    vendor-config-unverified (PROTOCOL.md).
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        startsign: int = 0,
        endsign: int = 0x0A,
        checksum: int = 0,
        echo: bool = False,
    ) -> None:
        if checksum not in (0, 1, 2):
            raise ValueError(f"checksum must be 0, 1 or 2, got {checksum}")
        if not 0 < endsign <= 0xFF:
            raise ValueError(f"endsign must be 1..255, got {endsign}")
        self._port: str = port
        self._baudrate: int = baudrate
        self._start: bytes = bytes((startsign,)) if startsign else b""
        self._end: bytes = bytes((endsign,))
        self._checksum: int = checksum
        self._echo: bool = echo
        self._ser: serial.Serial | None = None

    def _require(self) -> serial.Serial:
        if self._ser is None:
            raise RuntimeError("not connected")
        return self._ser

    def open(self) -> None:
        self._ser = _import_serial().Serial(
            self._port,
            baudrate=self._baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=_TIMEOUT_MS / 1000.0,
            write_timeout=_TIMEOUT_MS / 1000.0,
            exclusive=True,
        )

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def _set_timeout(self, timeout_ms: int) -> serial.Serial:
        ser: serial.Serial = self._require()
        ser.timeout = timeout_ms / 1000.0
        ser.write_timeout = timeout_ms / 1000.0
        return ser

    def write(self, cmd: str) -> None:
        payload: bytes = cmd.encode("ascii")
        if len(payload) > _SERIAL_MAX_FRAME:
            raise Css45Error(-25100, f"command too long: {cmd!r}")
        ser: serial.Serial = self._require()
        ser.write(
            self._start + payload + _checksum_chars(payload, self._checksum) + self._end
        )
        if self._echo:
            # The echoed frame ends with the endsign; read it back and drop it.
            ser.read_until(self._end)

    def read_ascii(self, timeout_ms: int) -> str:
        line: bytes = self._set_timeout(timeout_ms).read_until(
            self._end,
            size=_SERIAL_MAX_FRAME + 2,
        )
        if not line.endswith(self._end):
            raise TimeoutError(f"no reply frame within {timeout_ms} ms")
        line = line[:-1]
        if self._start and line.startswith(self._start):
            line = line[1:]
        if self._checksum:
            if len(line) < 2:
                raise Css45Error(-25008, "reply too short for a checksum")
            line, cs_chars = line[:-2], line[-2:]
            cs: int = ((cs_chars[1] & 0x0F) << 4) | (cs_chars[0] & 0x0F)
            if cs != _checksum_byte(line, self._checksum):
                raise Css45Error(-25008, "checksum mismatch in reply")
        return line.decode("ascii", errors="replace").strip()

    def read_binary(self, n: int, timeout_ms: int) -> bytes:
        # Binary framing (checksum / endsign) over RS232/485 is unverified;
        # raw exact-length reads are assumed (PROTOCOL.md).
        data: bytes = self._set_timeout(timeout_ms).read(n)
        if len(data) < n:
            raise TimeoutError(f"only {len(data)} of {n} bytes within {timeout_ms} ms")
        return data


# --- driver ------------------------------------------------------------------


class CSS45:
    """CSS45/MSC15 spectral sensor (288 pixels) via a wire transport.

    transport='hid' (default) speaks the USB HID wire protocol directly and
    works on Linux/macOS/Windows; 'serial' speaks RS232/RS485.

    Methods named _* (except the constructor helpers) expect the caller to
    hold self._lock; the public methods take it.
    """

    def __init__(
        self,
        transport: str = "hid",
        path: str | None = None,
        *,
        serial_port: str | None = None,
        baudrate: int = 115200,
        vid: int | None = None,
        pid: int | None = None,
        startsign: int = 0,
        endsign: int = 0x0A,
        checksum: int = 0,
        echo: bool = False,
    ) -> None:
        _check_transport(transport)
        self._lock: threading.Lock = threading.Lock()
        self._wire: _Wire | None = None
        self._usb_serial: str = ""  # USB descriptor serial (hid only)
        self._igr: int = 0  # Igr reply, meaning unverified (PROTOCOL.md)
        self._device_type: int = 0  # bF reply, meaning unverified

        wire: _Wire
        if transport == "hid":
            path_bytes: bytes
            path_bytes, self._usb_serial = self._resolve_hid(path, vid, pid)
            wire = _HidTransport(path_bytes)
        elif serial_port is None:
            raise ValueError(
                "the serial transport needs serial_port "
                "(e.g. 'COM3' or '/dev/ttyUSB0')",
            )
        else:
            wire = _SerialTransport(
                serial_port,
                baudrate=baudrate,
                startsign=startsign,
                endsign=endsign,
                checksum=checksum,
                echo=echo,
            )
        self._open(wire)

    @staticmethod
    def _resolve_hid(
        path: str | None,
        vid: int | None,
        pid: int | None,
    ) -> tuple[bytes, str]:
        """(HID path, USB descriptor serial): the first enumerated device,
        or the given path with its serial ('' if it is not enumerated)."""
        want: bytes | None = None if path is None else _hid_path(path)
        entry: dict[str, str] | None = next(
            (
                e
                for e in CSS45.find("hid", vid=vid, pid=pid)
                if want is None or _hid_path(e["path"]) == want
            ),
            None,
        )
        if entry is not None:
            return _hid_path(entry["path"]), entry["serial"]
        if want is not None:
            return want, ""
        raise LookupError(
            "no CSS45 found over USB HID (looked for VID/PID "
            f"{VID if vid is None else vid:#06x}/"
            f"{PID if pid is None else pid:#06x} or "
            "'Gigahertz' device strings; other MSC15-family "
            "variants may need explicit vid/pid, see PROTOCOL.md)",
        )

    @staticmethod
    def find(
        transport: str = "hid",
        serial_port: str | None = None,
        vid: int | None = None,
        pid: int | None = None,
    ) -> list[dict[str, str]]:
        """Candidate devices as dicts with path, manufacturer, product, serial.

        'hid': USB HID devices matching vid/pid (defaults: the CSS45's
        hardware-verified VID/PID, constant per device firmware and thus
        identical on every OS, but not guaranteed across device variants -
        pass vid/pid for other MSC15-family units) or, when the defaults
        are used, also a manufacturer or product string containing
        'Gigahertz' (the device reports 'Gigahertz Optik' with a space,
        not the vendor's legal name form). Entries sharing a path (one per
        HID usage) are deduplicated. 'serial': the given serial_port,
        else [].
        """
        _check_transport(transport)
        if transport == "serial":
            if serial_port is None:
                return []
            return [
                {
                    "path": serial_port,
                    "manufacturer": "",
                    "product": "",
                    "serial": "",
                }
            ]
        want: tuple[int, int] = (
            VID if vid is None else vid,
            PID if pid is None else pid,
        )
        exact: bool = vid is not None or pid is not None
        found: dict[str, dict[str, str]] = {}
        for d in _import_hid().enumerate():
            manufacturer: str = d.get("manufacturer_string") or ""
            product: str = d.get("product_string") or ""
            if (d.get("vendor_id"), d.get("product_id")) != want and (
                exact or "gigahertz" not in f"{manufacturer} {product}".lower()
            ):
                continue
            path: str = d["path"].decode("utf-8", "surrogateescape")
            found.setdefault(
                path,
                {
                    "path": path,
                    "manufacturer": manufacturer,
                    "product": product,
                    "serial": d.get("serial_number") or "",
                },
            )
        return list(found.values())

    # --- transport helpers --------------------------------------------------

    def _open(self, wire: _Wire) -> None:
        """Open wire and verify the device identity; close it on failure."""
        self._wire = wire
        try:
            wire.open()
            self._verify_identity()
        except BaseException:
            self._wire = None
            wire.close()
            raise

    def _verify_identity(self) -> None:
        """Check the uA family code and record Igr and bF.

        Hardware-observed on a CSS45 (serial 000000000051726, fw 1.58):
        uA -> 000117, Igr -> 51726 (the serial's last 5 digits), bF -> 4
        (4 = CSS-45 in the public device-type enum; meaning unverified).
        U18175 only echoes the command tail on the wire, so it carries no
        device type and is not queried (PROTOCOL.md). Called unlocked from
        _open() during construction.
        """
        ua: int = self._query("uA", int)
        if ua not in _UA_FAMILY:
            raise LookupError(
                f"not a CSS45/MSC15 device (uA={ua}, expected 112-119, PROTOCOL.md)",
            )
        self._igr = self._query("Igr", int)
        self._device_type = self._query("bF", int)

    def _require_wire(self) -> _Wire:
        if self._wire is None:
            raise RuntimeError("not connected")
        return self._wire

    def _exchange(self, cmd: str, timeout_ms: int = _TIMEOUT_MS) -> str:
        """Send cmd, return the stripped ASCII reply."""
        wire: _Wire = self._require_wire()
        wire.write(cmd)
        try:
            return wire.read_ascii(timeout_ms)
        except TimeoutError:
            raise Css45Error(
                -25008,
                f"timeout waiting for the reply to {cmd!r}",
            ) from None

    def _query(
        self,
        cmd: str,
        parse: Callable[[str], T],
        timeout_ms: int = _TIMEOUT_MS,
    ) -> T:
        """Send cmd, return parse(reply); a ValueError becomes Css45Error."""
        reply: str = self._exchange(cmd, timeout_ms)
        try:
            return parse(reply)
        except ValueError:
            raise Css45Error(
                -25008,
                f"invalid reply to {cmd!r}: {reply!r}",
            ) from None

    def _ack(self, cmd: str, timeout_ms: int = _TIMEOUT_MS) -> int:
        """Set/measure-command flow: reply is the command-tail echo
        (hardware-observed: G07200 -> '07200', hh0 -> 'h0') or, per the
        vendor SDK disassembly, '*'; I0 then carries the status."""
        reply: str = self._exchange(cmd, timeout_ms)
        if reply not in ("*", cmd[1:]):
            raise Css45Error(-25008, f"no ack to {cmd!r}, got {reply!r}")
        return _check_status(self._query("I0", int, timeout_ms))

    def _binary(self, cmd: str, n: int) -> np.ndarray:
        """Send cmd, read 4*n bytes as n little-endian float32 values."""
        wire: _Wire = self._require_wire()
        wire.write(cmd)
        try:
            data: bytes = wire.read_binary(4 * n, _TIMEOUT_MS)
        except TimeoutError:
            raise Css45Error(
                -25008,
                f"timeout waiting for the binary reply to {cmd!r}",
            ) from None
        return np.frombuffer(data, dtype="<f4").astype(np.float64)

    def _ihdv(self) -> np.ndarray | None:
        """IhdV wavelength-correction values, or None if unparseable (the
        reply format is unverified, PROTOCOL.md)."""
        reply: str = self._exchange("IhdV")
        try:
            values: list[float] = [
                float(t) for t in re.split(r"[\s_,;]+", reply.strip()) if t
            ]
        except ValueError:
            return None
        return np.asarray(values) if len(values) == N_PIXELS else None

    def _wl_mapping(self) -> np.ndarray:
        """Per-pixel wavelengths in nm; mirrors the vendor SDK host-side math.

        No wire command returns the per-pixel grid (the vendor DLL loads it
        from the per-device .gdf setup file, PROTOCOL.md open questions);
        derived as a uniform grid between the IgH borders plus the IhdV
        corrections when that reply parses as N_PIXELS floats.
        """
        start, end = self._query("IgH", _parse_wl_borders)
        grid: np.ndarray = np.linspace(start, end, N_PIXELS)
        corrections: np.ndarray | None = self._ihdv()
        return grid if corrections is None else grid + corrections

    def _spectrum_by_pixel(self) -> np.ndarray:
        """hh0 selects the raw buffer; 18 x GB016 = 288 LE float32 values."""
        self._ack("hh0")
        return np.concatenate(
            [self._binary("GB016", _BLOCK) for _ in range(N_PIXELS // _BLOCK)]
        )

    def _spectrum_interpolated(
        self,
        start_wl: float,
        delta_wl: float,
        n: int,
    ) -> np.ndarray:
        """GB1/GB2 select the grid; GB316 blocks of 16 plus one GB3<n>."""
        if n < 0:
            raise ValueError(f"n must be >= 0, got {n}")
        self._ack(f"GB1{start_wl:.1f}")  # %.1f, like the BTS256 SDK (observed)
        self._ack(f"GB2{delta_wl:.1f}")
        full, remainder = divmod(n, _BLOCK)
        sizes: list[int] = [_BLOCK] * full + ([remainder] if remainder else [])
        if not sizes:
            return np.empty(0, dtype=np.float64)
        return np.concatenate([self._binary(f"GB3{k}", k) for k in sizes])

    def _measure(self, *setup: str) -> Measurement:
        """Ack the setup commands, then Ihi, G17 and read out the result."""
        for cmd in setup:
            self._ack(cmd, _TIMEOUT_SLOW_MS)
        integration_time: float = self._query("Ihi", _parse_ihi, _TIMEOUT_SLOW_MS)
        self._ack("G17", _TIMEOUT_SLOW_MS)
        return Measurement(
            wavelengths=self._wl_mapping(),
            spectrum=self._spectrum_by_pixel(),
            integration_time=integration_time,
            cct=self._query("G22", float),
            photopic=self._query("G2R", float),
            par=self._query("G2L", float),
        )

    # --- lifecycle ----------------------------------------------------------

    def disconnect(self) -> None:
        with self._lock:
            if self._wire is not None:
                self._wire.close()
                self._wire = None

    def __enter__(self) -> CSS45:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.disconnect()

    def connected(self) -> bool:
        """True while the transport is open (no device round-trip)."""
        with self._lock:
            return self._wire is not None

    # --- info ---------------------------------------------------------------

    def get_serial_number(self) -> str:
        """Device serial number.

        hid: the USB descriptor string from enumeration (no wire command
        exists); serial: "" (no wire command, PROTOCOL.md open questions).
        """
        with self._lock:
            self._require_wire()
            return self._usb_serial

    def get_firmware_version(self) -> str:
        """Firmware version; wire 'P' replies a float (format unverified),
        returned as the reply string."""
        with self._lock:
            return self._exchange("P")

    def get_status(self) -> int:
        """Device status word (wire: I0); nonzero values are
        warnings/errors (ERRORS).

        By SDK convention (PROTOCOL.md) positive return codes are warnings
        and never raise anywhere in this driver; negative codes raise
        Css45Error. A measurement that returned a warning still delivered
        data; check this word or the per-call codes to see it.
        """
        with self._lock:
            return self._query("I0", int)

    def get_temperature(self) -> float:
        """PCB temperature in deg C (unit unverified, see PROTOCOL.md)."""
        with self._lock:
            return self._query("W", float)

    # --- measurements -------------------------------------------------------

    def measure(self) -> Measurement:
        """Auto-integration-time measurement (wire: G07200, Ihi, G17)."""
        with self._lock:
            return self._measure("G07200")

    def measure_manual(self, integration_time: IntegrationTime) -> Measurement:
        """Measurement with a fixed integration-time index (0-18).

        Wire: hi<us> takes microseconds 12..2560000 (the index table maps
        1:1), then G0400, Ihi, G17.
        """
        us: int = round(IntegrationTime(integration_time).seconds * 1e6)
        with self._lock:
            return self._measure(f"hi{us}", "G0400")

    def measure_dark_offset(self) -> None:
        """Trigger a dark/offset measurement (wire: GZA auto variant)."""
        with self._lock:
            self._ack("GZA", _TIMEOUT_SLOW_MS)

    def is_offset_invalid(self) -> bool:
        """True if the stored dark offset must be re-measured (wire: GZC)."""
        with self._lock:
            return self._query("GZC", int) != 0

    def get_last_integration_time(self) -> float:
        """Integration time of the last measurement in s (wire: Ihi, value
        in us, hardware-observed; the vendor DLL caches it in measure)."""
        with self._lock:
            return self._query("Ihi", _parse_ihi)

    # --- spectrum getters ---------------------------------------------------

    def get_wl_mapping(self) -> np.ndarray:
        """Per-pixel wavelengths in nm (N_PIXELS values).

        Host-side derivation from the IgH borders plus the IhdV
        corrections; the vendor DLL loads the per-pixel grid from the
        .gdf setup file, for which no wire command exists (PROTOCOL.md open
        questions).
        """
        with self._lock:
            return self._wl_mapping()

    def get_wl_borders(self) -> tuple[float, float]:
        """(start, end) wavelength of the spectral range in nm: first/last
        of the wavelength mapping, mirroring the vendor SDK host-side math.
        """
        with self._lock:
            wavelengths: np.ndarray = self._wl_mapping()
        return float(wavelengths[0]), float(wavelengths[-1])

    def get_spectrum_by_pixel(self) -> np.ndarray:
        """Per-pixel spectral values (N_PIXELS, vendor calibration unit).

        Wire: hh0 then 18 x GB016 binary blocks (16 LE float32 each).
        """
        with self._lock:
            return self._spectrum_by_pixel()

    def get_spectrum_interpolated(
        self,
        start_wl: float,
        delta_wl: float,
        n: int,
    ) -> np.ndarray:
        """Interpolated spectrum: n values from start_wl nm, step delta_wl nm.

        Wire: GB1%f start, GB2%f step, GB316 blocks plus one GB3<n> remainder.
        """
        with self._lock:
            return self._spectrum_interpolated(start_wl, delta_wl, n)

    def get_cct(self) -> float:
        """Correlated colour temperature in K (wire: G22)."""
        with self._lock:
            return self._query("G22", float)

    def get_photopic(self) -> float:
        """Photopic integral (wire: G2R)."""
        with self._lock:
            return self._query("G2R", float)

    def get_par(self) -> float:
        """Photosynthetically active radiation (wire: G2L)."""
        with self._lock:
            return self._query("G2L", float)

    def get_radiometric_value(self, start_wl: float, end_wl: float) -> float:
        """Radiometric integral from start_wl to end_wl in nm.

        Computed host-side as the integral of the interpolated spectrum on
        a ~1 nm grid, mirroring the vendor SDK host-side math (GOMath.dll);
        the device-side G2C%03d%03d command exists too (PROTOCOL.md) but
        its reply semantics are unverified.
        """
        if end_wl <= start_wl:
            raise ValueError(
                f"end_wl must be > start_wl, got {start_wl}..{end_wl}",
            )
        n: int = max(2, round(end_wl - start_wl) + 1)
        grid: np.ndarray = np.linspace(start_wl, end_wl, n)
        with self._lock:
            spectrum: np.ndarray = self._spectrum_interpolated(
                float(grid[0]),
                float(grid[1] - grid[0]),
                n,
            )
        return float(np.trapezoid(spectrum, grid))

    def get_radiometric_unit(self) -> RadiometricUnit:
        """Unit of the radiometric values (W or W/m2; wire: IhcU)."""
        with self._lock:
            unit: int = self._query("IhcU", int)
        try:
            return RadiometricUnit(unit)
        except ValueError:
            raise ValueError(f"unknown radiometric unit code {unit}") from None
