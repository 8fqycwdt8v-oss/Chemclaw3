"""`python -m chemclaw.rooms` — start the Teams bridge (off unless an operator enables it)."""

from chemclaw.rooms.serve import main

if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
