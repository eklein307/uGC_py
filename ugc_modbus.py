#!/usr/bin/env python3
"""
ugc_modbus.py - Command-line interface between a computer and an Agilent 990
Micro GC (PRO license) over Modbus TCP or Modbus serial (RTU).

Inputs : trigger run                        (parameter ID 0, Start Run)
         run type                           (parameter ID 60: analysis/calibration/blank/verification)
         calibration level                  (parameter ID 61, calibration runs)
         stream selector valve position     (parameter ID 62)
Outputs: new data available                 (parameter ID 2201, Sync: Data available with reset)
         peak information for all channels  (parameter IDs 1202, 1219, 1203, 1204, 1205, 1207)
         as JSON

On the 990 the Modbus register map is not fixed: registers are linked to
parameter IDs in PROstation (Modbus tab -> Registers setup). register_map.json
describes that table; `python ugc_modbus.py table` prints it so it can be
entered in PROstation exactly as this tool expects.

Usage
-----
  python ugc_modbus.py table                    # PROstation Modbus table to enter (CSV)
  python ugc_modbus.py trigger                  # start a run with current settings
  python ugc_modbus.py trigger --run-type calibration --cal-level 1 --valve 2
  python ugc_modbus.py set --valve 3            # only write settings, don't start
  python ugc_modbus.py status                   # new-data flag (reading it resets it!) + valve
  python ugc_modbus.py peaks                    # read peaks of the last finished run
  python ugc_modbus.py run --run-type analysis --valve 1 --pretty
  python ugc_modbus.py watch                    # output peaks JSON after every run

Exit codes: 0 ok, 1 no new data, 2 timeout, 3 communication/config error.
"""

import argparse
import csv
import json
import struct
import sys
import time
from datetime import datetime, timezone

try:
    from pymodbus.client import ModbusSerialClient, ModbusTcpClient
except ImportError:
    sys.exit("pymodbus is required:  pip install pymodbus")

REG_SIZE = {"int16": 1, "uint16": 1, "int32": 2, "float": 2}
PRO_DATATYPE = {"bit": "0 Bit", "int16": "1 Int16", "uint16": "1 Int16",
                "int32": "2 Int32", "float": "3 Float"}
RUN_TYPES = {"analysis": 0, "calibration": 1, "blank": 2, "verification": 3}
RUN_TYPE_NAMES = {v: k for k, v in RUN_TYPES.items()}
PRO_REGTYPE = {"coil": "0 Coil Status", "discrete": "1 Input Status",
               "holding": "2 Holding Register", "input": "3 Input Register"}


class UGCError(Exception):
    pass


def decode(regs, dtype, word_order="normal"):
    if dtype == "int16":
        return struct.unpack(">h", struct.pack(">H", regs[0]))[0]
    if dtype == "uint16":
        return regs[0]
    if word_order == "reverse":           # PROstation "Reverse": 16-bit words swapped
        regs = regs[::-1]
    raw = struct.pack(">HH", *regs)
    return struct.unpack(">f" if dtype == "float" else ">i", raw)[0]


# --------------------------------------------------------------------------- #
# Device access
# --------------------------------------------------------------------------- #
class UGC:
    def __init__(self, cfg):
        self.cfg = cfg
        conn = cfg["connection"]
        self.unit = conn.get("unit_id", 1)
        self.word_order = cfg.get("float_word_order", "normal")
        self.offset = cfg.get("address_offset", 0)   # match PROstation "Shift Modbus addresses"
        kind = conn["type"].lower()
        if kind == "tcp":
            self.client = ModbusTcpClient(conn["host"], port=conn.get("port", 502),
                                          timeout=conn.get("timeout", 3))
        elif kind == "rtu":
            self.client = ModbusSerialClient(
                port=conn["serial_port"], baudrate=conn.get("baudrate", 9600),
                parity=conn.get("parity", "N"), stopbits=conn.get("stopbits", 1),
                bytesize=conn.get("bytesize", 8), timeout=conn.get("timeout", 3))
        else:
            raise UGCError(f"Unknown connection type '{conn['type']}' (use tcp or rtu)")

    def __enter__(self):
        if not self.client.connect():
            c = self.cfg["connection"]
            where = c.get("host") or c.get("serial_port")
            raise UGCError(f"Could not connect to the Micro GC at {where}")
        return self

    def __exit__(self, *exc):
        self.client.close()

    def _call(self, fn, address, **kw):
        address += self.offset
        try:                                       # pymodbus >= 3.9
            rr = fn(address, device_id=self.unit, **kw)
        except TypeError:                          # pymodbus < 3.9
            rr = fn(address, slave=self.unit, **kw)
        if rr is None or rr.isError():
            raise UGCError(f"Modbus error at register {address}: {rr}")
        return rr

    def read_bit(self, table, address):
        fn = self.client.read_coils if table == "coil" else self.client.read_discrete_inputs
        return bool(self._call(fn, address, count=1).bits[0])

    def read_regs(self, table, address, count):
        fn = (self.client.read_holding_registers if table == "holding"
              else self.client.read_input_registers)
        out = []
        while count > 0:                           # max 125 registers per request
            n = min(count, 120)
            out += self._call(fn, address, count=n).registers
            address += n
            count -= n
        return out

    def write_int16(self, point, value):
        if point.get("table", "holding") != "holding":
            raise UGCError("run settings must be holding registers")
        self._call(self.client.write_register, point["address"], value=int(value) & 0xFFFF)

    def read_point(self, point):
        if point["table"] in ("coil", "discrete"):
            return self.read_bit(point["table"], point["address"])
        return decode(self.read_regs(point["table"], point["address"], 1), "int16")

    # ---- operations ---------------------------------------------------------
    def apply_run_settings(self, run_type=None, cal_level=None, stream=None):
        """Write single-run settings (params 60, 61, 62). None = leave unchanged."""
        rs = self.cfg.get("run_settings", {})
        written = {}
        for key, val in (("run_type", run_type), ("calibration_level", cal_level),
                         ("stream_position", stream)):
            if val is None:
                continue
            if key not in rs:
                raise UGCError(f"'{key}' is not mapped in run_settings of the register map")
            self.write_int16(rs[key], val)
            written[key] = val
        if "run_type" in written:
            written["run_type"] = RUN_TYPE_NAMES[written["run_type"]]
        return written

    def read_run_info(self):
        """Stream actually used by the last run (2205) and whether the valve switched (2206)."""
        info = {}
        for key, point in self.cfg.get("run_info", {}).items():
            val = self.read_point(point)
            info[key] = bool(val) if point["table"] in ("coil", "discrete") else val
        return info

    def current_stream(self):
        point = self.cfg.get("run_settings", {}).get("current_stream_position")
        return self.read_point(point) if point else None

    def trigger_run(self):
        t = self.cfg["trigger"]
        if t["table"] == "coil":
            self._call(self.client.write_coil, t["address"], value=True)
        else:
            self._call(self.client.write_register, t["address"], value=1)

    def new_data_available(self):
        """Reads parameter 2201. NOTE: on the instrument this read resets the flag."""
        nd = self.cfg["new_data"]
        if nd["table"] in ("coil", "discrete"):
            return self.read_bit(nd["table"], nd["address"])
        return bool(self.read_regs(nd["table"], nd["address"], 1)[0])

    def read_all_peaks(self):
        p = self.cfg["peaks"]
        fields = p["fields"]
        words = sum(REG_SIZE[f["type"]] for f in fields)
        channels = []
        for ch in p["channels"]:
            count = decode(self.read_regs(p["table"], ch["count_address"], 1), "int16")
            n = max(0, min(count, p["max_peaks"]))
            peaks = []
            if n:
                regs = self.read_regs(p["table"], ch["base_address"], n * words)
                for i in range(n):
                    block, pos = regs[i * words:(i + 1) * words], 0
                    peak = {"peak": i + 1}
                    for f in fields:
                        size = REG_SIZE[f["type"]]
                        val = decode(block[pos:pos + size], f["type"], self.word_order)
                        # float32 carries ~7 significant digits; drop conversion noise
                        peak[f["name"]] = float(f"{val:.7g}") if isinstance(val, float) else val
                        pos += size
                    peaks.append(peak)
            entry = {"channel": ch["channel"], "name": ch.get("name", f"Channel {ch['channel']}"),
                     "peak_count": count, "peaks": peaks}
            if count > p["max_peaks"]:
                entry["truncated"] = True       # more peaks than registers mapped
            channels.append(entry)
        return channels


# --------------------------------------------------------------------------- #
# PROstation Modbus table generator
# --------------------------------------------------------------------------- #
def prostation_rows(cfg):
    rows = []
    t, nd, p = cfg["trigger"], cfg["new_data"], cfg["peaks"]
    rows.append((t["address"], t["table"], "bit", 0, 0, 0, "Start Run (Execute Cmd, MB)"))
    rows.append((nd["address"], nd["table"], "bit", 2201, 0, 0,
                 "Sync: Data available with reset (Bit, MB)"))
    run_params = {
        "run_type": (60, "Set Manual Run RunType (Int16, MB)"),
        "calibration_level": (61, "Set Manual Run Calib. Level (Int16, MB)"),
        "stream_position": (62, "Set Manual Run Stream Pos. (Int16, MB)"),
        "current_stream_position": (58, "Read Current Stream Position (Int16, MB)"),
    }
    info_params = {
        "stream_position": (2205, "Application: Stream Position, last run (Int16, MB)"),
        "stream_position_ok": (2206, "Application: Stream Position OK (Bit, MB)"),
    }
    for group, params in (("run_settings", run_params), ("run_info", info_params)):
        for key, point in cfg.get(group, {}).items():
            if key not in params:
                raise UGCError(f"unknown {group} entry '{key}'")
            pid, desc = params[key]
            dtype = "bit" if point["table"] in ("coil", "discrete") else "int16"
            rows.append((point["address"], point["table"], dtype, pid, 0, 0, desc))
    words = sum(REG_SIZE[f["type"]] for f in p["fields"])
    for ch in p["channels"]:
        c = ch["channel"]
        rows.append((ch["count_address"], p["table"], "int16", 1202, c, 0,
                     "Int.Rep.: Number of Peaks, Named + Unnamed"))
        for k in range(p["max_peaks"]):
            addr = ch["base_address"] + k * words
            for f in p["fields"]:
                rows.append((addr, p["table"], f["type"], f["parameter_id"], c, k + 1,
                             f.get("description", f["name"])))
                addr += REG_SIZE[f["type"]]
    return rows


def check_map(cfg):
    """Detect overlapping registers in the map (per register table)."""
    used = {}
    for addr, table, dtype, pid, ch, pk, _ in prostation_rows(cfg):
        key = "bits" if table in ("coil", "discrete") else "regs"
        key = f"{key}:{table}"
        for a in range(addr, addr + (1 if dtype == "bit" else REG_SIZE[dtype])):
            if (key, a) in used:
                raise UGCError(f"register map overlap at {table} {a} "
                               f"(param {pid} ch {ch} peak {pk} vs {used[(key, a)]})")
            used[(key, a)] = f"param {pid} ch {ch} peak {pk}"
    n = len(prostation_rows(cfg))
    if n > 1000:
        raise UGCError(f"map needs {n} PROstation rows; the instrument allows 1000. "
                       "Reduce max_peaks or channels.")
    return n


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(obj, args):
    text = json.dumps(obj, indent=2 if args.pretty else None)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    print(text, flush=True)


def log(msg):
    print(json.dumps({"timestamp": now(), **msg}), file=sys.stderr, flush=True)


def wait_for_data(ugc, timeout, poll):
    t0 = time.monotonic()
    while not ugc.new_data_available():
        if timeout and time.monotonic() - t0 > timeout:
            return False
        time.sleep(poll)
    return True


def result(ugc, settings=None):
    data = {"timestamp": now(), "new_data_available": True}
    run = dict(settings or {})
    info = ugc.read_run_info()                     # stream used + valve switched OK
    if info:
        run["last_run"] = info
    if run:
        data["run"] = run
    data["channels"] = ugc.read_all_peaks()
    return data


def run_settings_from_args(args):
    """Validate --run-type / --cal-level / --stream; return kwargs for apply_run_settings."""
    rt = RUN_TYPES[args.run_type] if args.run_type else None
    if args.cal_level is not None and not 1 <= args.cal_level <= 8:
        raise ValueError("--cal-level must be 1..8")
    if args.cal_level is not None and args.run_type not in ("calibration", None):
        raise ValueError("--cal-level only applies to --run-type calibration")
    if args.run_type == "calibration" and args.cal_level is None:
        args.cal_level = 1
    if args.stream is not None and not 1 <= args.stream <= 64:
        raise ValueError("--stream must be 1..64")
    return {"run_type": rt, "cal_level": args.cal_level, "stream": args.stream}


def start(ugc, args):
    settings = ugc.apply_run_settings(**run_settings_from_args(args))
    ugc.trigger_run()
    return settings


def cmd_table(cfg, args):
    n = check_map(cfg)
    w = csv.writer(sys.stdout)
    w.writerow(["Register", "Register type", "Data type", "Parameter ID", "Channel", "Peak#",
                "Description"])
    for addr, table, dtype, pid, ch, pk, desc in prostation_rows(cfg):
        w.writerow([addr, PRO_REGTYPE[table], PRO_DATATYPE[dtype], pid, ch, pk, desc])
    print(f"# {n} rows. PROstation process settings: protocol MODICON, Shift Modbus addresses "
          f"= No (or set address_offset to match), Float type = {cfg.get('float_word_order', 'normal')}.",
          file=sys.stderr)


def cmd_trigger(ugc, args):
    settings = start(ugc, args)
    emit({"timestamp": now(), "event": "run_triggered", "run": settings}, args)


def cmd_set(ugc, args):
    settings = ugc.apply_run_settings(**run_settings_from_args(args))
    if not settings:
        raise ValueError("give at least one of --run-type, --cal-level, --stream")
    emit({"timestamp": now(), "event": "run_settings_written", "run": settings}, args)


def cmd_status(ugc, args):
    out = {"timestamp": now(), "new_data_available": ugc.new_data_available()}
    stream = ugc.current_stream()
    if stream is not None:
        out["current_stream_position"] = stream
    emit(out, args)


def cmd_peaks(ugc, args):
    if args.wait is not None:
        if not wait_for_data(ugc, args.wait, args.poll):
            emit({"timestamp": now(), "new_data_available": False, "channels": []}, args)
            return 1
        emit(result(ugc), args)
    else:  # read last run's results without touching the flag
        data = result(ugc)
        data["new_data_available"] = None
        emit(data, args)


def cmd_run(ugc, args):
    run_settings_from_args(args)      # validate before touching the instrument
    ugc.new_data_available()          # clear any stale flag from a previous run
    settings = start(ugc, args)
    log({"event": "run_triggered", "run": settings})
    if not wait_for_data(ugc, args.timeout, args.poll):
        emit({"timestamp": now(), "error": "timeout", "new_data_available": False}, args)
        return 2
    emit(result(ugc, {"requested": settings} if settings else None), args)


def cmd_watch(ugc, args):
    log({"event": "watching"})
    while True:
        wait_for_data(ugc, 0, args.poll)
        emit(result(ugc), args)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agilent 990 Micro GC Modbus CLI")
    ap.add_argument("--config", default="register_map.json", help="register map JSON")
    ap.add_argument("--host", help="override TCP host")
    ap.add_argument("--port", type=int, help="override TCP port")
    ap.add_argument("--pretty", action="store_true", help="indent JSON output")
    ap.add_argument("--out", help="also write the JSON output to this file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("table", help="print the PROstation Modbus table (CSV) for this map")
    def run_opts(sp):
        g = sp.add_argument_group("run settings (omit to keep the instrument's current value)")
        g.add_argument("--run-type", choices=list(RUN_TYPES),
                       help="param 60: analysis, calibration, blank or verification")
        g.add_argument("--cal-level", type=int, metavar="N",
                       help="param 61: calibration level 1-8 (calibration runs; default 1)")
        g.add_argument("--stream", "--valve", type=int, metavar="POS", dest="stream",
                       help="param 62: stream selector valve position 1-64")

    run_opts(sub.add_parser("trigger", help="(optionally set run type/valve and) start a run"))
    run_opts(sub.add_parser("set", help="write run type / cal level / valve position only"))
    sub.add_parser("status", help="new data available? (reading resets the flag)")
    p = sub.add_parser("peaks", help="read peak information for all channels")
    p.add_argument("--wait", type=float, nargs="?", const=0,
                   help="wait for new data first (optional timeout s, 0 = forever)")
    p.add_argument("--poll", type=float, default=1.0)
    r = sub.add_parser("run", help="trigger, wait for new data, output peaks")
    r.add_argument("--timeout", type=float, default=900, help="seconds (0 = forever)")
    r.add_argument("--poll", type=float, default=1.0, help="poll interval, seconds")
    run_opts(r)
    w = sub.add_parser("watch", help="output peaks JSON every time a run finishes")
    w.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)

    try:
        with open(args.config) as f:
            cfg = json.load(f)
        if args.host:
            cfg["connection"]["host"] = args.host
        if args.port:
            cfg["connection"]["port"] = args.port
        if args.cmd == "table":
            return cmd_table(cfg, args) or 0
        check_map(cfg)
        handlers = {"trigger": cmd_trigger, "set": cmd_set, "status": cmd_status, "peaks": cmd_peaks,
                    "run": cmd_run, "watch": cmd_watch}
        with UGC(cfg) as ugc:
            return handlers[args.cmd](ugc, args) or 0
    except (UGCError, OSError, KeyError, ValueError) as e:
        log({"error": f"{type(e).__name__}: {e}"})
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
