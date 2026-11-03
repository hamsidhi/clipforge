# Things only you can test (need your accounts, devices or hardware)

Everything else was run on this laptop during the build. These steps need your accounts, your phone,
another network device, or hardware that isn't here.

## 1. YouTube upload (Phase 10): needs your Google account
The code is unit-tested with a fake Google client, but a real upload needs your OAuth consent.
1. Follow **README → 5. YouTube upload** to create `client_secret.json` (free, about 5 minutes).
2. Set `upload.enabled: true` in `config.yaml`.
3. Run `clipforge upload <video_id> --only 1`. A browser opens: sign in, then allow.
4. Check YouTube Studio: the Short should be **Private**, scheduled about 1 hour ahead.

## 2. Phone over Wi-Fi
```powershell
clipforge ui --host 0.0.0.0
```
- The first time, Windows Defender Firewall asks whether to allow Python on **Private networks**. Click **Allow**.
- Open the printed `http://192.168.x.x:8000` address on your phone. It must be on the same Wi-Fi.
- If it doesn't load, set the network to Private: Settings → Network → your Wi-Fi → *Private network*.

## 3. Gemini / Groq free tiers: need your free API keys
Both providers are implemented (REST, rate-limit back-off), but neither was called without a key.
Add a key in **Settings** (or `.env`), then run a short test:
```powershell
clipforge run "<url>" --section 10:00-20:00 --provider gemini --clips 5
```

## 4. Colab notebook
`notebooks/clipforge_colab.ipynb` clones this repo, installs the pinned requirements and runs on a T4.
Open it in Colab (File → Upload notebook, or open it from GitHub), choose *Runtime → T4 GPU*, and run the cells in order.
If a pinned package fails to install on Colab's Python, run `!pip install -U <that package>` and run the cell again.

## 5. NVIDIA GPU path
This laptop has an AMD iGPU, so the CUDA (Whisper `large-v3` float16) and NVENC code paths were never run here.
On an NVIDIA machine, `clipforge doctor` should show both green. If CUDA Whisper fails to load, ClipForge
falls back to the CPU automatically and logs a warning.
