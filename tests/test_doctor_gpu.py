"""`clipforge doctor` line: does Ollama use a GPU (and the iGPU hint)."""

import httpx

from clipforge import doctor

DROPPED = """time=1 level=INFO msg="server config" env="map[OLLAMA_VULKAN:true]"
time=2 level=INFO msg="dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1" id=0 library=Vulkan name=Vulkan0 description="AMD Radeon 780M Graphics"
time=3 level=INFO msg="inference compute" id=cpu library=cpu compute="" name=cpu description=cpu total="31.3 GiB"
"""
ENABLED = DROPPED + """time=4 level=INFO msg="server config" env="map[OLLAMA_IGPU_ENABLE:1]"
time=5 level=INFO msg="inference compute" id=0 library=Vulkan compute=0.0 name=Vulkan0 description="AMD Radeon 780M Graphics" type=iGPU
"""


def _no_ps(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)


def test_igpu_dropped_gives_fix(tmp_path, monkeypatch):
    _no_ps(monkeypatch)
    log = tmp_path / "server.log"
    log.write_text(DROPPED, encoding="utf-8")
    monkeypatch.setattr(doctor, "_ollama_server_log", lambda: log)
    gpu, detail, fix = doctor.ollama_gpu("http://x")
    assert gpu is False and "780M" in detail and "OLLAMA_IGPU_ENABLE=1" in fix and "Undo" in fix


def test_latest_start_wins(tmp_path, monkeypatch):
    _no_ps(monkeypatch)
    log = tmp_path / "server.log"
    log.write_text(ENABLED, encoding="utf-8")
    monkeypatch.setattr(doctor, "_ollama_server_log", lambda: log)
    gpu, detail, fix = doctor.ollama_gpu("http://x")
    assert gpu is True and "Vulkan" in detail and not fix


def test_loaded_model_on_gpu(monkeypatch):
    class R:
        def json(self):
            return {"models": [{"name": "qwen2.5:7b", "size": 100, "size_vram": 100}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    assert doctor.ollama_gpu("http://x")[:2] == (True, "qwen2.5:7b: 100% on the GPU")
