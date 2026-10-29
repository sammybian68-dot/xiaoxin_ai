"""Paraformer 客户端：worker 探活 / 拉起 / 加载的状态机，transcribe 的请求格式，StreamingParaformer。

HTTP 用一个假的 requests 模块顶替，不起真 worker。
"""

from __future__ import annotations

import asyncio
import io
import wave

import pytest

from src.voice import paraformer
from tests.audio import tone


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeWorker:
    """模拟 28182 的 worker：up=False 时连不上；seaco_loaded 控制 /health 里的 loaded。"""

    def __init__(self, up=True, loaded=True, has_seaco=True):
        self.up, self.loaded, self.has_seaco = up, loaded, has_seaco
        self.posts, self.spawned = [], 0

    def health(self):
        models = [{"key": "sensevoice-small", "loaded": False}]
        if self.has_seaco:
            models.append({"key": "seaco-paraformer", "loaded": self.loaded})
        return {"ok": True, "models": models}

    # requests 接口
    def get(self, url, timeout=None):
        if not self.up:
            raise paraformer_requests.ConnectionError("refused")
        assert url.endswith("/health")
        return Resp(data=self.health())

    def post(self, url, timeout=None, files=None, data=None):
        if not self.up:
            raise paraformer_requests.ConnectionError("refused")
        self.posts.append((url.rsplit("/", 2)[-2:], files, data))
        if url.endswith("/load"):
            self.loaded = True
            return Resp(data={"ok": True})
        if url.endswith("/transcribe"):
            return Resp(data={"text": " 你好小新 "})
        return Resp(404, text="not found")


import requests as paraformer_requests  # noqa: E402  真模块，只借它的异常类型


@pytest.fixture
def worker(monkeypatch):
    w = FakeWorker()
    monkeypatch.setattr(paraformer_requests, "get", w.get)
    monkeypatch.setattr(paraformer_requests, "post", w.post)

    def spawn():
        w.spawned += 1
        w.up = True

    monkeypatch.setattr(paraformer, "_spawn", spawn)
    monkeypatch.setattr(paraformer.time, "sleep", lambda s: None)
    return w


def test_status_none_when_worker_down(worker):
    worker.up = False
    assert paraformer.status() is None
    assert paraformer.ready() is False


def test_status_empty_when_worker_lacks_seaco(worker):
    worker.has_seaco = False
    assert paraformer.status() == {}


def test_ensure_noop_when_ready(worker):
    paraformer.ensure()
    assert worker.spawned == 0 and worker.posts == []


def test_ensure_spawns_worker_when_down(worker):
    worker.up, worker.loaded = False, True
    paraformer.ensure()
    assert worker.spawned == 1


def test_ensure_loads_model_when_worker_up_but_not_loaded(worker):
    worker.loaded = False
    paraformer.ensure()
    assert worker.spawned == 0
    assert [p[0] for p in worker.posts] == [["seaco-paraformer", "load"]]


def test_ensure_raises_when_worker_has_no_seaco(worker):
    worker.has_seaco = False
    with pytest.raises(paraformer.ParaformerError, match="没有 seaco"):
        paraformer.ensure()


def test_ensure_times_out_when_spawned_worker_never_comes_up(worker, monkeypatch):
    monkeypatch.setattr(paraformer, "_spawn", lambda: None)  # 拉了但起不来
    worker.up = False
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(paraformer.time, "monotonic", lambda: next(clock))
    with pytest.raises(paraformer.ParaformerError, match="没起来"):
        paraformer.ensure()


def test_spawn_fails_clearly_without_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(paraformer, "CARHER_SERVER", tmp_path)
    with pytest.raises(paraformer.ParaformerError, match="没有 ASR venv"):
        paraformer._spawn()


def test_transcribe_sends_16k_wav_with_backend_and_hotword(worker):
    pcm = tone(1.0)
    rec = paraformer.transcribe(pcm, hotword="小新 车控")
    assert rec.text == "你好小新"
    assert rec.audio_seconds == pytest.approx(1.0)
    (path, files, data), = [p for p in worker.posts if p[0][-1] == "transcribe"]
    assert data == {"backend": "seaco-paraformer", "language": "zh", "hotword": "小新 车控"}
    with wave.open(io.BytesIO(files["audio"][1])) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.readframes(w.getnframes()) == pcm


def test_transcribe_raises_on_http_error(worker, monkeypatch):
    monkeypatch.setattr(paraformer_requests, "post", lambda *a, **kw: Resp(500, text="boom"))
    with pytest.raises(paraformer.ParaformerError, match="500"):
        paraformer.transcribe(tone(0.5))


def test_streaming_buffers_and_transcribes_once_on_finish(worker):
    async def main():
        async with paraformer.StreamingParaformer() as rec:
            for _ in range(5):
                await rec.feed(tone(0.2))
            assert rec.partials.empty()  # 没有中间结果
            text = await rec.finish()
        return rec, text

    rec, text = asyncio.run(main())
    assert text == "你好小新"
    assert rec.seconds == pytest.approx(1.0)
    assert sum(1 for p in worker.posts if p[0][-1] == "transcribe") == 1


def test_streaming_finish_surfaces_startup_error(worker):
    worker.has_seaco = False

    async def main():
        async with paraformer.StreamingParaformer() as rec:
            await rec.feed(tone(0.2))
            await rec.finish()

    with pytest.raises(paraformer.ParaformerError):
        asyncio.run(main())


def test_streaming_cancel_without_finish_does_not_leak_errors(worker):
    """上滑取消：没调 finish 就退出，后台 ensure 的异常不能变成 asyncio 的「Task exception was never retrieved」。"""
    import gc

    worker.has_seaco = False
    leaked = []

    async def main():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: leaked.append(ctx["message"]))
        async with paraformer.StreamingParaformer() as rec:
            await rec.feed(tone(0.2))
            await asyncio.sleep(0.05)  # 让后台 ensure 跑完并失败
        del rec
        gc.collect()  # 没人取的异常在 task 被回收时才报

    asyncio.run(main())
    assert leaked == []
