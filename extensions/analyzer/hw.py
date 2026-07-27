"""Hardware layer for the Pi module agent — real and simulated.

Real classes mirror the deployed NH4MOD gateway exactly:
  * RealSerialPort — pyserial, 9600 8N1, line-based, reconnect w/ backoff.
    Port A = PLC (controller) · Port B = pump controller (EZ-stepper ASCII).
  * RealADC — ADS1115 over I2C (adafruit), voltage divider / 4-20 mA shunt.

Sim classes run the IDENTICAL agent code with no hardware (UII_SIM=1):
  * SimSerialPort — the pump controller answers EZ-stepper commands with an
    ack; the PLC port is silent unless a test injects traffic.
  * SimADC — synthetic detector physics: a hidden true concentration drives
    absorbance; the ADC only ever reports volts. Capture-name vocabulary
    (…_DIW_…, …_STD_…, …_SAMP_…, …_5X) selects what liquid is "in the cell",
    exactly as the real timelines put it there. The hub must recover the
    hidden slope through a calibrate run — same epistemics as the field.

pyserial / adafruit imports are guarded so the sim runs anywhere.
"""
from __future__ import annotations

import math
import queue
import random
import threading
import time
from typing import Callable, Optional

_SERIAL_OK = True
try:
    import serial  # type: ignore
except Exception:
    _SERIAL_OK = False

_ADC_OK = True
try:
    import board  # type: ignore
    import busio  # type: ignore
    import adafruit_ads1x15.ads1115 as ADS  # type: ignore
    from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore
except Exception:
    _ADC_OK = False


# ---------------------------------------------------------------------------
# Serial
# ---------------------------------------------------------------------------

class RealSerialPort(threading.Thread):
    """Line-based serial worker, ported from the gateway's SerialPortWorker."""

    def __init__(self, name: str, device: str, baud: int,
                 on_line: Callable[[str, str], None]):
        super().__init__(daemon=True)
        self.name = name
        self.device = device
        self.baud = baud
        self.on_line = on_line
        self.ok = False
        self.err = ""
        self._stop = threading.Event()
        self._ser = None
        self._lock = threading.Lock()

    def write_line(self, line: str) -> None:
        with self._lock:
            if not self._ser or not self._ser.is_open:
                raise RuntimeError(f"{self.name}: port not open")
            data = (line.rstrip("\r\n") + "\r\n").encode("utf-8", errors="replace")
            self._ser.write(data)
            self._ser.flush()

    def stop(self):
        self._stop.set()

    def run(self):
        if not _SERIAL_OK:
            self.err = "pyserial not installed"
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self._ser or not self._ser.is_open:
                    self._ser = serial.Serial(
                        port=self.device, baudrate=self.baud,
                        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE, timeout=0.2,
                        write_timeout=0.6)
                    self.ok, self.err, backoff = True, "", 1.0
                raw = self._ser.readline()
                if raw:
                    txt = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    self.on_line(self.name, txt)
            except Exception as e:  # noqa: BLE001 — field resilience
                self.ok, self.err = False, str(e)
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                time.sleep(backoff)
                backoff = min(15.0, backoff * 1.5)


class SimSerialPort(threading.Thread):
    """Sim port. The pump controller (role='pump') acks EZ-stepper commands;
    the PLC port stays silent. Tests can inject() inbound lines and inspect
    .sent for outbound ones."""

    def __init__(self, name: str, device: str, baud: int,
                 on_line: Callable[[str, str], None], role: str = "silent"):
        super().__init__(daemon=True)
        self.name = name
        self.role = role
        self.on_line = on_line
        self.ok = True
        self.err = ""
        self.sent: list[str] = []
        self._rx: queue.Queue = queue.Queue()
        self._stop = threading.Event()

    def write_line(self, line: str) -> None:
        self.sent.append(line)
        if self.role == "pump" and line.startswith("/") and line.endswith("R"):
            # EZ-stepper answers a terse ack
            self._rx.put("/0@\x03")

    def inject(self, line: str) -> None:
        self._rx.put(line)

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                line = self._rx.get(timeout=0.2)
            except queue.Empty:
                continue
            self.on_line(self.name, line)


# ---------------------------------------------------------------------------
# ADC / detector
# ---------------------------------------------------------------------------

class RealADC:
    """ADS1115 read, ported from the gateway's _init_adc/_read_signal."""

    def __init__(self, enabled: bool, gain: int = 1, channel: int = 0,
                 divider_ratio: float = 1.0, shunt_ohms: float = 250.0,
                 signal_mode: str = "voltage"):
        self.divider_ratio = divider_ratio
        self.shunt_ohms = shunt_ohms
        self.signal_mode = signal_mode
        self.ready = False
        self._chan = None
        if not enabled or not _ADC_OK:
            return
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c)
            ads.gain = int(gain)
            channel_map = [ADS.P0, ADS.P1, ADS.P2, ADS.P3]
            self._chan = AnalogIn(ads, channel_map[max(0, min(3, int(channel)))])
            self.ready = True
        except Exception:
            self.ready = False

    def read(self, context: Optional[dict] = None) -> dict:
        if not self.ready or not self._chan:
            return {"vadc": None, "vin": None, "ma": None}
        try:
            vadc = float(self._chan.voltage)
            vin = vadc * float(self.divider_ratio)
            ma = (vin / float(self.shunt_ohms)) * 1000.0 \
                if str(self.signal_mode).lower() == "current_4_20" else None
            return {"vadc": vadc, "vin": vin, "ma": ma}
        except Exception:
            return {"vadc": None, "vin": None, "ma": None}


# Hidden truth of the simulated chemistry — per optical channel. The hub
# never sees these constants; it must recover them through calibration.
TRUE_SLOPE = {"NH4": 25.0, "PO4": 12.0, "NOX": 20.0, "NO2": 18.0}
I0_VOLTS = 2.400


class SimADC:
    """Synthetic detector. read(context) needs context={"name": capture_name,
    "std_conc": float|None} — the capture name says what liquid the timeline
    has put in the flow cell, exactly like the real method does."""

    def __init__(self, analyte: str, seed: Optional[int] = None):
        self.analyte = analyte.upper()
        self.ready = True
        self.t0 = time.time()
        self._rng = random.Random(seed)
        self._pending_i0: dict[str, tuple[float, float]] = {}  # pair key -> (i0, A)

    # hidden true stream concentrations
    def true_conc(self, channel: str) -> float:
        t = time.time() - self.t0
        base = {"NH4": 4.5, "PO4": 1.8, "NOX": 6.0, "NO2": 1.5}[channel]
        return max(0.05, base + 0.25 * base * math.sin(t / 90.0)
                   + self._rng.gauss(0, 0.01 * base))

    def _absorbance_for(self, name: str, std_conc: Optional[float]) -> float:
        channel = "NO2" if name.startswith("NO2") else self.analyte
        if "_DIW_" in name:
            conc = 0.0            # blank
        elif "_STD_" in name:
            conc = float(std_conc or 5.0)
        else:                     # sample from the stream
            conc = self.true_conc(channel)
        if name.endswith("_5X"):
            conc = conc / 5.0     # 5x dilution path
        return conc / TRUE_SLOPE[channel]

    def read(self, context: Optional[dict] = None) -> dict:
        name = (context or {}).get("name", "")
        std_conc = (context or {}).get("std_conc")
        pair = name.replace("_I0", "").replace("_I1", "")
        if "_I0" in name:
            a = self._absorbance_for(name, std_conc)
            i0 = I0_VOLTS * (1 + self._rng.gauss(0, 0.002))
            self._pending_i0[pair] = (i0, a)
            vin = i0
        else:
            i0, a = self._pending_i0.pop(
                pair, (I0_VOLTS, self._absorbance_for(name, std_conc)))
            vin = i0 * (10 ** -a) * (1 + self._rng.gauss(0, 0.002))
        vin = round(vin, 5)
        return {"vadc": vin, "vin": vin, "ma": None}
