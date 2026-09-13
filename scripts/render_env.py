"""Print the environment block to paste into Render (Environment -> Add from .env).

    python scripts/render_env.py

Reads your local .env and the local demo files, and prints one KEY=value per line - the demo post and
contacts as single-line JSON, because they hold real test addresses and never go into the repo. The
output contains secrets: paste it into Render and nowhere else.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKIP = {"WARMPATH_DB", "COMPOSIO_TOOL_VERSION"}


def main() -> None:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    env.update({"WARMPATH_MODE": "live", "WARMPATH_HEARTBEAT_S": "5", "WARMPATH_AUTOPILOT_RUN": "1"})
    env.setdefault("WARMPATH_ACCESS_KEY", secrets.token_urlsafe(18))
    for name, path in (("WARMPATH_DEMO_POST_JSON", ROOT / "fixtures" / "demo_post.local.json"),
                       ("WARMPATH_CONTACTS_JSON", ROOT / "contacts.local.json")):
        if path.exists():
            env[name] = json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, separators=(",", ":"))
    for k, v in env.items():
        if k not in SKIP and v:
            print(f"{k}={v}")
    print(f"\n# Your console access key: {env['WARMPATH_ACCESS_KEY']}", file=sys.stderr)


if __name__ == "__main__":
    main()
