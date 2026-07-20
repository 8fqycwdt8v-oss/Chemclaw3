"""CLI: validate the ELN export's reactions — RDKit structure + mass balance (plan 4.4).

A thin shim so the plan's named entry point exists; the logic lives in `eln.validate`.
Run as `python -m scripts.validate_ord [export_dir]` (or `make eln-validate`).
"""

from eln.validate import main

if __name__ == "__main__":
    raise SystemExit(main())
