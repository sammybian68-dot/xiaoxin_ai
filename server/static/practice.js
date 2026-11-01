"use strict";
// 练习模式（server/practice.py）：全屏，盖住聊天。
//   听写：小新一个词一个词读，每个读两遍、每遍后等 5 秒（屏幕上「第 n / N 个」，可以「再读一遍」「暂停」）；
//   读完说「默写结束了，下面 xx 你自己完成吧，还剩 xx」→ 倒计时；到 0 报「时间到了」，接着走「超时 x 分」（红色）；
//   点「完成」→「拍照交卷」（可以拍好几张）→ 交上去后小新批改，结果发群里，这里显示「批改中…」→ 收到结果关掉。
// 只有孩子的 pad 上全屏；家长那边只在群里看到消息。刷新、断线重连后从 /api/practice/current 接着计时。

const prac = { r: null, timer: 0, gen: 0, paused: false, listenAt: 0, said: {}, photos: [] };

function pracIsKid() { return state.me?.role === "kid" || (state.me?.kind === "admin" && !!native); }

async function pracSay(text) {
  // 用小新的声音念一句，念完才返回（听写要等它念完再开始数 5 秒）
  const r = await api("api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
  const blob = await r.blob();
  return new Promise((resolve) => {
    const a = new Audio(URL.createObjectURL(blob));
    prac.audio = a;
    a.onended = a.onerror = () => resolve();
    a.play().catch(() => resolve());
  });
}
const pracSleep = (ms) => new Promise((r) => setTimeout(r, ms));

function pracOpen(r) {
  prac.r = r;
  prac.skew = (r.now ? r.now * 1000 - Date.now() : 0);  // 服务端时间和本机差多少
  let el = $("practice");
  if (!el) {
    el = document.createElement("div");
    el.id = "practice";
    el.className = "prac";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "练习");
    document.body.appendChild(el);
  }
  el.hidden = false;
  pracRender();
  clearInterval(prac.timer);
  prac.timer = setInterval(pracTick, 500);
  if (r.state === "listening") pracListen();
}

function pracClose() {
  prac.gen++;
  clearInterval(prac.timer);
  prac.audio?.pause();
  prac.r = null;
  const el = $("practice");
  if (el) el.hidden = true;
}

function pracNow() { return (Date.now() + (prac.skew || 0)) / 1000; }
function pracUsed() { const r = prac.r; return Math.max(0, Math.floor((r.finished || pracNow()) - r.started)); }
function pracClock(s) { const m = Math.floor(s / 60), x = s % 60; return `${m}:${String(x).padStart(2, "0")}`; }

function pracRender() {
  const r = prac.r, el = $("practice");
  const kidName = state.people?.[r.kid]?.name || "";
  let body = "";
  if (r.state === "listening") {
    body = `<div class="prac-big" id="prac-word">准备好笔</div><div class="prac-sub" id="prac-step">听写 · 一共 ${r.listen.length} 个词，每个读两遍</div>`
      + `<div class="prac-btns"><button type="button" data-p="again">🔁 再读一遍</button><button type="button" data-p="pause">⏸ 暂停</button>`
      + `<button type="button" data-p="skip">听写结束</button></div>`;
  } else if (r.state === "writing") {
    body = `<div class="prac-sub">${esc(kidName)}，自己完成剩下的吧</div>`
      + `<div class="prac-btns"><button type="button" class="primary" data-p="finish">✅ 我做完了</button></div>`;
  } else if (r.state === "done") {
    body = `<div class="prac-sub">做完啦！把卷子每一页都拍下来交给小新批改</div>`
      + `<div class="prac-photos" id="prac-photos"></div>`
      + `<div class="prac-btns"><label class="primary"><input type="file" accept="image/*" capture="environment" hidden data-p="shot">📷 拍一页</label>`
      + `<button type="button" data-p="submit" ${prac.photos.length ? "" : "disabled"}>交卷（${prac.photos.length} 页）</button>`
      + `<button type="button" data-p="later">等会儿再交</button></div>`;
  } else if (r.state === "graded") {
    const g = r.grade || {};
    body = `<div class="prac-big">${g.right ?? "?"} / ${g.total ?? "?"}</div><div class="prac-sub">批改好了，结果发到群里了</div>`
      + `<div class="prac-btns"><button type="button" class="primary" data-p="close">好的</button></div>`;
  }
  el.innerHTML = `<div class="prac-top"><b>${esc(r.title)}</b><span>第 ${r.attempt} 次练</span></div>`
    + `<div class="prac-time" id="prac-time"></div><div class="prac-limit">限时 ${pracClock(r.limit_s)}</div>${body}`;
  pracTick();
  if (r.state === "done") pracPhotos();
}

function pracTick() {
  const r = prac.r;
  if (!r) return;
  const t = $("prac-time");
  if (!t) return;
  const used = pracUsed(), left = r.limit_s - used;
  if (r.state === "graded" || r.state === "done") { t.className = "prac-time"; t.textContent = `用时 ${pracClock(used)}` + (left < 0 ? ` · 超时 ${pracClock(-left)}` : ""); return; }
  if (left >= 0) { t.className = "prac-time" + (left <= 60 ? " soon" : ""); t.textContent = `还剩 ${pracClock(left)}`; }
  else { t.className = "prac-time over"; t.textContent = `超时 ${pracClock(-left)}`; }
  if (left <= 0 && !prac.said.timeup && r.state === "writing") { prac.said.timeup = true; pracSay("时间到了。还没写完的可以接着写，写完点我做完了。"); }
}

// 听写：每个词读两遍，每遍读完等 5 秒。gen 变了（暂停、跳过、关掉）就停
async function pracListen(from = prac.listenAt) {
  const gen = ++prac.gen;
  const r = prac.r;
  if (from === 0) { await pracSay(`开始听写，一共 ${r.listen.length} 个词，每个词读两遍。`); await pracSleep(1500); }
  for (let i = from; i < r.listen.length; i++) {
    if (gen !== prac.gen) return;
    prac.listenAt = i;
    const w = r.listen[i];
    const big = $("prac-word"), step = $("prac-step");
    if (big) big.textContent = `第 ${w.no} 个`;
    if (step) step.textContent = `听写 · ${w.no} / ${r.listen.length}`;
    for (let k = 0; k < 2; k++) {
      if (gen !== prac.gen) return;
      await pracSay(k === 0 ? `第${w.no}个，${w.word}` : w.word);
      if (gen !== prac.gen) return;
      await pracSleep(5000);
    }
  }
  if (gen !== prac.gen) return;
  await pracListenDone();
}

async function pracListenDone() {
  prac.gen++;
  const r = await api(`api/practice/${prac.r.id}/listened`, { method: "POST" }).then(json);
  prac.r = r;
  pracRender();
  const kidName = state.people?.[r.kid]?.name || "";
  const left = r.limit_s - pracUsed();
  await pracSay(`默写结束了，下面${kidName}你自己完成吧，` + (left > 0 ? `还剩${Math.round(left / 60)}分钟。` : "已经超时了，抓紧哦。"));
}

async function pracPhotos() {
  const box = $("prac-photos");
  if (!box) return;
  box.innerHTML = prac.photos.map((p, i) => `<figure><img src="${p.preview}" alt="第 ${i + 1} 页"><button type="button" data-rm="${i}" aria-label="去掉">✕</button></figure>`).join("");
}

document.addEventListener("click", async (e) => {
  const el = e.target.closest("#practice [data-p], #practice [data-rm]");
  if (!el || !prac.r) return;
  const p = el.dataset.p;
  if (el.dataset.rm != null) { prac.photos.splice(+el.dataset.rm, 1); pracRender(); return; }
  if (p === "again") { prac.gen++; prac.audio?.pause(); pracListen(prac.listenAt); }
  else if (p === "pause") {
    if (el.dataset.on) { delete el.dataset.on; el.textContent = "⏸ 暂停"; pracListen(prac.listenAt); }
    else { prac.gen++; prac.audio?.pause(); el.dataset.on = "1"; el.textContent = "▶ 接着读"; }
  } else if (p === "skip") { prac.audio?.pause(); await pracListenDone(); }
  else if (p === "finish") {
    prac.gen++;
    prac.r = await api(`api/practice/${prac.r.id}/finish`, { method: "POST" }).then(json);
    pracRender();
    pracSay("做完啦！把卷子拍下来交给小新批改吧。");
  } else if (p === "submit") {
    el.disabled = true;
    el.textContent = "上传中…";
    try {
      const ids = [];
      for (const ph of prac.photos) ids.push(ph.meta?.id || (ph.meta = await uploadImage(ph.file)).id);
      prac.r = await api(`api/practice/${prac.r.id}/submit`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: ids }) }).then(json);
      prac.photos = [];
      $("practice").querySelector(".prac-btns").innerHTML = '<div class="prac-sub">交上去了，小新在批改…（结果会发到群里）</div>';
      setTimeout(() => { if (prac.r?.state !== "graded") pracClose(); }, 4000);
    } catch (err) { toast("交卷失败：" + err.message); el.disabled = false; el.textContent = "再交一次"; }
  } else if (p === "later" || p === "close") pracClose();
});
document.addEventListener("change", (e) => {
  const inp = e.target.closest('#practice [data-p="shot"]');
  if (!inp || !prac.r) return;
  for (const f of inp.files || []) prac.photos.push({ file: f, preview: URL.createObjectURL(f) });
  inp.value = "";
  pracRender();
});

// hub 推来的：小新开了练习（start_practice）、状态变了（家长那边也看得到）、批改结果
function onPracticeEvent(ev) {
  const r = ev.practice;
  if (!r || !pracIsKid() || r.thread !== state.conv?.id) return;
  if (prac.r && prac.r.id === r.id) {
    const changed = prac.r.state !== r.state;
    prac.r = { ...prac.r, ...r };
    if (changed && r.state !== "listening") { if (r.state === "graded" && $("practice")?.hidden) return; pracRender(); }
    return;
  }
  if (r.state === "listening" || r.state === "writing") { prac.listenAt = 0; prac.said = {}; prac.photos = []; pracOpen(r); }
}

// 打开 / 重连：有没做完的练习就接着
async function pracResume() {
  if (!pracIsKid() || prac.r) return;
  try {
    const r = await api(`api/practice/current?kid=${encodeURIComponent(state.conv?.kid || "")}`).then(json);
    if (r?.id && r.state !== "graded") { prac.said = { timeup: r.limit_s - Math.floor(r.now - r.started) < 0 }; prac.listenAt = 0; pracOpen(r); }
  } catch (_) { /* 没有就算了 */ }
}

window.onPracticeEvent = onPracticeEvent;
window.pracResume = pracResume;
