# ugc_modbus — Agilent 990 Micro GC Modbus CLI

Triggers a run on an Agilent 990 Micro GC (PRO license, Modbus licensed) and returns the
new-data flag and the peak results of every channel as JSON.

## Install

```
pip install pymodbus
```

## One-time instrument setup (PROstation)

The 990 has no fixed Modbus map; registers are linked to parameter IDs in PROstation.

1. `python ugc_modbus.py table > prostation_modbus_table.csv` (a copy is included).
2. In PROstation, **Modbus tab → Registers setup**, enter those rows
   (register, register type, data type, parameter ID, channel, peak#) and download the table.
3. **Process settings**: protocol *MODICON*, *Shift Modbus addresses = No*,
   Float type *Normal*. Set *Reset Time New Data available flag* shorter than the run time
   but longer than the poll interval (default poll is 1 s).
4. Edit `register_map.json` → `connection` with the GC's IP (factory default 192.168.100.100)
   or serial port settings.

If values come back as nonsense (e.g. 1e-38), switch `float_word_order` to `"reverse"`.
If every read errors, the address shift probably differs: set `address_offset` to -1 or 1.

| Purpose | Parameter ID | Default register |
|---|---|---|
| Start Run | 0 | coil 100 |
| Run type (0 analysis, 1 calibration, 2 blank, 3 verification) | 60 | holding 200 |
| Calibration level (1–8) | 61 | holding 201 |
| Stream selector valve position (1–64) | 62 | holding 202 |
| Current valve position (read) | 58 | holding 203 |
| Stream used by last run (read) | 2205 | holding 204 |
| Valve switched OK for last run (read) | 2206 | input status 101 |
| New data available (resets when read) | 2201 | input status 100 |
| Number of peaks, per channel | 1202 | holding 1000–1003 |
| Retention time (min), area, height, amount, width per peak | 1219, 1203, 1204, 1205, 1207 | holding 1100+ (ch1), 1300+ (ch2), 1500+ (ch3), 1700+ (ch4) |

20 peaks per channel × 4 channels uses 412 of the 1000 rows PROstation allows.
Remove unused channels from `register_map.json` (and the table) on a 2‑channel unit.

## Commands

```
python ugc_modbus.py run --pretty            # trigger, wait for new data, print peaks JSON
python ugc_modbus.py trigger                 # just start a run (current settings)
python ugc_modbus.py run --run-type calibration --cal-level 1 --valve 2
python ugc_modbus.py trigger --run-type analysis --valve 3
python ugc_modbus.py set --valve 4           # write settings only, no run
python ugc_modbus.py status                  # new data? (reading resets the flag) + current valve
python ugc_modbus.py peaks                   # read last run's peaks now
python ugc_modbus.py peaks --wait 600        # wait (max 600 s) for new data, then read peaks
python ugc_modbus.py watch                   # print a JSON line after every finished run
```

Run settings (`trigger`, `run`, `set`): `--run-type analysis|calibration|blank|verification`,
`--cal-level 1-8` (calibration only; defaults to 1 for calibration runs),
`--valve N` / `--stream N` (stream selector position 1–64). Any option you leave out keeps
the instrument's current value. These set the 990's single-run settings, so they apply to
runs started this way, not to a running automation sequence.

Global options: `--config FILE`, `--host IP`, `--port N`, `--pretty`, `--out result.json`.
Exit codes: 0 ok · 1 no new data · 2 timeout · 3 communication/config error (JSON on stderr).

Note: parameter 2201 clears itself when read, so `status` consumes the flag. Use `run`,
`peaks --wait` or `watch` to wait for results; they read the peaks immediately after.

## Output

```json
{
  "timestamp": "2026-11-05T15:55:20+00:00",
  "new_data_available": true,
  "run": {
    "requested": {"run_type": "calibration", "calibration_level": 1, "stream_position": 2},
    "last_run": {"stream_position": 2, "stream_position_ok": true}
  },
  "channels": [
    {"channel": 1, "name": "Channel 1", "peak_count": 2, "peaks": [
      {"peak": 1, "retention_time_min": 0.21, "area": 1000.5, "height": 50.2, "amount": 1.2, "width": 0.8}
    ]}
  ]
}
```

`peak_count` is the instrument's total (named + unnamed); if it exceeds `max_peaks`,
the channel gets `"truncated": true`.
