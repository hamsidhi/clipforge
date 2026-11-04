# Deploy end-to-end test

Result: **PASS** · 2026-11-04 17:25 · Windows AMD64 · Python 3.12.0

Two real processes over HTTP: `clipforge ui` in **server** mode and `clipforge worker` in **worker** mode, each with its own workspace and `.env`; mock LLM, Whisper `tiny`, ultrafast render, fake YouTube.
Run it yourself: `python scripts/deploy_e2e.py --out docs/DEPLOY_TEST.md`.

```
[    0.0s] python 3.12.0 on win32; temp dir C:\Users\hamsi\AppData\Local\Temp\cf_deploy_e2e_m922kfbk
[    2.2s] sample video: 78.6 MB
[    4.2s] server up (deployment.mode=server)
[    5.5s] worker key created on the server (only its SHA-256 is stored there)
[    7.9s] worker online: e2e-laptop caps=['download', 'transcribe', 'select', 'render']
[    7.9s] job 1 created on the server
[    7.9s] job: running   download   C:\Users\hamsi\AppData\Local\Temp\cf_deploy_e2e_m922kfbk\sample.mp4
[    9.9s] job: running   transcribe transcribe: waiting for a worker (task 1)
[   18.0s] job: running   transcribe transcribe on e2e-laptop
[   20.0s] job: running   select     select: waiting for a worker (task 2)
[   30.0s] job: running   render     render: waiting for a worker (task 3)
[   40.1s] job: running   render     render on e2e-laptop
[   46.1s] job: running   render     reframe: running — 2 clip(s)
[   52.2s] job: running   render     clip 1: blur
[   56.2s] job: running   render     0/2 clips rendered
[   62.2s] job: running   render     1/2 clips rendered
[   66.3s] job: done      done       2 clips ready
[   67.2s] stage tasks: [['transcribe', 'done', 'w_5d6751b1', 1], ['select', 'done', 'w_5d6751b1', 1], ['render', 'done', 'w_5d6751b1', 1]]
[   67.3s] 2 clips on the server: #1 38s rendered=True, #2 36s rendered=True
[   69.0s] publish: approved 2 clips (like tapping ✅ in Telegram)
[   69.0s] publish: queued 2 clips, scheduled 2
[   69.0s] publish: [17:25:06] INFO     Uploaded queue item 1 → yt1 (scheduled, slot               
[   69.0s] publish:                     2026-11-04T13:00:00+00:00)                                 
[   69.0s] publish:            INFO     Uploaded queue item 2 → yt2 (scheduled, slot               
[   69.0s] publish:                     2026-11-05T13:00:00+00:00)                                 
[   69.0s] publish: scheduler run: uploaded=2 failed=0 
[   69.0s] publish: every upload: privacyStatus=private + publishAt (nothing public before its slot)
[   69.0s] publish: after the slots: published=2
[   69.0s] PASS
--- server.log (last lines) ---
--- worker.log (last lines) ---
to the server).
[17:24:04] INFO     [worker] connected to Leaderhub_AD8 as e2e-laptop          
[17:24:15] INFO     [worker] transcribe: running                               
           INFO     [worker] Whisper model=tiny device=cpu compute=int8 (first 
[17:24:17] INFO     [worker] Transcribed 22 segments / 206 words (language=en) 
           INFO     [worker] transcribe: done                                  
[17:24:27] INFO     [worker] analyze: running — audio energy                   
           INFO     [worker] analyze: done — 2 energy peaks                    
           INFO     [worker] select: running — mock:mock                       
           INFO     [worker] select: done — 2 clips                            
[17:24:38] INFO     [worker] reframe: running — 2 clip(s)                      
[17:24:48] INFO     [worker] reframe: done                                     
           INFO     [worker] captions: running                                 
           INFO     [worker] captions: done — 2 subtitle files                 
           INFO     [worker] render: running — 2 clip(s)                       
[17:25:01] INFO     [worker] render: done — 2 clip(s)                          
```

**Not covered by this run:** Linux/Arm64 (run `.github/workflows/deploy-test.yml` from the Actions tab — the same test on `ubuntu-24.04-arm`), `deploy/install.sh` on a real VM (only `bash -n` / shellcheck), real Tailscale/SSH, real Whisper `large-v3-turbo` / Ollama speeds on Ampere (estimates in deploy/README.md). Failure drills (worker killed mid-render, server restart mid-job, laptop sleeping through its lease, …): DECISIONS.md → reliability drills and tests/test_reliability.py.
