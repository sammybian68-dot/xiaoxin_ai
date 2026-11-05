"""自然拼读拆解（server/phonics.py）：核对规则（字母拼回去、音连起来对上词典），接口、缓存。模型换成假的。"""

from __future__ import annotations

import json

from server import phonics as P


def _p(*pairs):
    return [{"g": g, "s": s} for g, s in pairs]


def test_spell_with_magic_e():
    assert P.spell(_p(("c", "k"), ("a_e", "eɪ"), ("k", "k"))) == "cake"
    assert P.spell(_p(("b", "b"), ("i_e", "aɪ"), ("k", "k"))) == "bike"
    assert P.spell(_p(("k", ""), ("n", "n"), ("ow", "əʊ"))) == "know"


def test_check_accepts_good_and_rejects_bad():
    good = {"parts": _p(("c", "k"), ("a_e", "eɪ"), ("k", "k")), "syl": "cake"}
    assert P.check("cake", good, "keɪk") == ""
    # 括号里可读可不读、(r) 不读、两种读法：都认
    assert P.check("beautiful", {"parts": _p(("b", "b"), ("eau", "juː"), ("t", "t"), ("i", "ɪ"), ("f", "f"), ("u", "ə"), ("l", "l")),
                                 "syl": "beau·ti·ful"}, "ˈbjuːtɪf(ə)l") == ""
    assert P.check("father", {"parts": _p(("f", "f"), ("a", "ɑː"), ("th", "ð"), ("er", "ə")), "syl": "fa·ther"}, "ˈfɑːðə(r)") == ""
    assert P.check("elephant", {"parts": _p(("e", "e"), ("l", "l"), ("e", "ɪ"), ("ph", "f"), ("a", "ə"), ("n", "n"), ("t", "t")),
                                "syl": "el·e·phant"}, "ˈelɪfənt; ˈeləfənt") == ""
    # 字母对不上、音对不上、音节对不上：都不收
    assert "字母" in P.check("cake", {"parts": _p(("c", "k"), ("a", "eɪ"), ("k", "k")), "syl": "cake"}, "keɪk")
    assert "音连起来" in P.check("cake", {"parts": _p(("c", "k"), ("a_e", "æ"), ("k", "k")), "syl": "cake"}, "keɪk")
    assert "音节" in P.check("cake", {"parts": good["parts"], "syl": "ca·kee"}, "keɪk")
    assert P.check("cake", {"parts": "c-a-k-e"}, "keɪk")


def test_build_caches_and_rejects(monkeypatch):
    calls = []

    def fake_ask(words):
        calls.append(dict(words))
        return {"cake": {"parts": [{"g": "c", "s": "k"}, {"g": "a_e", "s": "eɪ"}, {"g": "k", "s": "k"}], "syl": "cake"},
                "ship": {"parts": [{"g": "s", "s": "s"}, {"g": "h", "s": "h"}, {"g": "i", "s": "ɪ"}, {"g": "p", "s": "p"}], "syl": "ship"}}

    monkeypatch.setattr(P, "_ask", fake_ask)
    monkeypatch.setattr(P, "_ipa", lambda w: {"cake": "keɪk", "ship": "ʃɪp"}[w])
    out = P.build(["cake", "ship", "I'm", "cake"])
    assert not out["cake"].get("fail") and out["ship"]["fail"].startswith("音连起来")   # s+h 读 sh 不对：不收
    assert list(calls[0]) == ["cake", "ship"]                                           # 带撇号的、重复的不问
    P.build(["cake", "ship"])
    assert len(calls) == 1                                                              # 都存下了，不再问
    assert P.get("cake")["parts"][1]["g"] == "a_e" and P.get("ship")["fail"]


def test_api(client, monkeypatch):
    from server import dictionary

    monkeypatch.setattr(dictionary, "phonetic", lambda w: {"word": w, "uk": "keɪk", "us": "keɪk"})
    monkeypatch.setattr(P, "_ipa", lambda w: "keɪk")
    monkeypatch.setattr(P, "_ask", lambda words: {"cake": {"parts": [{"g": "c", "s": "k"}, {"g": "a_e", "s": "eɪ"}, {"g": "k", "s": "k"}],
                                                           "syl": "cake"}})
    d = client.get("/api/english/phonics/cake?wait=5").json()
    assert d["uk"] == "keɪk" and [p["g"] for p in d["parts"]] == ["c", "a_e", "k"] and not d.get("pending")
    assert client.get("/api/english/phonics/苹果").status_code == 400
    # 短语：只给音标，不拆
    d = client.get("/api/english/phonics/ice%20cream").json()
    assert "parts" not in d and d["pending"] is False
    json.dumps(d)
