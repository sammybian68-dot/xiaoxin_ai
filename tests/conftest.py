"""测试框架的公共部分。

分三层，默认只跑前两层，全程不联网、不碰 gpu2：
  单元   纯函数和单个模块（切句、静音截停、挑参考段、引擎设置）
  接口   FastAPI TestClient 打 server.main，外部服务全换成 tests/fakes.py 里的假实现
  live   打 gpu2 上真实的 Triton / ASR worker / 豆包，标 @pytest.mark.live，加 --live 才跑

隔离：每个测试一个临时 XIAOXIN_DATA，key 用测试专用的；真实的 .env 不会被读到（见 isolate_env）。
"""

from __future__ import annotations

import pytest

TEST_KEY = "test-key-0123456789-abcdefghij"


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="也跑打真实服务的 live 测试（要在 gpu2 上）")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live 测试，加 --live 才跑")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolate_env(request, tmp_path, monkeypatch):
    """所有非 live 测试：数据目录换成临时目录，.env 读不到，key 是测试 key。"""
    if "live" in request.keywords:
        return
    from server import store
    from src.voice import doubao

    data = tmp_path / "data"
    monkeypatch.setattr(store, "DATA", data)
    monkeypatch.setattr(store, "CONV", data / "conversations")
    monkeypatch.setattr(store, "FILES", data / "files")
    monkeypatch.setattr(store, "VOICE", data / "voice")
    monkeypatch.setattr(store, "TALK_AUDIO", data / "talk_audio")
    monkeypatch.setattr(store, "SETTINGS", data / "settings.json")
    monkeypatch.setattr(doubao, "ROOT", tmp_path)  # .env 从这里读，这里没有
    for name in ("DOUBAO_TTS_API_KEY", "DOUBAO_API_KEY", "XIAOXIN_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    from server import voices

    monkeypatch.setattr(voices, "COSYVOICE_ASSET", request.getfixturevalue("fake_cosyvoice_asset"))
    monkeypatch.setenv("XIAOXIN_KEY", TEST_KEY)
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "fake-doubao-key")


@pytest.fixture(scope="session")
def fake_cosyvoice_asset(tmp_path_factory):
    """内置男声素材：造一段 24 kHz、5 秒的「男声」（和真素材同采样率），不依赖 gpu2。整个测试只造一次。"""
    from server import voices
    from tests.audio import tone, wav_bytes

    asset = tmp_path_factory.mktemp("cosyvoice_asset")
    (asset / voices.BUILTIN_MALE_SOURCE).write_bytes(wav_bytes(tone(5.0, rate=24000, freq=125), rate=24000))
    return asset


@pytest.fixture
def fakes(monkeypatch):
    """把豆包、Paraformer、CosyVoice、LLM 全换成假的，返回控制它们的对象。"""
    from tests.fakes import install

    return install(monkeypatch)


@pytest.fixture
def client(fakes):
    """带登录的 TestClient（Bearer），外部服务都是假的。"""
    from fastapi.testclient import TestClient

    from server import main

    with TestClient(main.app, headers={"Authorization": f"Bearer {TEST_KEY}"}) as c:
        yield c


@pytest.fixture
def anon(fakes):
    from fastapi.testclient import TestClient

    from server import main

    with TestClient(main.app) as c:
        yield c
