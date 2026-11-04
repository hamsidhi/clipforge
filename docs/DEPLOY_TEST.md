# Deploy end-to-end test

Result: **PASS** · 2026-11-04 16:59 · Windows AMD64 · Python 3.12.0

Two real processes over HTTP: `clipforge ui` in **server** mode and `clipforge worker` in **worker** mode, each with its own workspace and `.env`; mock LLM, Whisper `tiny`, ultrafast render, fake YouTube.
Run it yourself: `python scripts/deploy_e2e.py --out docs/DEPLOY_TEST.md`.

```
[    0.0s] python 3.12.0 on win32; temp dir C:\Users\hamsi\AppData\Local\Temp\cf_deploy_e2e_ltvot3vu
[    2.3s] sample video: 78.8 MB
[    4.3s] server up (deployment.mode=server)
[    5.7s] worker key created on the server (only its SHA-256 is stored there)
[    8.1s] worker online: e2e-laptop caps=['download', 'transcribe', 'select', 'render']
[    8.2s] job 1 created on the server
[    8.2s] job: queued               
[   10.2s] job: running   transcribe transcribe: waiting for a worker (task 1)
[   18.2s] job: running   transcribe transcribe on e2e-laptop
[   20.2s] job: running   select     select: waiting for a worker (task 2)
[   30.3s] job: running   select     select on e2e-laptop
[   32.3s] job: running   render     render: waiting for a worker (task 3)
[   42.4s] job: running   render     render on e2e-laptop
[   46.4s] job: running   render     reframe: running — 2 clip(s)
[   52.4s] job: running   render     render: running — 2 clip(s)
[   56.4s] job: running   render     1/2 clips rendered
[   64.5s] job: done      done       2 clips ready
[   65.6s] stage tasks: [['transcribe', 'done', 'w_da07799b', 1], ['select', 'done', 'w_da07799b', 1], ['render', 'done', 'w_da07799b', 1]]
[   65.6s] 2 clips on the server: #1 38s rendered=True, #2 36s rendered=True
[   67.3s] publish: approved 2 clips (like tapping ✅ in Telegram)
[   67.3s] publish: queued 2 clips, scheduled 2
[   67.3s] publish: [16:59:41] INFO     Uploaded queue item 1 → yt1 (scheduled, slot               
[   67.3s] publish:                     2026-11-04T13:00:00+00:00)                                 
[   67.3s] publish:            INFO     Uploaded queue item 2 → yt2 (scheduled, slot               
[   67.3s] publish:                     2026-11-05T13:00:00+00:00)                                 
[   67.3s] publish: scheduler run: uploaded=2 failed=0 
[   67.3s] publish: every upload: privacyStatus=private + publishAt (nothing public before its slot)
[   67.3s] publish: after the slots: published=2
[   67.3s] PASS
--- server.log (last lines) ---
--- worker.log (last lines) ---
to the server).
[16:58:41] INFO     [worker] connected to Leaderhub_AD8 as e2e-laptop          
[16:58:52] INFO     [worker] transcribe: running                               
           INFO     [worker] Whisper model=tiny device=cpu compute=int8 (first 
[16:58:54] INFO     [worker] Transcribed 22 segments / 206 words (language=en) 
           INFO     [worker] transcribe: done                                  
[16:59:04] INFO     [worker] analyze: running — audio energy                   
           INFO     [worker] analyze: done — 2 energy peaks                    
           INFO     [worker] select: running — mock:mock                       
           INFO     [worker] select: done — 2 clips                            
[16:59:15] INFO     [worker] reframe: running — 2 clip(s)                      
[16:59:24] INFO     [worker] reframe: done                                     
           INFO     [worker] captions: running                                 
           INFO     [worker] captions: done — 2 subtitle files                 
           INFO     [worker] render: running — 2 clip(s)                       
[16:59:37] INFO     [worker] render: done — 2 clip(s)                          
```
