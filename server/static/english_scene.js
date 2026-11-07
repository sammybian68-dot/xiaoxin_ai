"use strict";
// 英语小剧场（server/english/）：聊天里的课程卡片 → 全屏场景练习（选角色 → 对方说 → 轮到她：听一遍 / 小新演示 / 按住说 →
// 讯飞打分、读不准的词标红、最多读 3 次 → 下一句）→ 练完结束页。能暂停回聊天，顶栏「继续英语」或说「继续英语场景练习」回来。
// 录音用 app.js 的 Recorder（16 kHz wav），按住说话和这里共用。

const ens = { L: null, s: null, me: null, i: 0, gen: 0, zh: true, audio: null, rec: null, demoGen: 0, busy: false };
const ensKid = () => (state.conv?.kid ? `kid=${encodeURIComponent(state.conv.kid)}` : "");
const ensUrl = (p) => p + (ensKid() ? (p.includes("?") ? "&" : "?") + ensKid() : "");
async function ensApi(path, opts) {
  const r = await fetch(ensUrl(path), opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `出错了（${r.status}）`);
  return d;
}
const ensSleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ensWords = (t) => t.split(/\s+/).filter(Boolean);
const ensKey = (w) => w.replace(/[^A-Za-z']/g, "").toLowerCase();

function ensPlay(src) {
  return new Promise((res) => {
    ens.audio?.pause();
    const a = new Audio(src);
    ens.audio = a;
    a.onended = a.onerror = () => res();
    a.play().catch(() => res());
  });
}
function ensHush() { ens.demoGen++; ens.audio?.pause(); }

// ---------------------------------------------------------------- 打开一课
async function ensOpen(lid, sid) {
  let L;
  try { L = await ensApi(`api/english/lesson/${lid}`); } catch (e) { toast(e.message); return; }
  if (typeof pracHushXiaoxin === "function") pracHushXiaoxin();
  ens.L = L; ens.gen++; ens.i = 0;
  let el = $("ens");
  if (!el) {
    el = document.createElement("div");
    el.id = "ens"; el.className = "ens"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "英语小剧场");
    document.body.appendChild(el);
    el.addEventListener("click", ensClick);
  }
  el.hidden = false;
  const roles = L.roles;
  el.innerHTML = `<div class="ens-stage">
    <div class="ens-bg" style="background-image:url(api/english/asset/${L.id}/bg.jpg)"></div>
    ${roles.map((r, k) => `<div class="ens-actor idle a${k}" data-role="${r.id}"><img src="api/english/char/${r.id}.png" alt="${esc(r.name)}"></div>`).join("")}
    <div class="ens-bubble" id="ens-bubble"></div>
    <div class="ens-top"><span class="t">${esc(L.name)}</span><span class="ens-dots" id="ens-dots"></span><span class="sp"></span>
      <button type="button" data-e="zh" aria-pressed="true" title="句子下面显示 / 不显示中文翻译">中文翻译 开</button>
      <button type="button" class="pause" data-e="pause">⏸ 暂停</button>
      <button type="button" class="quit" data-e="quit">✕ 结束</button></div>
    <div class="ens-turn" id="ens-turn" aria-live="polite"></div>
    <div class="ens-cover" id="ens-cover"></div></div>`;
  ensDots();
  if (sid) {   // 接着上次暂停的
    try { ens.s = await ensApi(`api/english/session/${sid}`); } catch (_) { ens.s = null; }
    if (ens.s) { ens.me = ens.s.role; ens.i = ensResumeAt(); $("ens-cover").hidden = true; ensMarkMe(); ensRun(); return; }
  }
  ensPick();
}

function ensResumeAt() {
  // 从她还没读过的第一句、或者最后读的那句之后接着
  const lines = ens.L.lines, said = ens.s.lines || {};
  for (let i = 0; i < lines.length; i++) if (lines[i].role === ens.me && !said[i]) return Math.max(0, i - 1);
  return lines.length;
}

function ensPick() {
  const L = ens.L, c = $("ens-cover"), p = L.progress || {};
  c.hidden = false;
  c.innerHTML = `<div class="ens-card"><h2>${esc(L.name)}<small>英语小剧场 · ${esc(L.zh)}${p.times ? ` · 练过 ${p.times} 次 · ${["没练过", "会读", "熟练", "掌握 👑"][p.level || 0]}` : ""} —— 选一个角色，和小新演一遍</small></h2>
    <div class="ens-roles">${L.roles.map((r) => `<button type="button" class="ens-role" data-pick="${r.id}"><img src="api/english/char/${r.id}.png" alt=""><b>${esc(r.name)}</b><small>${esc(r.zh)}</small></button>`).join("")}</div>
    ${L.new_words?.length ? `<div class="ens-words">新单词：${L.new_words.map((w) => `<span>${esc(w.en)} · ${esc(w.zh)}</span>`).join("")}</div>` : ""}</div>`;
}

async function ensStart(role) {
  try { ens.s = await ensApi("api/english/session", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lesson: ens.L.id, role, kid: state.conv?.kid }) }); } catch (e) { toast(e.message); return; }
  ens.me = role; ens.i = 0; ens.gen++;
  $("ens-cover").hidden = true;
  ensMarkMe();
  ensRun();
}
function ensMarkMe() {
  document.querySelectorAll("#ens .ens-actor").forEach((a) => {
    a.querySelector(".ens-me")?.remove();
    if (a.dataset.role === ens.me) a.insertAdjacentHTML("beforeend", '<span class="ens-me">我</span>');
  });
}
function ensDots() {
  const n = ens.L.lines.length;
  $("ens-dots").innerHTML = Array.from({ length: n }, (_, k) => `<i class="${k < ens.i ? "done" : k === ens.i ? "now" : ""}"></i>`).join("");
}

// ---------------------------------------------------------------- 一句一句走
async function ensRun() {
  const my = ens.gen, lines = ens.L.lines;
  while (ens.i < lines.length && my === ens.gen) {
    ensDots();
    if (lines[ens.i].role === ens.me) await ensMyTurn(ens.i);
    else { await ensSpeak(ens.i); await ensSleep(600); }
    if (my !== ens.gen) return;
    ens.i++;
  }
  if (my === ens.gen) ensFinish();
}

function ensActors(talking) {
  document.querySelectorAll("#ens .ens-actor").forEach((a) => {
    const t = a.dataset.role === talking;
    a.classList.toggle("talk", !!talking && t); a.classList.toggle("idle", !talking || !t); a.classList.toggle("dim", !!talking && !t);
  });
}
async function ensSpeak(i, slow = false) {
  const l = ens.L.lines[i], b = $("ens-bubble"), my = ens.gen;
  const k = ens.L.roles.findIndex((r) => r.id === l.role);
  b.className = "ens-bubble at" + k;
  b.innerHTML = ensWords(l.en).map((w) => `<span class="w" data-ww="${esc(ensKey(w))}">${esc(w)}</span>`).join(" ") + `<span class="zh"${ens.zh ? "" : " hidden"}>${esc(l.zh)}</span>`;
  requestAnimationFrame(() => b.classList.add("show"));
  ensActors(l.role);
  const ws = [...b.querySelectorAll(".w")], dur = ((slow ? l.slow_s : l.dur_s) || 2.5) * 1000;
  const wt = ws.map((w) => ensKey(w.textContent).length + 2), tot = wt.reduce((a, c) => a + c, 0);
  let acc = 0;
  ws.forEach((w, n) => { setTimeout(() => { if (my === ens.gen) w.classList.add("on"); }, 120 + (acc / tot) * dur * 0.92); acc += wt[n]; });
  await ensPlay(`api/english/asset/${ens.L.id}/l${i}${slow ? "_slow" : ""}.mp3?v=${ens.L.audio_v || 0}`);
  if (my !== ens.gen) return;
  ws.forEach((w) => w.classList.add("on"));
  ensActors(null);
}

// 轮到她：面板升起；按住说 → 送服务端打分 → 标词、给星；过了 / 读满 3 次就「下一句」
function ensMyTurn(i) {
  return new Promise((done) => {
    ens.next = done;
    $("ens-bubble").classList.remove("show");
    ensActors(null);
    ensRenderTurn(ens.L.lines[i]);
    $("ens-turn").classList.add("show");
  });
}
function ensRenderTurn(l, r) {
  const t = $("ens-turn"), ws = ensWords(l.en);
  const scored = r?.words || [];
  let k = 0;
  const line = ws.map((w) => {
    const key = ensKey(w), s = scored.find((x, j) => j >= k && x.w === key);
    if (s) k = scored.indexOf(s) + 1;
    const v = s?.s, cls = !s ? "" : s.e === "漏读" || v < 60 ? "bad" : v < 80 ? "ok" : "good";
    return `<button type="button" class="w ${cls}" data-e="word" data-w="${esc(key)}" title="点一下单练这个词">${esc(w)}${s ? `<small>${s.e || Math.round(v)}</small>` : ""}</button>`;
  }).join(" ");
  const meRole = ens.L.roles.find((x) => x.id === ens.me);
  t.innerHTML = `<div class="say"><b>轮到你啦</b>你是 ${esc(meRole?.name || "")}，读这句：</div>
    <div class="ens-line">${line}</div><div class="zh"${ens.zh ? "" : " hidden"}>${esc(l.zh)}</div>
    <div class="ens-row">
      <button type="button" class="ens-btn" data-e="listen">🔊 听一遍</button>
      <button type="button" class="ens-btn" data-e="demo">👨‍🏫 小新演示</button>
      <button type="button" class="ens-mic" data-e="mic">🎤 按住说</button>
      <span class="ens-lvl"><i id="ens-lvl"></i></span>
    </div>
    ${ens.busy ? `<div class="ens-wait">小新在听你读得怎么样…</div>` : ""}
    ${r ? `<div class="ens-score"><span class="ens-stars">${"⭐".repeat(r.stars)}${"☆".repeat(3 - r.stars)}</span><span class="msg">${esc(r.msg)}</span></div>
      <div class="ens-row"><button type="button" class="ens-btn" data-e="mine">▶ 听听我说的</button>
      ${r.pass || r.last ? `<button type="button" class="ens-btn primary" data-e="next">下一句 →</button>`
        : `<span class="ens-wait">按住「🎤 按住说」再说一次</span><button type="button" class="ens-btn" data-e="next">先跳过</button>`}</div>` : ""}`;
  const mic = t.querySelector("[data-e=mic]");
  mic.addEventListener("pointerdown", (e) => { e.preventDefault(); ensRecStart(mic); });
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) mic.addEventListener(ev, () => ensRecStop(mic, l));
}

async function ensRecStart(btn) {
  if (ens.rec || ens.busy) return;
  ensHush();
  const lv = $("ens-lvl");
  ens.rec = new Recorder((peak) => { if (lv) lv.style.width = Math.min(100, peak * 260) + "%"; });
  ens.recAt = performance.now();
  btn.classList.add("rec"); btn.textContent = "🎙 在听…松手结束";
  try { await ens.rec.start(); } catch (e) { ens.rec = null; btn.classList.remove("rec"); btn.textContent = "🎤 按住说"; toast("没拿到麦克风：" + e.message); }
}
async function ensRecStop(btn, l) {
  const rec = ens.rec;
  if (!rec) return;
  ens.rec = null;
  const held = performance.now() - ens.recAt;
  const wavBlob = await rec.stop();
  btn.classList.remove("rec"); btn.textContent = "🎤 按住说";
  if (held < 500) { toast("按住说完再松手哦"); return; }
  ens.busy = true; ensRenderTurn(l, ens.last);
  try {
    const r = await ensApi(`api/english/session/${ens.s.id}/line/${ens.i}`, { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wavBlob });
    ens.last = r; ens.lastN = r.n;
    ens.busy = false;
    ensRenderTurn(l, r);
    if (r.pass && r.stars === 3) ensCheer();
  } catch (e) { ens.busy = false; ensRenderTurn(l, ens.last); toast(e.message); }
}
function ensCheer() { try { goSnd?.ctx && goClack(0); } catch (_) { /* 没有就算了 */ } }

// 小新演示：一个词一个词带读（正在读的词跳一下），再整句慢读一遍
async function ensDemo() {
  const my = ++ens.demoGen;
  const el = [...document.querySelectorAll("#ens-turn .ens-line .w")];
  for (let k = 0; k < el.length && my === ens.demoGen; k++) {
    el.forEach((x, j) => x.classList.toggle("now", j === k));
    await ensPlay(`api/english/word/${encodeURIComponent(el[k].dataset.w)}.mp3`);
    await ensSleep(250);
  }
  el.forEach((x) => x.classList.remove("now"));
  if (my === ens.demoGen) { await ensSleep(300); await ensSpeak(ens.i, true); $("ens-bubble").classList.remove("show"); }
}

async function ensClick(e) {
  const ww = e.target.closest("[data-ww]");
  if (ww && ww.dataset.ww) { ensWordCard(ww.dataset.ww); return; }   // 气泡里对方说的词：点了也能单练
  const b = e.target.closest("[data-e],[data-pick],[data-again]");
  if (!b) return;
  if (b.dataset.pick) { ensStart(b.dataset.pick); return; }
  if (b.dataset.again) { ensStart(b.dataset.again); return; }
  const k = b.dataset.e, l = ens.L.lines[ens.i];
  if (k === "pause" || k === "close") { ensPause(); return; }
  if (k === "quit") { ensQuit(); return; }
  if (k === "dict") { ensDictation(ens.L.id, b, ens.me); return; }
  if (k === "zh") {   // 句子下面的中文翻译：开 / 关（下一句起生效，这一句也马上跟着变）
    ens.zh = !ens.zh;
    b.setAttribute("aria-pressed", String(ens.zh));
    b.textContent = ens.zh ? "中文翻译 开" : "中文翻译 关";
    document.querySelectorAll("#ens .zh").forEach((z) => { z.hidden = !ens.zh; });
    return;
  }
  if (k === "listen") { ensDemoGenStop(); await ensSpeak(ens.i); $("ens-bubble").classList.remove("show"); }
  if (k === "demo") ensDemo();
  if (k === "word") { ensDemoGenStop(); ensWordCard(b.dataset.w); }
  if (k === "mine" && ens.lastN) ensPlay(ensUrl(`api/english/session/${ens.s.id}/audio/${ens.i}/${ens.lastN}.wav`));
  if (k === "next") { ens.last = null; ens.lastN = 0; $("ens-turn").classList.remove("show"); ens.next?.(); }
}
function ensDemoGenStop() { ens.demoGen++; }

// 暂停：收起场景，回聊天；这次练习留着（顶栏「继续英语」、说「继续英语场景练习」能回来）
function ensPause() {
  ens.gen++; ensHush();
  if (ens.rec) { ens.rec.stop(); ens.rec = null; }
  const el = $("ens"); if (el) el.hidden = true;
  ensResumeBtn();
}

// 结束：不演了，直接收起回聊天。读过的几句照常记成绩（群里说「练了一部分就结束了」）；一句没读就当没练。
// 和「暂停」不同：结束后顶栏不再亮「继续英语」
async function ensQuit() {
  ens.gen++; ensDemoGenStop(); ensHush();
  if (ens.rec) { ens.rec.stop(); ens.rec = null; }
  ensWordClose?.();
  const el = $("ens"); if (el) el.hidden = true;
  if (ens.s && !ens.s.done) {
    try { await ensApi(`api/english/session/${ens.s.id}/finish`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kid: state.conv?.kid, early: true }) }); } catch (e) { toast(e.message); }
    ens.s.done = true;
  }
  ensResumeBtn();
}

async function ensFinish() {
  let s;
  try { s = await ensApi(`api/english/session/${ens.s.id}/finish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kid: state.conv?.kid }) }); }
  catch (e) { toast(e.message); return; }
  ens.s.done = true;
  const sm = s.summary, other = ens.L.roles.find((r) => r.id !== ens.me);
  const c = $("ens-cover");
  c.hidden = false;
  c.innerHTML = `<div class="ens-card ens-end"><div class="big">${sm.stars >= 2.7 ? "🏆" : "🎉"}</div>
    <h2>演完啦！<small>${sm.said} / ${sm.of} 句 · 平均 ${sm.avg ?? "-"} 分 · ${"⭐".repeat(Math.round(sm.stars))}</small></h2>
    ${sm.weak?.length ? `<div class="ens-words">还要多练：${sm.weak.slice(0, 4).map(([w]) => `<span>${esc(w)}</span>`).join("")}</div>` : ""}
    <div class="ens-roles"><button type="button" class="ens-role" data-again="${ens.me}"><img src="api/english/char/${ens.me}.png" alt=""><b>再演一遍</b></button>
      <button type="button" class="ens-role" data-again="${other.id}"><img src="api/english/char/${other.id}.png" alt=""><b>换演 ${esc(other.name)}</b></button></div>
    <div class="ens-row" style="justify-content:center"><button type="button" class="ens-btn primary" data-e="dict">✍️ 来一张默写小练习</button>
      <button type="button" class="ens-btn" data-e="close">回到聊天</button></div></div>`;
  ensResumeBtn();
}

// 默写小练习：服务端出卷（听写句子 + 补全对话），以小新的消息发进群里，带练习纸卡片（打印 / 开始练习 / 拍照交卷）
async function ensDictation(lid, btn, role) {
  if (btn) { btn.textContent = "出题中…"; btn.style.pointerEvents = "none"; }
  try {
    await ensApi("api/english/dictation", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lesson: lid, role: role || "", kid: state.conv?.kid }) });
    if (btn) btn.textContent = "✓ 出好了，看下面";
    if ($("ens") && !$("ens").hidden) ensPause();   // 从结束页点的：回聊天看卷子
  } catch (e) { toast(e.message); if (btn) { btn.textContent = "✍️ 默写小练习"; btn.style.pointerEvents = ""; } }
}

// ---------------------------------------------------------------- 顶栏「继续英语」：有暂停着的就亮
async function ensResumeBtn() {
  let b = $("ens-resume-top");
  if (!b) {
    const anchor = $("open-history");
    if (!anchor) return;
    b = document.createElement("button");
    b.id = "ens-resume-top"; b.type = "button"; b.className = "go-resume-top"; b.hidden = true;
    b.textContent = "🗣 继续英语";
    b.addEventListener("click", async () => {
      try { const d = await ensApi("api/english"); if (d.current) ensOpen(d.current.lesson, d.current.id); else b.hidden = true; } catch (e) { toast(e.message); }
    });
    anchor.parentNode.insertBefore(b, anchor);
  }
  if ($("ens") && !$("ens").hidden) { b.hidden = true; return; }
  try { const d = await ensApi("api/english"); b.hidden = !d.current; } catch (_) { b.hidden = true; }
}

// ---------------------------------------------------------------- 聊天里：工具结果 → 卡片；小新后台发来的「做好了」→ 卡片
function ensCard(row, lid, sid, autoOpen) {
  const card = document.createElement("button");
  card.type = "button"; card.className = "ens-chip";   // 两个按钮：练口语（全屏）/ 默写小练习（出卷子）
  card.innerHTML = `<img src="api/english/asset/${lid}/bg.jpg" alt=""><span><b>英语小剧场</b><small>读取中…</small><br>
    <span class="go" data-k="talk">${sid ? "▶ 接着练口语" : "🗣 练口语"}</span> <span class="go dict" data-k="dict">✍️ 默写小练习</span></span>`;
  ensApi(`api/english/lesson/${lid}`).then((L) => {
    const p = L.progress || {};
    card.querySelector("b").textContent = `${L.name} · ${L.zh}`;
    card.querySelector("small").textContent = p.times ? `练过 ${p.times} 次 · ${["没练过", "会读", "熟练", "掌握 👑"][p.level || 0]}` : "还没练过";
  }).catch(() => { card.querySelector("small").textContent = "这一课不在了"; });
  card.addEventListener("click", (e) => {
    if (e.target.closest('[data-k="dict"]')) ensDictation(lid, e.target.closest('[data-k="dict"]'));
    else ensOpen(lid, sid);
  });
  row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
  requestAnimationFrame(() => typeof revealRow === "function" && revealRow(row));
  if (autoOpen) ensOpen(lid, sid);
}

function ensAction(row, a) {
  if (/^失败/.test(a.result || "")) return false;
  if (a.name === "english_scene") {
    const m = /\[en:([a-z0-9_]+)(?::([0-9a-f]{16}))?\]/.exec(a.result || "");
    if (!m) return false;
    ensCard(row, m[1], m[2], !!a.live);
    return true;
  }
  if (a.name === "english_scene_new") {
    const d = document.createElement("div");
    d.className = "ens-making";
    d.innerHTML = '<i aria-hidden="true"></i><span>小新在做新的对话场景：画画、写台词、配音，大约一分钟…</span>';
    row.querySelector(".bubble").insertBefore(d, row.querySelector(".bubble .text"));
    return true;
  }
  if (a.name === "english_scene_board") { ensBoardCard(row); return true; }
  return false;
}

// 小新后台发来的消息（练完的小结、新场景做好了）：带 english 字段的消息出卡片
function ensMessage(row, m) {
  if (!m.english?.lesson || m.english.session) return;
  ensCard(row, m.english.lesson, null, false);
}

setTimeout(ensResumeBtn, 1800);
// ---------------------------------------------------------------- 单词卡：一个词读不好，单独反复练
// 点任何一个英文词（口语面板里、看板「总是读不准的词」里）弹出：🔊 听（正常）/ 🐢 慢慢听 / 🔁 连听三遍（正常-慢-正常）/
// 🎤 按住跟读（讯飞按单词打分）/ ▶ 听我读的。关掉卡片回到原来的地方，不影响口语练习的进度
const ensW = { word: "", gen: 0, blob: null, rec: null };
function ensWordCard(word) {
  ensW.word = word; ensW.blob = null; ensW.gen++;
  let el = $("ens-word");
  if (!el) {
    el = document.createElement("div");
    el.id = "ens-word"; el.className = "ens-wordcard"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "练一个单词");
    document.body.appendChild(el);
    el.addEventListener("click", ensWordClick);
  }
  el.hidden = false;
  ensWordRender();
  ensWordPlay(0);
}
function ensWordRender(r) {
  const el = $("ens-word");
  el.innerHTML = `<div class="ens-wc"><button type="button" class="x" data-w="close" aria-label="关掉">×</button>
    <div class="big">${esc(ensW.word)}</div>
    <div class="ens-ph"></div>
    <div class="ens-row" style="justify-content:center">
      <button type="button" class="ens-btn" data-w="play">🔊 听</button>
      <button type="button" class="ens-btn" data-w="slow">🐢 慢慢听</button>
      <button type="button" class="ens-btn" data-w="loop">🔁 连听三遍</button>
    </div>
    <div class="ens-row" style="justify-content:center">
      <button type="button" class="ens-mic" data-w="mic">🎤 按住跟读</button>
      ${ensW.blob ? '<button type="button" class="ens-btn" data-w="mine">▶ 听我读的</button>' : ""}
    </div>
    ${r ? `<div class="ens-score" style="justify-content:center"><span class="ens-stars">${"⭐".repeat(r.stars)}${"☆".repeat(3 - r.stars)}</span><span class="msg">${esc(r.msg)}</span></div>` : ""}
    <small class="tip">读成别的词能听出来；口音上细小的差别以「听标准音、跟着读」为主</small></div>`;
  // 音标 + 自然拼读：同一个词重画（打完分）时用上次的那块，不再请求
  if (ensW.ph?.word !== ensW.word) ensW.ph = { word: ensW.word, box: typeof phonicsBox === "function" ? phonicsBox(ensW.word) : null };
  if (ensW.ph.box) el.querySelector(".ens-ph").appendChild(ensW.ph.box);
  const mic = el.querySelector("[data-w=mic]");
  mic.addEventListener("pointerdown", (e) => { e.preventDefault(); ensWordRec(mic); });
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) mic.addEventListener(ev, () => ensWordStop(mic));
}
const ensWordUrl = (slow) => `api/english/word/${encodeURIComponent(ensW.word)}.mp3${slow ? "?slow=1" : ""}`;
async function ensWordPlay(mode) {
  const my = ++ensW.gen;
  const seq = mode === "loop" ? [0, 1, 0] : [mode === "slow" ? 1 : 0];
  for (const slow of seq) {
    if (my !== ensW.gen) return;
    await ensPlay(ensWordUrl(slow));
    await ensSleep(700);
  }
}
// 关单词卡：停声音、丢掉没录完的；回到原来的地方（口语练习的进度、面板都不动）
function ensWordClose() {
  const el = $("ens-word");
  if (!el || el.hidden) return false;
  ensW.gen++;
  ens.audio?.pause();
  if (ensW.rec) { ensW.rec.stop(); ensW.rec = null; }
  el.hidden = true;
  return true;
}
function ensWordClick(e) {
  // 点卡片外面（半透明的那一圈）任何地方都关；点卡片里空白的地方不关
  if (!e.target.closest(".ens-wc")) { ensWordClose(); return; }
  const k = e.target.closest("[data-w]")?.dataset.w;
  if (!k) return;
  if (k === "close") { ensWordClose(); return; }
  if (k === "play") ensWordPlay(0);
  if (k === "slow") ensWordPlay("slow");
  if (k === "loop") ensWordPlay("loop");
  if (k === "mine" && ensW.blob) ensPlay(URL.createObjectURL(ensW.blob));
}
async function ensWordRec(btn) {
  if (ensW.rec) return;
  ensW.gen++; ens.audio?.pause();
  ensW.rec = new Recorder(() => {});
  ensW.at = performance.now();
  btn.classList.add("rec"); btn.textContent = "🎙 在听…松手结束";
  try { await ensW.rec.start(); } catch (e) { ensW.rec = null; btn.classList.remove("rec"); btn.textContent = "🎤 按住跟读"; toast("没拿到麦克风：" + e.message); }
}
async function ensWordStop(btn) {
  const rec = ensW.rec;
  if (!rec) return;
  ensW.rec = null;
  const held = performance.now() - ensW.at;
  const wav = await rec.stop();
  btn.classList.remove("rec"); btn.textContent = "🎤 按住跟读";
  if (held < 400) { toast("按住说完再松手哦"); return; }
  ensW.blob = wav;
  btn.textContent = "小新在听…";
  try {
    const r = await ensApi(`api/english/word/${encodeURIComponent(ensW.word)}/check`, { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
    ensWordRender(r);
  } catch (e) { toast(e.message); ensWordRender(); }
}

// 键盘 Esc、安卓返回键：先关单词卡
document.addEventListener("keydown", (e) => { if (e.key === "Escape") ensWordClose(); });
{
  const prevBack = window.xiaoxinBack;
  window.xiaoxinBack = () => ensWordClose() || (prevBack ? prevBack() : false);
}

// ---------------------------------------------------------------- 看板（聊天里一张卡）：今日推荐、两周、熟练度墙、读不准的词、最近练习
const ENS_LV = ["没练过", "会读", "熟练", "掌握 👑"];
async function ensBoardCard(row) {
  let d;
  try { d = await ensApi("api/english"); } catch (e) { return; }
  const box = document.createElement("div");
  box.className = "ensb";
  const rec = d.recommend?.pick ? d.lessons.find((L) => L.id === d.recommend.pick.id) : null;
  const maxMin = Math.max(5, ...d.days.map((x) => x.min));
  box.innerHTML = `${rec ? `<button type="button" class="ensb-today" data-lid="${rec.id}" style="background-image:linear-gradient(90deg,rgba(40,26,14,.92) 45%,rgba(40,26,14,.25)),url(api/english/asset/${rec.id}/bg.jpg)">
      <small>小新今天推荐</small><b>${esc(rec.name)} · ${esc(rec.zh)}</b><span>${esc(d.recommend.why)}</span><em>▶ 开始练</em></button>` : ""}
    <div class="ensb-nums"><div><b>${d.total.mastered}/${d.lessons.length}</b><small>课掌握了</small></div><div><b>${d.total.sessions}</b><small>次练习</small></div>
      <div><b>${d.streak}</b><small>天连续</small></div><div><b>${d.total.avg ?? "-"}</b><small>平均分</small></div></div>
    <h5>最近两周（分钟）</h5><div class="ensb-week">${d.days.map((x, k) => `<span title="${x.d} ${x.min} 分钟"><i style="height:${x.min ? Math.max(6, (x.min / maxMin) * 54) : 3}px" class="${x.min ? "" : "z"}"></i>${k === 13 ? "今" : x.d.slice(8)}</span>`).join("")}</div>
    <h5>每一课</h5><div class="ensb-wall">${d.lessons.map((L) => `<button type="button" class="l${L.level || 0}" data-lid="${L.id}" title="${esc(L.zh)} · ${ENS_LV[L.level || 0]}">
      <img src="api/english/asset/${L.id}/bg.jpg" alt="" loading="lazy"><span>${esc(L.zh)}</span><i>${(L.level === 3 ? "👑" : "")}${L.due && L.level ? ` ${L.due.slice(5)}` : ""}</i></button>`).join("")}</div>
    ${d.weak.length ? `<h5>总是读不准的词</h5><div class="ensb-weak">${d.weak.map((w) => `<span><b>${esc(w.w)}</b><small>${Math.round(w.last)} 分</small>
      <button type="button" data-word="${esc(w.w)}" aria-label="听标准读音">🔊</button><button type="button" data-mine="${w.sid}/${w.i}/${w.n}" aria-label="听她读的">👧</button></span>`).join("")}</div>` : ""}`;
  box.addEventListener("click", (e) => {
    const t = e.target.closest("[data-lid],[data-word],[data-mine]");
    if (!t) return;
    if (t.dataset.lid) ensOpen(t.dataset.lid);
    if (t.dataset.word) ensWordCard(t.dataset.word);
    if (t.dataset.mine) { const [sid, i, n] = t.dataset.mine.split("/"); ensPlay(ensUrl(`api/english/session/${sid}/audio/${i}/${n}.wav`)); }
  });
  row.querySelector(".bubble").insertBefore(box, row.querySelector(".bubble .text"));
  requestAnimationFrame(() => typeof revealRow === "function" && revealRow(row));
}
