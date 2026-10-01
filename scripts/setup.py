"""One-time initialization for the Nexoryn Agent CLI.

Run:
    python scripts/setup.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Windows consoles often default to cp1252, which can't encode the ✓/✖
# characters used below.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    env_path = PROJECT_ROOT / ".env"
    env_example_path = PROJECT_ROOT / ".env.example"

    print("Nexoryn Agent setup\n")

    if env_path.exists():
        print(f"✓ .env already exists at {env_path}")
    else:
        print(f"✖ No .env found at {env_path}")
        shutil.copy(env_example_path, env_path)
        print("  Created one from .env.example -- open it and fill in your real values:")
        print(f"    {env_path}")
        print("  Then re-run this script.")
        return

    try:
        load_config()
    except RuntimeError as exc:
        print(f"✖ {exc}")
        return
    print("✓ .env has both required variables set")

    db_path = PROJECT_ROOT / "nexoryn_agent.db"
    init_db(str(db_path))
    print(f"✓ Database ready at {db_path} (posts, comments, messages, summaries tables)")

    print("\nSetup complete. Next steps:")
    print("  python app/cli.py summary")
    print("  python app/cli.py summary --output json")
    print("  python app/cli.py status")


if __name__ == "__main__":
    main()
