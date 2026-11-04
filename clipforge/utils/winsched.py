"""Windows Task Scheduler tasks defined as XML (one task can have several triggers, run hidden, start when
available after a missed run, and run on battery). Used by `clipforge schedule-task` and `clipforge service`."""

from __future__ import annotations

import datetime as dt
import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

WAKE_EVENT = ("&lt;QueryList&gt;&lt;Query Id=\"0\" Path=\"System\"&gt;&lt;Select Path=\"System\"&gt;"
              "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]"
              "&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;")


def pythonw() -> str:
    """pythonw.exe of this venv (no console window); falls back to python.exe."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def user_id() -> str:
    dom = os.environ.get("USERDOMAIN")
    return f"{dom}\\{getpass.getuser()}" if dom else getpass.getuser()


def task_xml(description: str, command: str, arguments: str, workdir: str, *, logon: bool = True,
             every_minutes: int | None = None, on_wake: bool = False, restart_on_failure: bool = False,
             time_limit: str = "PT2H", start: dt.datetime | None = None) -> str:
    """Build a Task Scheduler 1.2 XML definition."""
    uid = escape(user_id())
    trig = []
    if logon:
        trig.append(f"<LogonTrigger><Enabled>true</Enabled><UserId>{uid}</UserId><Delay>PT30S</Delay></LogonTrigger>")
    if every_minutes:
        sb = (start or dt.datetime.now()).replace(second=0, microsecond=0).isoformat()
        trig.append(f"<TimeTrigger><Repetition><Interval>PT{every_minutes}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd>"
                    f"</Repetition><StartBoundary>{sb}</StartBoundary><Enabled>true</Enabled></TimeTrigger>")
    if on_wake:
        trig.append(f"<EventTrigger><Enabled>true</Enabled><Subscription>{WAKE_EVENT}</Subscription><Delay>PT1M</Delay></EventTrigger>")
    restart = ("<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>" if restart_on_failure else "")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description><Author>ClipForge</Author></RegistrationInfo>
  <Triggers>{''.join(trig)}</Triggers>
  <Principals><Principal id="Author"><UserId>{uid}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>{restart}
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def install(name: str, xml: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Task Scheduler is Windows-only."
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(xml, encoding="utf-16")
        res = subprocess.run(["schtasks", "/Create", "/F", "/TN", name, "/XML", path], capture_output=True, text=True)
        msg = (res.stdout or res.stderr).strip()
        if res.returncode != 0 and "denied" in msg.lower():
            msg += " — run the command once from an Administrator terminal (Task Scheduler sometimes needs it for logon triggers)."
        return res.returncode == 0, msg
    finally:
        Path(path).unlink(missing_ok=True)


def remove(name: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Task Scheduler is Windows-only."
    res = subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True, text=True)
    return res.returncode == 0, (res.stdout or res.stderr).strip()


def query(name: str) -> dict | None:
    """{'name', 'status', 'last_run', 'last_result', 'next_run'} or None if the task doesn't exist."""
    if sys.platform != "win32":
        return None
    res = subprocess.run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"], capture_output=True, text=True)
    if res.returncode != 0:
        return None
    info: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info.setdefault(k.strip(), v.strip())
    return {"name": name, "status": info.get("Status", ""), "last_run": info.get("Last Run Time", ""),
            "last_result": info.get("Last Result", ""), "next_run": info.get("Next Run Time", "")}
