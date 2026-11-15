"""Live gaming mode (2026-11-15): highlight clips WHILE you stream, on the same PC, without hurting the game or stream.

Footage: OBS Replay Buffer (`obs.py`, no extra encode) or the live HLS stream (`source.py`).
Signals: manual marks, OBS volume meters (`audio.py`), chat (`chat.py`), HUD screenshots (`hud.py`, `games.py`).
`scorer.py` turns signals into moments; `session.py` saves → cuts → renders (`render.py`, iGPU AMF) → Telegram/posting.
`governor.py` pauses everything when the CPU or OBS struggles. `simulate.py` replays a file as if it were live.
Decisions: DECISIONS.md → "Live gaming". Setup: HANDOFF_LIVE.md.
"""
