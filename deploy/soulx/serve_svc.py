"""SoulX-Singer-SVC 推理服务，5090 本机 127.0.0.1:18024（gpu2 经 ssh 隧道连过来，见 tunnel_5090.sh）。模型常驻显存，一次只跑一个任务。

  POST /jobs       翻唱：录的声音（prompt）+ 原曲（target）→ 换成这个声音唱、混回伴奏
  POST /separate   只分离：一首歌（小新拿翻唱好的成品来）→ 去掉人声的伴奏，跟着唱用
部署：拷到 5090 ~/codes/SoulX-Singer/serve_svc.py，由 svc_up_5090.sh 拉起。"""

from __future__ import annotations

import gc
import os
import queue
import random
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ.setdefault("HF_HOME", "/Data/cmbian/hf-cache")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import imageio_ffmpeg
import librosa
import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from cli.inference_svc import build_model, process as svc_process
from preprocess.tools.f0_extraction import F0Extractor
from preprocess.tools.vocal_separation.model import VocalSeparator
from soulxsinger.utils.file_utils import load_config

DEVICE = "cuda:0"
WORK = Path(os.environ.get("SOULX_WORK", "/Data/cmbian/private/soulx-jobs"))
SEP_SR = 44100
PROMPT_MAX_SEC = 30
TARGET_MAX_SEC = 600
JOB_TTL = 3600
MAX_QUEUE = 4
PRE = "pretrained_models/SoulX-Singer-Preprocess"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

WORK.mkdir(parents=True, exist_ok=True)
os.chmod(WORK, 0o700)

separator = VocalSeparator(
    sep_model_path=f"{PRE}/mel-band-roformer-karaoke/mel_band_roformer_karaoke_becruily.ckpt",
    sep_config_path=f"{PRE}/mel-band-roformer-karaoke/config_karaoke_becruily.yaml",
    der_model_path=f"{PRE}/dereverb_mel_band_roformer/dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt",
    der_config_path=f"{PRE}/dereverb_mel_band_roformer/dereverb_mel_band_roformer_anvuew.yaml",
    device=DEVICE,
)
f0_extractor = F0Extractor(model_path=f"{PRE}/rmvpe/rmvpe.pt", device=DEVICE)
config = load_config("soulxsinger/config/soulxsinger.yaml")
model = build_model(
    model_path="pretrained_models/SoulX-Singer/model-svc.pt",
    config=config,
    device=DEVICE,
    use_fp16=True,
)

app = FastAPI()
jobs: dict[str, dict] = {}
pending: queue.Queue[str] = queue.Queue()
lock = threading.Lock()


def _decode(src: Path, dst: Path, max_sec: int) -> float:
    result = subprocess.run(
        [FFMPEG, "-nostdin", "-y", "-v", "error", "-i", str(src), "-t", str(max_sec),
         "-ac", "1", "-ar", str(SEP_SR), "-f", "wav", str(dst)],
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0 or not dst.exists():
        raise ValueError("读不了这个音频：" + result.stderr.decode("utf-8", "replace")[-300:])
    seconds = sf.info(str(dst)).duration
    if seconds < 1:
        raise ValueError("音频不到 1 秒")
    return seconds


def _prepare(raw: Path, out: Path, separate: bool) -> tuple[Path, Path, Path | None]:
    out.mkdir(parents=True, exist_ok=True)
    vocal_path = out / "vocal.wav"
    acc_path = None
    if separate:
        sep = separator.process(str(raw), verbose=False)
        sf.write(vocal_path, sep.vocals_dereverbed.T, sep.sample_rate)
        acc_path = out / "acc.wav"
        sf.write(acc_path, sep.accompaniment.T, sep.sample_rate)
    else:
        shutil.copy(raw, vocal_path)
    f0_path = out / "vocal_f0.npy"
    f0_extractor.process(str(vocal_path), f0_path=str(f0_path))
    return vocal_path, f0_path, acc_path


def _mix(vocal_path: Path, acc_path: Path, shift: int, dst: Path) -> None:
    sr = config.audio.sample_rate
    acc_shift = (abs(shift) % 12) * (-1 if shift < 0 else 1)
    if acc_shift > 6:
        acc_shift -= 12
    if acc_shift < -6:
        acc_shift += 12
    vocal, _ = librosa.load(str(vocal_path), sr=sr, mono=True)
    acc, _ = librosa.load(str(acc_path), sr=sr, mono=True)
    if acc_shift:
        acc = librosa.effects.pitch_shift(acc, sr=sr, n_steps=acc_shift)
    n = min(len(vocal), len(acc))
    mixed = vocal[:n] + acc[:n]
    peak = float(np.max(np.abs(mixed))) if n else 1.0
    if peak > 1.0:
        mixed = mixed / peak
    sf.write(str(dst), mixed, sr)


def _run(job_id: str) -> None:
    job = jobs[job_id]
    base = WORK / job_id
    started = time.perf_counter()
    timings: dict[str, float] = {}

    def stage(name: str) -> None:
        job["stage"] = name
        job["stage_at"] = time.time()

    try:
        if job.get("kind") == "sep":
            stage("separate")
            sep = separator.process(str(base / "target.wav"), verbose=False)
            (base / "out").mkdir(exist_ok=True)
            acc = base / "out" / "acc.wav"
            sf.write(acc, sep.accompaniment.T, sep.sample_rate)
            job.update(state="done", result=str(acc), total_ms=(time.perf_counter() - started) * 1000)
            return
        stage("prompt")
        t = time.perf_counter()
        pt_wav, pt_f0, _ = _prepare(base / "prompt.wav", base / "prompt", job["prompt_sep"])
        timings["prompt_ms"] = (time.perf_counter() - t) * 1000

        stage("separate" if job["target_sep"] else "target")
        t = time.perf_counter()
        gt_wav, gt_f0, acc = _prepare(base / "target.wav", base / "target", job["target_sep"])
        timings["target_ms"] = (time.perf_counter() - t) * 1000

        stage("convert")
        t = time.perf_counter()
        torch.manual_seed(job["seed"])
        np.random.seed(job["seed"])
        random.seed(job["seed"])

        class Args:
            pass

        args = Args()
        args.device = DEVICE
        args.prompt_wav_path = str(pt_wav)
        args.target_wav_path = str(gt_wav)
        args.prompt_f0_path = str(pt_f0)
        args.target_f0_path = str(gt_f0)
        args.save_dir = str(base / "out")
        args.auto_shift = job["auto_shift"]
        args.pitch_shift = job["pitch_shift"]
        args.n_steps = job["n_steps"]
        args.cfg = job["cfg"]
        args.use_fp16 = True
        svc_process(args, config, model)
        timings["convert_ms"] = (time.perf_counter() - t) * 1000
        vocal = base / "out" / "generated.wav"
        result = vocal
        if job["mix_acc"] and acc is not None:
            stage("mix")
            result = base / "out" / "mixed.wav"
            _mix(vocal, acc, args.pitch_shift, result)
        job.update(
            state="done",
            result=str(result),
            vocal=str(vocal),
            shift=int(args.pitch_shift),
            total_ms=(time.perf_counter() - started) * 1000,
            **timings,
        )
    except Exception as exc:
        job.update(state="error", error=str(exc)[:500])
    finally:
        job["stage"] = None
        job["finished"] = time.time()
        for sub in ("prompt", "target"):
            shutil.rmtree(base / sub, ignore_errors=True)
        for name in ("prompt.wav", "target.wav", "prompt.in", "target.in"):
            (base / name).unlink(missing_ok=True)
        gc.collect()
        torch.cuda.empty_cache()


def _worker() -> None:
    while True:
        job_id = pending.get()
        if job_id in jobs:
            jobs[job_id]["state"] = "running"
            _run(job_id)


def _sweeper() -> None:
    while True:
        time.sleep(300)
        now = time.time()
        for job_id, job in list(jobs.items()):
            if job.get("finished") and now - job["finished"] > JOB_TTL:
                _drop(job_id)
        known = set(jobs)
        for path in WORK.iterdir():
            if path.name not in known and now - path.stat().st_mtime > JOB_TTL:
                shutil.rmtree(path, ignore_errors=True)


def _drop(job_id: str) -> None:
    jobs.pop(job_id, None)
    shutil.rmtree(WORK / job_id, ignore_errors=True)


threading.Thread(target=_worker, daemon=True).start()
threading.Thread(target=_sweeper, daemon=True).start()


@app.get("/health")
def health() -> dict:
    waiting = sum(1 for job in jobs.values() if job["state"] in ("queued", "running"))
    return {
        "ok": True,
        "model": "SoulX-Singer-SVC",
        "busy": waiting,
        "gpu_mem_gb": round(torch.cuda.memory_reserved() / 1024**3, 1),
    }


@app.post("/jobs")
async def create_job(
    prompt: UploadFile = File(...),
    target: UploadFile = File(...),
    prompt_sep: bool = Form(False),
    target_sep: bool = Form(True),
    auto_shift: bool = Form(True),
    mix_acc: bool = Form(True),
    pitch_shift: int = Form(0),
    n_steps: int = Form(32),
    cfg: float = Form(1.0),
    seed: int = Form(42),
) -> dict:
    if sum(1 for job in jobs.values() if job["state"] in ("queued", "running")) >= MAX_QUEUE:
        raise HTTPException(429, "排队的任务太多，等前面的做完")
    if not -24 <= pitch_shift <= 24:
        raise HTTPException(400, "变调范围 -24 到 24 个半音")
    if not 4 <= n_steps <= 100:
        raise HTTPException(400, "采样步数 4 到 100")
    if not 0 <= cfg <= 5:
        raise HTTPException(400, "cfg 0 到 5")
    job_id = uuid.uuid4().hex[:16]
    base = WORK / job_id
    base.mkdir(mode=0o700)
    try:
        for upload, name in ((prompt, "prompt"), (target, "target")):
            raw = base / f"{name}.in"
            raw.write_bytes(await upload.read())
        prompt_sec = _decode(base / "prompt.in", base / "prompt.wav", PROMPT_MAX_SEC)
        target_sec = _decode(base / "target.in", base / "target.wav", TARGET_MAX_SEC)
    except (ValueError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(base, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    jobs[job_id] = {
        "state": "queued",
        "stage": None,
        "created": time.time(),
        "prompt_sec": round(prompt_sec, 1),
        "target_sec": round(target_sec, 1),
        "prompt_sep": prompt_sep,
        "target_sep": target_sep,
        "auto_shift": auto_shift,
        "mix_acc": mix_acc,
        "pitch_shift": pitch_shift,
        "n_steps": n_steps,
        "cfg": cfg,
        "seed": seed,
    }
    pending.put(job_id)
    return {"id": job_id, "prompt_sec": round(prompt_sec, 1), "target_sec": round(target_sec, 1)}


@app.post("/separate")
async def create_separate(target: UploadFile = File(...)) -> dict:
    """去掉人声留伴奏；进度、结果和翻唱任务一样走 /jobs/{id}、/jobs/{id}/audio。"""
    if sum(1 for job in jobs.values() if job["state"] in ("queued", "running")) >= MAX_QUEUE:
        raise HTTPException(429, "排队的任务太多，等前面的做完")
    job_id = uuid.uuid4().hex[:16]
    base = WORK / job_id
    base.mkdir(mode=0o700)
    try:
        (base / "target.in").write_bytes(await target.read())
        target_sec = _decode(base / "target.in", base / "target.wav", TARGET_MAX_SEC)
    except (ValueError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(base, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    jobs[job_id] = {"state": "queued", "kind": "sep", "stage": None, "created": time.time(), "target_sec": round(target_sec, 1)}
    pending.put(job_id)
    return {"id": job_id, "target_sec": round(target_sec, 1)}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或已过期")
    ahead = 0
    if job["state"] == "queued":
        ahead = sum(
            1 for other in jobs.values()
            if other["state"] in ("queued", "running") and other["created"] < job["created"]
        )
    keys = ("state", "stage", "prompt_sec", "target_sec", "shift", "error",
            "prompt_ms", "target_ms", "convert_ms", "total_ms", "stage_at")
    return {"id": job_id, "ahead": ahead, **{k: job[k] for k in keys if k in job}}


@app.get("/jobs/{job_id}/audio")
def job_audio(job_id: str, kind: str = "result", fmt: str = "wav") -> FileResponse:
    job = jobs.get(job_id)
    if job is None or job.get("state") != "done":
        raise HTTPException(404, "结果不存在")
    path = Path(job.get("vocal", "") if kind == "vocal" else job["result"])
    if not path.is_file():
        raise HTTPException(404, "结果不存在")
    if fmt == "mp3":
        mp3 = path.with_suffix(".mp3")
        if not mp3.exists():
            tmp = mp3.with_suffix(".part.mp3")
            subprocess.run([FFMPEG, "-nostdin", "-y", "-v", "error", "-i", str(path), "-codec:a", "libmp3lame",
                            "-b:a", "128k", str(tmp)], check=True, timeout=120)
            tmp.replace(mp3)
        return FileResponse(mp3, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.delete("/jobs/{job_id}")
def job_delete(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        return {"ok": True}
    if job["state"] in ("queued", "running"):
        raise HTTPException(409, "任务还在跑")
    _drop(job_id)
    return {"ok": True}
