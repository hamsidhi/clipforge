"""Phase 15b: always-on server + laptop worker (deployment.mode: standalone | server | worker).

status.py (uptime/disk/RAM/CPU for /server), leases.py (stage tasks with leases + fencing), worker_api.py (HTTP
endpoints for workers), worker.py (the laptop loop), artifacts.py (chunked sha256 transfers), handover.py, backup.py,
housekeeping.py. Default `standalone` keeps everything exactly as before.
"""
