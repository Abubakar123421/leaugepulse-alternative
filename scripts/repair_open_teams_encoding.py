"""Repair legacy double-encoded UTF-8 text in the Open Teams module."""

from __future__ import annotations

from pathlib import Path


path = Path("leaguebot/open_teams_ui.py")
lines = path.read_text(encoding="utf-8").splitlines()
repaired: list[str] = []
for line in lines:
    for _ in range(4):
        try:
            candidate = line.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == line:
            break
        line = candidate
    repaired.append(line)
path.write_text("\n".join(repaired) + "\n", encoding="utf-8")
