"""Hardware-free tests for pycss45: wire framing, reply parsing and the
CSS45 command flow against scripted fake transports."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import pycss45
from pycss45 import N_PIXELS, CSS45, Css45Error, IntegrationTime

IDENTITY: dict[str, str | bytes] = {"uA": "000117", "Igr": "51726", "bF": "4"}


def floats(values: np.ndarray | list[float]) -> bytes:
    return np.asarray(values, dtype="<f4").tobytes()


class FakeWire:
    """Scripted _Wire: each written command is answered by replies[cmd]
    (identity replies included); a command without a reply times out."""

    def __init__(self, replies: dict[str, str | bytes]) -> None:
        self.replies: dict[str, str | bytes] = {**IDENTITY, **replies}
        self.sent: list[str] = []
        self.closed: bool = False
        self._reply: str | bytes | None = None

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def write(self, cmd: str) -> None:
        self.sent.append(cmd)
        self._reply = self.replies.get(cmd)

    def read_ascii(self, timeout_ms: int) -> str:
        if self._reply is None:
            raise TimeoutError
        assert isinstance(self._reply, str)
        return self._reply

    def read_binary(self, n: int, timeout_ms: int) -> bytes:
        if self._reply is None:
            raise TimeoutError
        assert isinstance(self._reply, bytes) and len(self._reply) == n
        return self._reply


def connect(wire: FakeWire) -> CSS45:
    with (
        mock.patch.object(pycss45, "_HidTransport", return_value=wire),
        mock.patch.object(CSS45, "_resolve_hid", return_value=(b"p", "SN1")),
    ):
        return CSS45()


class FakeHidDevice:
    """hidapi device: records output reports, plays back input reports."""

    def __init__(self, reports: list[bytes]) -> None:
        self.reports: list[bytes] = reports
        self.written: list[bytes] = []

    def open_path(self, path: bytes) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def read(self, size: int, timeout_ms: int) -> list[int]:
        return list(self.reports.pop(0)) if self.reports else []


def hid_transport(dev: FakeHidDevice) -> pycss45._HidTransport:
    t: pycss45._HidTransport = pycss45._HidTransport(b"p")
    hid: SimpleNamespace = SimpleNamespace(device=mock.Mock(return_value=dev))
    with mock.patch.object(pycss45, "_import_hid", return_value=hid):
        t.open()
    return t


class FakeSerial:
    """pyserial Serial: records tx, serves rx."""

    def __init__(self, rx: bytes) -> None:
        self.rx: bytes = rx
        self.tx: bytes = b""
        self.timeout: float = 0.0
        self.write_timeout: float = 0.0

    def write(self, data: bytes) -> int:
        self.tx += data
        return len(data)

    def read_until(self, expected: bytes, size: int | None = None) -> bytes:
        i: int = self.rx.find(expected)
        end: int = len(self.rx) if i < 0 else i + len(expected)
        line, self.rx = self.rx[:end], self.rx[end:]
        return line

    def read(self, n: int) -> bytes:
        data, self.rx = self.rx[:n], self.rx[n:]
        return data

    def close(self) -> None:
        pass


def serial_transport(
    rx: bytes,
    startsign: int = 0,
    checksum: int = 0,
) -> tuple[pycss45._SerialTransport, FakeSerial]:
    ser: FakeSerial = FakeSerial(rx)
    t: pycss45._SerialTransport = pycss45._SerialTransport(
        "COM1", startsign=startsign, checksum=checksum
    )
    serial: SimpleNamespace = SimpleNamespace(Serial=mock.Mock(return_value=ser))
    with mock.patch.object(pycss45, "_import_serial", return_value=serial):
        t.open()
    return t, ser


class TestHelpers(unittest.TestCase):
    def test_checksum(self) -> None:
        # b"uA" = 0x75 0x41: sum mode (0x55 - 0xB6) & 0xFF = 0x9F, XOR = 0x34
        self.assertEqual(pycss45._checksum_byte(b"uA", 1), 0x9F)
        self.assertEqual(pycss45._checksum_byte(b"uA", 2), 0x34)
        self.assertEqual(pycss45._checksum_chars(b"uA", 1), b"OI")
        self.assertEqual(pycss45._checksum_chars(b"uA", 2), b"DC")
        self.assertEqual(pycss45._checksum_chars(b"uA", 0), b"")

    def test_check_status(self) -> None:
        self.assertEqual(pycss45._check_status(0), 0)
        self.assertEqual(pycss45._check_status(25022), 25022)
        with self.assertRaises(Css45Error) as cm:
            pycss45._check_status(-25021)
        self.assertEqual(cm.exception.code, -25021)
        self.assertEqual(cm.exception.message, "measurement aborted")
        self.assertEqual(Css45Error(-1).message, "unknown error")

    def test_parse_ihi(self) -> None:
        self.assertEqual(pycss45._parse_ihi("2560000_0"), 2.56)
        self.assertEqual(pycss45._parse_ihi("2500_0"), 0.0025)
        with self.assertRaises(ValueError):
            pycss45._parse_ihi("x_0")

    def test_parse_wl_borders(self) -> None:
        self.assertEqual(pycss45._parse_wl_borders("360830"), (360.0, 830.0))
        self.assertEqual(pycss45._parse_wl_borders("360_830"), (360.0, 830.0))
        with self.assertRaises(ValueError):
            pycss45._parse_wl_borders("36")

    def test_integration_time(self) -> None:
        self.assertEqual(
            set(pycss45.INTEGRATION_SECONDS), {t.value for t in IntegrationTime}
        )
        self.assertEqual(IntegrationTime.S2_56.seconds, 2.56)
        self.assertEqual(IntegrationTime.US12.seconds, 12e-6)


class TestHidTransport(unittest.TestCase):
    def test_write_chunks_into_reports(self) -> None:
        dev: FakeHidDevice = FakeHidDevice([b"\x00bcdefgh".ljust(64, b"\x00")])
        hid_transport(dev).write("abcdefghij")
        self.assertEqual(dev.written, [b"\x00abcdefgh", b"\x00ij" + b"\x00" * 6])
        self.assertEqual(dev.reports, [])  # per-report reply consumed

    def test_long_command_echo(self) -> None:
        # Real CSS45 at 2.56 s: hi2560000 spans two reports; the first gets
        # its own echo, which used to be taken as the ack ('i256000').
        reports: list[bytes] = [
            b"\x00i256000".ljust(64, b"\x00"),
            b"\x00i2560000".ljust(64, b"\x00"),
        ]
        t: pycss45._HidTransport = hid_transport(FakeHidDevice(reports))
        t.write("hi2560000")
        self.assertEqual(t.read_ascii(10), "i2560000")
        with self.assertRaises(TimeoutError):  # nothing left to desync on
            t.read_ascii(10)

    def test_write_too_long(self) -> None:
        with self.assertRaises(Css45Error) as cm:
            hid_transport(FakeHidDevice([])).write("x" * 44)
        self.assertEqual(cm.exception.code, -25100)

    def test_read_ascii(self) -> None:
        echo: bytes = b"\x0007200".ljust(64, b"\x00")
        windows: bytes = b"\x00" + b"000117".ljust(64, b"\x00")  # report id kept
        long: list[bytes] = [b"1" * 64, b"23\n".ljust(64, b"\x00")]
        t: pycss45._HidTransport = hid_transport(
            FakeHidDevice([echo, windows, *long])
        )
        self.assertEqual(t.read_ascii(10), "07200")
        self.assertEqual(t.read_ascii(10), "000117")
        self.assertEqual(t.read_ascii(10), "1" * 64 + "23")
        with self.assertRaises(TimeoutError):
            t.read_ascii(10)

    def test_read_binary_spans_reports(self) -> None:
        reports: list[bytes] = [bytes(range(64)), bytes(range(64, 128))]
        t: pycss45._HidTransport = hid_transport(FakeHidDevice(reports))
        self.assertEqual(t.read_binary(100, 10), bytes(range(100)))


class TestSerialTransport(unittest.TestCase):
    def test_write_frame(self) -> None:
        t, ser = serial_transport(b"", startsign=0x02, checksum=1)
        t.write("uA")
        self.assertEqual(ser.tx, b"\x02uAOI\n")

    def test_read_ascii_checksum(self) -> None:
        good: bytes = b"\x02000117" + pycss45._checksum_chars(b"000117", 1) + b"\n"
        t, _ = serial_transport(good + b"\x02000117AA\n", startsign=0x02, checksum=1)
        self.assertEqual(t.read_ascii(10), "000117")
        with self.assertRaises(Css45Error):
            t.read_ascii(10)

    def test_read_ascii_timeout(self) -> None:
        t, _ = serial_transport(b"000117")
        with self.assertRaises(TimeoutError):
            t.read_ascii(10)

    def test_invalid_checksum_mode(self) -> None:
        with self.assertRaises(ValueError):
            pycss45._SerialTransport("COM1", checksum=3)


class TestFind(unittest.TestCase):
    def test_hid(self) -> None:
        def entry(
            vid: int, pid: int, path: bytes, manufacturer: str
        ) -> dict[str, object]:
            return {
                "vendor_id": vid,
                "product_id": pid,
                "path": path,
                "manufacturer_string": manufacturer,
                "product_string": "",
                "serial_number": "S",
            }

        devices: list[dict[str, object]] = [
            entry(pycss45.VID, pycss45.PID, b"a", ""),
            entry(pycss45.VID, pycss45.PID, b"a", ""),  # second HID usage
            entry(1, 2, b"b", "Gigahertz Optik"),
            entry(1, 2, b"c", "Other"),
        ]
        hid: SimpleNamespace = SimpleNamespace(
            enumerate=mock.Mock(return_value=devices)
        )
        with mock.patch.object(pycss45, "_import_hid", return_value=hid):
            self.assertEqual([d["path"] for d in CSS45.find()], ["a", "b"])
            self.assertEqual([d["path"] for d in CSS45.find(vid=1, pid=2)], ["b", "c"])

    def test_serial_and_bad_transport(self) -> None:
        self.assertEqual(CSS45.find("serial"), [])
        self.assertEqual(CSS45.find("serial", "COM3")[0]["path"], "COM3")
        with self.assertRaises(ValueError):
            CSS45.find("usb")
        with self.assertRaises(ValueError):
            CSS45("serial")


BLOCK: bytes = floats(np.arange(16.0))
MEASURE: dict[str, str | bytes] = {
    "G07200": "07200",
    "I0": "0",
    "Ihi": "2560000_0",
    "G17": "17",
    "IgH": "360830",
    "IhdV": "",
    "hh0": "h0",
    "GB016": BLOCK,
    "G22": "5000",
    "G2R": "1.5",
    "G2L": "2.5",
}


class TestCSS45(unittest.TestCase):
    def test_identity(self) -> None:
        wire: FakeWire = FakeWire({})
        dev: CSS45 = connect(wire)
        self.assertEqual(wire.sent, ["uA", "Igr", "bF"])
        self.assertEqual(dev.get_serial_number(), "SN1")

    def test_wrong_device_closes_wire(self) -> None:
        wire: FakeWire = FakeWire({"uA": "000042"})
        with self.assertRaises(LookupError):
            connect(wire)
        self.assertTrue(wire.closed)

    def test_context_manager_disconnects(self) -> None:
        wire: FakeWire = FakeWire({})
        with connect(wire) as dev:
            self.assertTrue(dev.connected())
        self.assertTrue(wire.closed)
        self.assertFalse(dev.connected())
        with self.assertRaises(RuntimeError):
            dev.get_status()

    def test_measure(self) -> None:
        wire: FakeWire = FakeWire(MEASURE)
        m: pycss45.Measurement = connect(wire).measure()
        self.assertEqual(
            wire.sent[3:10], ["G07200", "I0", "Ihi", "G17", "I0", "IgH", "IhdV"]
        )
        self.assertEqual(wire.sent.count("GB016"), N_PIXELS // 16)
        self.assertEqual(m.integration_time, 2.56)
        np.testing.assert_allclose(m.wavelengths, np.linspace(360, 830, N_PIXELS))
        np.testing.assert_array_equal(m.spectrum, np.tile(np.arange(16.0), 18))
        self.assertEqual((m.cct, m.photopic, m.par), (5000.0, 1.5, 2.5))

    def test_measure_manual(self) -> None:
        wire: FakeWire = FakeWire({**MEASURE, "hi10000": "*", "G0400": "0400"})
        connect(wire).measure_manual(IntegrationTime.MS10)
        self.assertEqual(wire.sent[3:7], ["hi10000", "I0", "G0400", "I0"])

    def test_wl_mapping_applies_ihdv_corrections(self) -> None:
        ihdv: str = " ".join(["0.5"] * N_PIXELS)
        dev: CSS45 = connect(FakeWire({"IgH": "360_830", "IhdV": ihdv}))
        np.testing.assert_allclose(
            dev.get_wl_mapping(), np.linspace(360, 830, N_PIXELS) + 0.5
        )
        self.assertEqual(dev.get_wl_borders(), (360.5, 830.5))

    def test_ack_errors(self) -> None:
        cases: list[tuple[dict[str, str | bytes], int]] = [
            ({"GZA": "nope", "I0": "0"}, -25008),  # wrong echo
            ({"GZA": "ZA", "I0": "-25021"}, -25021),  # negative status
            ({}, -25008),  # timeout
        ]
        for replies, code in cases:
            with self.subTest(replies=replies):
                with self.assertRaises(Css45Error) as cm:
                    connect(FakeWire(replies)).measure_dark_offset()
                self.assertEqual(cm.exception.code, code)
        # positive status is a warning, not an error
        connect(FakeWire({"GZA": "ZA", "I0": "25022"})).measure_dark_offset()

    def test_invalid_reply(self) -> None:
        with self.assertRaises(Css45Error) as cm:
            connect(FakeWire({"W": "abc"})).get_temperature()
        self.assertEqual(cm.exception.code, -25008)

    def test_spectrum_interpolated(self) -> None:
        wire: FakeWire = FakeWire(
            {
                "GB1380.0": "B1380.0",
                "GB25.0": "B25.0",
                "I0": "0",
                "GB316": floats(np.ones(16)),
                "GB34": floats(np.ones(4)),
            }
        )
        dev: CSS45 = connect(wire)
        self.assertEqual(len(dev.get_spectrum_interpolated(380, 5, 20)), 20)
        self.assertEqual(wire.sent[-2:], ["GB316", "GB34"])
        self.assertEqual(len(dev.get_spectrum_interpolated(380, 5, 0)), 0)

    def test_radiometric_value(self) -> None:
        # 400..500 nm -> 101 values on a 1 nm grid: 6 x GB316 + GB35
        dev: CSS45 = connect(
            FakeWire(
                {
                    "GB1400.0": "*",
                    "GB21.0": "*",
                    "I0": "0",
                    "GB316": floats(np.ones(16)),
                    "GB35": floats(np.ones(5)),
                }
            )
        )
        self.assertAlmostEqual(dev.get_radiometric_value(400, 500), 100.0, places=4)
        with self.assertRaises(ValueError):
            dev.get_radiometric_value(500, 400)

    def test_radiometric_unit(self) -> None:
        dev: CSS45 = connect(FakeWire({"IhcU": "1"}))
        self.assertEqual(dev.get_radiometric_unit(), pycss45.RadiometricUnit.W_PER_M2)
        with self.assertRaises(ValueError):
            connect(FakeWire({"IhcU": "7"})).get_radiometric_unit()


if __name__ == "__main__":
    unittest.main()
