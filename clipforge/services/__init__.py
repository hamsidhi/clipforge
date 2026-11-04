"""Shared service layer (Phase 15): the ONE implementation of every user action.

The web API (`clipforge/api/*_routes.py`), the Telegram bot (`clipforge/notify/`) and the CLI all call these functions,
so an edit made from the phone goes through exactly the same checks, locks and pipeline code as one made in the web app.

Errors are `ServiceError` (an HTTPException subclass, so FastAPI routes need no translation; Telegram and the CLI read
`.detail`, which is always a human sentence).
"""
