"""Deploy end-to-end test: a real SERVER process + a real WORKER process talking HTTP, like the Oracle box and the laptop.

    python scripts/deploy_e2e.py [--out docs/DEPLOY_TEST.md] [--keep]

1. synthetic 80-second "podcast" (test-pattern video + spoken English from espeak-ng or Windows SAPI)
2. `clipforge ui` in server mode (own workspace, own .env, mock LLM, Whisper tiny) on 127.0.0.1:8011
3. `clipforge server add-worker` → key → `clipforge worker` in worker mode (own workspace, own .env)
4. POST /api/jobs on the server → transcribe / select / render are leased to the worker, results flow back with
   sha256 checks → the job finishes on the server
5. fake publish: the server's scheduler uploads the clips to the fake YouTube (tests/fake_youtube.py): private + publishAt
Writes a timestamped log (and with --out a Markdown report). Exit code 0 = pass.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PORT = 8011
LOG: list[str] = []
T0 = time.time()
SCRIPT = ("ClipForge turns long podcasts into short vertical clips. The first idea is simple. Most people never start "
          "because they wait for the perfect moment. The perfect moment never comes. Start small and keep going. "
          "The second idea is about money. Save a little every month and let time do the hard work. "
          "Compounding is boring for years and then suddenly it is amazing. The third idea is about health. "
          "Sleep well, walk every day and drink water. Your body is the only place you really have to live. "
          "That is the whole episode. Thank you for listening and see you next week.")


def say(msg: str) -> None:
    line = f"[{time.time() - T0:7.1f}s] {msg}"
    print(line, flush=True)
    LOG.append(line)


def speech(wav: Path) -> None:
    if shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-s", "150", "-w", str(wav), SCRIPT * 2], check=True)
    elif os.name == "nt":
        ps = (f"Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.Rate=0; $s.SetOutputToWaveFile('{wav}'); $s.Speak(\"{SCRIPT} {SCRIPT}\"); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    else:
        raise SystemExit("Need espeak-ng (apt install espeak-ng) for the spoken sample")


def make_sample(dst: Path) -> Path:
    from clipforge.utils.ffmpeg import run_ffmpeg

    wav = dst.with_suffix(".wav")
    speech(wav)
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-i", str(wav), "-map", "0:v", "-map", "1:a",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst)])
    return dst


def write_cfg(path: Path, ws: Path, mode: str, extra: str = "") -> None:
    path.write_text(f"""output: {{dir: '{ws.as_posix()}'}}
llm: {{provider: mock}}
whisper: {{model: tiny, language: en}}
clips: {{count: 2, min_sec: 15, max_sec: 40}}
render: {{preset: ultrafast, workers: 1}}
telegram: {{enabled: false}}
remote: {{preview_proxy: true}}
deployment: {{mode: {mode}, worker_grace_minutes: 30, heartbeat_seconds: 5, lease_seconds: 60{extra}}}
""", encoding="utf-8")


def env_for(cfg: Path, envfile: Path) -> dict:
    e = {**os.environ, "CLIPFORGE_CONFIG": str(cfg), "CLIPFORGE_ENV": str(envfile), "PYTHONUTF8": "1"}
    e.pop("CLIPFORGE_TESTING", None)
    return e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="cf_deploy_e2e_"))
    procs: list[subprocess.Popen] = []
    ok = False
    try:
        srv_cfg, wrk_cfg = tmp / "server.yaml", tmp / "worker.yaml"
        srv_env, wrk_env = tmp / "server.env", tmp / "worker.env"
        srv_env.write_text("")
        write_cfg(srv_cfg, tmp / "server_ws", "server")
        say(f"python {sys.version.split()[0]} on {sys.platform}; temp dir {tmp}")
        sample = make_sample(tmp / "sample.mp4")
        say(f"sample video: {sample.stat().st_size / 1e6:.1f} MB")
        senv = env_for(srv_cfg, srv_env)
        srv_log = open(tmp / "server.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen([PY, "-m", "clipforge", "ui", "--host", "127.0.0.1", "--port", str(PORT), "--no-browser"],
                                      cwd=ROOT, env=senv, stdout=srv_log, stderr=subprocess.STDOUT))
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(120):
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        say("server up (deployment.mode=server)")
        key = subprocess.run([PY, "-m", "clipforge", "server", "add-worker", "e2e-laptop", "--key-only"], cwd=ROOT, env=senv,
                             capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
        assert key.startswith("cfw_")
        say("worker key created on the server (only its SHA-256 is stored there)")
        write_cfg(wrk_cfg, tmp / "worker_ws", "worker", f", server_url: '{base}'")
        wrk_env.write_text(f"CLIPFORGE_WORKER_KEY={key}\n")
        wrk_log = open(tmp / "worker.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen([PY, "-m", "clipforge", "worker"], cwd=ROOT, env=env_for(wrk_cfg, wrk_env),
                                      stdout=wrk_log, stderr=subprocess.STDOUT))
        c = httpx.Client(base_url=base, timeout=30)
        for _ in range(60):
            ws_ = c.get("/api/server/workers").json()
            if ws_ and ws_[0]["online"]:
                break
            time.sleep(1)
        say(f"worker online: {ws_[0]['name']} caps={ws_[0]['capabilities']}")
        job = c.post("/api/jobs", json={"url": str(sample), "settings": {"clips": 2}}).json()
        say(f"job {job['id']} created on the server")
        seen: set = set()
        deadline = time.time() + 40 * 60
        while time.time() < deadline:
            j = c.get(f"/api/jobs/{job['id']}").json()
            ev = (j["status"], j["stage"], j.get("message", "")[:70])
            if ev not in seen:
                seen.add(ev)
                say(f"job: {j['status']:9} {j['stage']:10} {ev[2]}")
            if j["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(2)
        assert j["status"] == "done", f"job ended {j['status']}: {j.get('error')}"
        tasks = json.loads(subprocess.run([PY, "-c", "import json;from clipforge.server import leases;from clipforge.api import db;"
                                           "print(json.dumps([[t.stage,t.status,t.worker_id,t.fence] for t in leases.tasks_for(%d)]))" % job["id"]],
                                          cwd=ROOT, env=senv, capture_output=True, text=True, check=True).stdout)
        say(f"stage tasks: {tasks}")
        assert {t[0] for t in tasks if t[1] == "done"} >= {"transcribe", "select", "render"}, "heavy stages didn't run on the worker"
        clips = c.get(f"/api/jobs/{job['id']}/clips").json()
        say(f"{len(clips)} clips on the server: " + ", ".join(f"#{x['rank']} {x['duration']:.0f}s rendered={x['rendered']}" for x in clips))
        assert clips and all(x["rendered"] for x in clips)
        pub = subprocess.run([PY, str(ROOT / "scripts" / "deploy_e2e_publish.py"), str(job["id"])], cwd=ROOT, env=senv,
                             capture_output=True, text=True)
        for line in pub.stdout.strip().splitlines():
            say(f"publish: {line}")
        assert pub.returncode == 0, pub.stderr[-800:]
        ok = True
        say("PASS")
    except Exception as exc:
        say(f"FAIL: {exc}")
    finally:
        for p in reversed(procs):
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
        for name in ("server.log", "worker.log"):
            f = tmp / name
            if f.exists():
                tail = [ln for ln in f.read_text(encoding="utf-8", errors="replace").splitlines() if "[worker]" in ln or "server" in ln][-25:]
                LOG.append(f"--- {name} (last lines) ---")
                LOG.extend(tail)
        if args.out:
            args.out.write_text(report(ok), encoding="utf-8")
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def report(ok: bool) -> str:
    import platform

    return (f"# Deploy end-to-end test\n\nResult: **{'PASS' if ok else 'FAIL'}** · {time.strftime('%Y-%m-%d %H:%M')} · "
            f"{platform.system()} {platform.machine()} · Python {platform.python_version()}\n\n"
            "Two real processes over HTTP: `clipforge ui` in **server** mode and `clipforge worker` in **worker** mode, "
            "each with its own workspace and `.env`; mock LLM, Whisper `tiny`, ultrafast render, fake YouTube.\n"
            "Run it yourself: `python scripts/deploy_e2e.py --out docs/DEPLOY_TEST.md`.\n\n```\n" + "\n".join(LOG) + "\n```\n"
            + NOT_COVERED)


NOT_COVERED = (
    "\n**Not covered by this run:** Linux/Arm64 (run `.github/workflows/deploy-test.yml` from the Actions tab — the same "
    "test on `ubuntu-24.04-arm`), `deploy/install.sh` on a real VM (only `bash -n` / shellcheck), real Tailscale/SSH, real "
    "Whisper `large-v3-turbo` / Ollama speeds on Ampere (estimates in deploy/README.md). Failure drills (worker killed "
    "mid-render, server restart mid-job, laptop sleeping through its lease, …): DECISIONS.md → reliability drills and "
    "tests/test_reliability.py.\n")


if __name__ == "__main__":
    sys.exit(main())
