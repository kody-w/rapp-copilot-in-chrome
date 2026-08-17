#!/usr/bin/env python3
"""Build and verify the local Voice Twin as a RAPP/1 rapplication egg."""

import argparse
import json
from pathlib import Path

import rapp1
import voice_twin

CONFIG_FILE = Path.home() / ".rappter-chrome" / "config.json"


def build(cfg, output=None, created_utc=None):
    output = Path(output or (voice_twin.TWIN_ROOT / "voice-twin.rapp.egg"))
    with voice_twin.twin_lock():
        rappid = voice_twin.ensure_identity(cfg)
        files = {
            "agent.py": voice_twin.AGENT_FILE.read_bytes(),
            "rappid.json": voice_twin.IDENTITY_FILE.read_bytes(),
            "state/conformance.json": voice_twin.CONFORMANCE_FILE.read_bytes(),
        }
        blob = rapp1.pack_egg(
            "rapplication",
            rappid,
            created_utc or voice_twin._utc(),
            files=files,
            payload={},
        )
        ok, step, reason = rapp1.verify_egg(blob)
        if not ok:
            raise RuntimeError(f"Voice Twin egg failed {step}: {reason}")
        manifest, unpacked = rapp1.read_egg(blob)
        if (
            manifest.get("rappid") != rappid
            or set(unpacked) != set(files)
            or unpacked.get("agent.py") != files["agent.py"]
        ):
            raise RuntimeError("Voice Twin egg did not round-trip exactly")
        voice_twin._write_bytes_atomic(output, blob)
    return {
        "path": str(output),
        "rappid": rappid,
        "egg_hash": rapp1.egg_address(manifest),
        "status": "structural-pre-acceptance",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    print(json.dumps(build(cfg, output=args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
