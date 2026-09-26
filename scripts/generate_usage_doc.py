"""Generate docs/usage.md from src/nextdns_mcp/usage.py.

SPDX-License-Identifier: MIT
"""

import sys
from pathlib import Path

from nextdns_mcp.usage import nextdns_usage_guide

HEADER = "<!-- Generated from src/nextdns_mcp/usage.py via scripts/generate_usage_doc.py. Do not edit directly. -->\n\n"


def generate_usage_doc() -> str:
    """Generate the full content of docs/usage.md."""
    return f"{HEADER}{nextdns_usage_guide().strip()}\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point to generate or verify docs/usage.md."""
    if argv is None:
        argv = sys.argv[1:]

    repo_root = Path(__file__).resolve().parents[1]
    target = repo_root / "docs" / "usage.md"

    content = generate_usage_doc()

    if "--check" in argv:
        if not target.exists():
            print(f"Error: {target} does not exist", file=sys.stderr)
            return 1
        current = target.read_text(encoding="utf-8")
        if current != content:
            print(
                f"Error: {target} is out of date. Run 'python scripts/generate_usage_doc.py' to update.",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {target} is up to date.")
        return 0

    target.write_text(content, encoding="utf-8")
    print(f"Updated {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
