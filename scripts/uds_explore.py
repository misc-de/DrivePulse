#!/usr/bin/env python3
"""Read-only UDS explorer for collecting control-module data.

This is a *learning / collecting* tool, not part of the running app. It talks to
one CAN control module at a time (instrument cluster, comfort module, …) and
reads identifiers via UDS ReadDataByIdentifier (0x22). It never writes anything.

The raw adapter reply is always printed alongside the decoded value so you can
see exactly what the firmware returns and learn the format. Positive results can
be appended to a JSONL file to build up a per-module data set over time — the
raw material for a later coding table.

Examples
--------
Read the standardised identification DIDs from the engine ECU::

    python scripts/uds_explore.py --tx 7E0 --rx 7E8 --ident

Read a single DID from a body/comfort module (note non-standard rx id), entering
an extended session first, logging hits to a file::

    python scripts/uds_explore.py --tx 714 --rx 77E --session --did F190 \
        --out collected/comfort.jsonl

Sweep a DID range (slow — use a small range while learning)::

    python scripts/uds_explore.py --tx 7E0 --rx 7E8 --range 0200 0220

Safety: only services 0x10 (session) and 0x22 (read) are ever sent. There is no
write path here. Coding/writing is a deliberate later step.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivepulse_app.obd.adapter import probe_adapter, raw_send  # noqa: E402
from drivepulse_app.obd.uds import (  # noqa: E402
    IDENTIFICATION_DIDS,
    VAG_MODULES,
    UdsClient,
    UdsError,
    UdsResponse,
    as_ascii,
    did_payload,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/rfcomm0", help="serial device (default: /dev/rfcomm0)")
    p.add_argument("--baud", type=int, default=38400, help="baud rate (default: 38400)")
    p.add_argument("--tx", default="7E0", help="module request CAN id, hex (default: 7E0)")
    p.add_argument("--rx", default="7E8", help="module response CAN id, hex (default: 7E8)")
    p.add_argument("--vag-module", choices=sorted(VAG_MODULES), metavar="NAME",
                   help="VAG (Audi/VW) module preset, overrides --tx/--rx: " + ", ".join(sorted(VAG_MODULES)))
    p.add_argument("--protocol", default="6", help="ELM CAN protocol: 6=11bit/500k, 7=29bit (default: 6)")
    p.add_argument("--session", action="store_true", help="enter extended session (10 03) before reading")
    p.add_argument("--did", action="append", default=[], metavar="HEX", help="read one DID (repeatable), e.g. F190")
    p.add_argument("--ident", action="store_true", help="read the standardised identification DIDs (0xF1xx)")
    p.add_argument("--range", nargs=2, metavar=("START", "END"), help="sweep DID range inclusive, hex")
    p.add_argument("--delay", type=float, default=0.05, help="pause between DIDs in seconds (default: 0.05)")
    p.add_argument("--out", help="append positive hits as JSONL to this file")
    return p.parse_args(argv)


def _dids_to_read(args: argparse.Namespace) -> list[int]:
    dids: list[int] = []
    if args.ident:
        dids.extend(IDENTIFICATION_DIDS)
    for d in args.did:
        dids.append(int(d, 16))
    if args.range:
        start, end = (int(x, 16) for x in args.range)
        dids.extend(range(start, end + 1))
    # De-duplicate while preserving order.
    seen: set[int] = set()
    unique: list[int] = []
    for d in dids:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def _describe(did: int, response: UdsResponse) -> dict[str, Any]:
    label = IDENTIFICATION_DIDS.get(did, "")
    row: dict[str, Any] = {"did": f"{did:04X}", "label": label}
    if response.positive:
        payload = did_payload(response, did)
        if payload is None:
            row["status"] = "unexpected"
            row["raw"] = response.data.hex().upper()
        else:
            row["status"] = "ok"
            row["hex"] = payload.hex().upper()
            ascii_val = as_ascii(payload)
            if ascii_val is not None:
                row["ascii"] = ascii_val
    else:
        assert response.negative is not None
        row["status"] = "negative"
        row["nrc"] = f"{response.negative.nrc:02X}"
        row["nrc_name"] = response.negative.name
    return row


def _print_row(row: dict[str, Any]) -> None:
    head = f"  DID {row['did']}"
    if row["label"]:
        head += f" [{row['label']}]"
    if row["status"] == "ok":
        line = f"{head}: {row['hex']}"
        if "ascii" in row:
            line += f"   \"{row['ascii']}\""
        print(line)
    elif row["status"] == "negative":
        # Skip the noisy "not supported" codes unless they're a labelled DID.
        if row["nrc"] in {"11", "31", "7F"} and not row["label"]:
            return
        print(f"{head}: -- {row['nrc_name']} (0x{row['nrc']})")
    else:
        print(f"{head}: ? unexpected {row.get('raw', '')}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.vag_module:
        args.tx, args.rx = VAG_MODULES[args.vag_module]
    dids = _dids_to_read(args)
    if not dids:
        print("Nothing to read. Use --ident, --did HEX or --range START END.", file=sys.stderr)
        return 2

    try:
        import serial  # pyserial, pulled in by python-obd
    except ImportError:
        print("pyserial not available — install python-obd / pyserial.", file=sys.stderr)
        return 1

    try:
        port = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as exc:  # noqa: BLE001 - surface any open failure to the user
        print(f"Could not open {args.port}: {exc}", file=sys.stderr)
        return 1

    def send(cmd: str) -> str:
        return raw_send(port, cmd)

    try:
        send("ATZ")
        time.sleep(0.3)
        info = probe_adapter(connection=None, locked_raw=send)
        print(f"Adapter: {info.kind.value} {info.version}".strip())
        if not info.supports_stpx:
            print("  (note: a genuine STN/OBDLink adapter handles ISO-TP far more reliably)")

        client = UdsClient(send)
        client.open(args.tx, args.rx, protocol=args.protocol)
        print(f"Module tx={args.tx.upper()} rx={args.rx.upper()} protocol={args.protocol}")

        if args.session:
            try:
                sess = client.enter_session(0x03)
                state = "ok" if sess.positive else str(sess.negative)
                print(f"Extended session: {state}")
            except UdsError as exc:
                print(f"Extended session: failed ({exc})")

        out_fh = open(args.out, "a", encoding="utf-8") if args.out else None
        hits = 0
        try:
            for did, response in client.scan_dids(dids):
                row = _describe(did, response)
                _print_row(row)
                if out_fh is not None and row["status"] == "ok":
                    hits += 1
                    out_fh.write(json.dumps({
                        "timestamp": datetime.now(UTC).isoformat(),
                        "tx": args.tx.upper(),
                        "rx": args.rx.upper(),
                        **row,
                    }) + "\n")
                if args.delay:
                    time.sleep(args.delay)
        finally:
            if out_fh is not None:
                out_fh.close()
                print(f"Wrote {hits} hits to {args.out}")

        client.close()
    finally:
        port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
