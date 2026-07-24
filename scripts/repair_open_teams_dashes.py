"""Finish repairing mojibake sequences that contain invalid cp1252 controls."""

from pathlib import Path


path = Path("leaguebot/open_teams_ui.py")
text = path.read_text(encoding="utf-8")
text = text.replace("\xc3\xa2\xe2\u201a\xac\xe2\u20ac\x9d", "—")
text = text.replace("\xc3\u201a\xc2\xb7", "·")
path.write_text(text, encoding="utf-8")
