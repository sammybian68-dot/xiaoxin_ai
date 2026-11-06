"use strict";
// 翻唱（server/sing/）：
//   录唱歌声音：工具 record_singing_voice 的结果带 [sing-rec:名字] → 全屏录音：怎么唱的提示 → 3、2、1 → 30 秒圆环倒计时
//   （10 秒后能提前结束）→ 试听 / 重录 / 存。点「开始录」时小新先闭嘴、自由对话挂断、待命让出麦克风；关掉录音界面恢复待命。
//   录音用 app.js 的 Recorder（raw：不降噪、不消回声、不自动增益，声卡原生采样率）。
//   音乐卡片：小新后台发来的 via=sing 消息（m.sing）→ 仿 QQ 音乐：转动的唱片 + 封面、播放 / 暂停、能拖的进度条、
//   滚动歌词（当前句高亮，点一句跳过去）、歌词快慢微调（按歌记在本机）。点唱片全屏。同一时间只放一首。
//   设置 → 唱歌：唱歌服务开没开、QQ 音乐扫码登录、录过的声音（试听、重录、删）。

const sing = { gen: 0, rec: null, timer: 0, blob: null, url: "", name: "", secs: 0, top: 0, levels: [], wasStandby: false,
  preview: null, cur: null };
const SING_SECS = 30, SING_MIN = 10;
const SING_TIPS = [
  "唱一首你熟悉、有高有低的歌，流行歌最好，儿歌也行",
  "用平时唱歌的声音，放开唱，大声一点",
  "不用在调上，不用唱准，也不用唱完整",
  "安静的房间，不要放伴奏，离屏幕一臂远",
];
const SING_CHEER = ["开始唱吧～", "唱得真好，继续！", "声音再大一点点", "保持住，快好啦", "最后几秒，唱完这句"];
const singSleep = (ms) => new Promise((r) => setTimeout(r, ms));
const singTime = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, "0")}`;

function singBusy() {
  return !!(sing.rec || ($("sgr") && !$("sgr").hidden) || (sing.cur?.audio && !sing.cur.audio.paused));
}

// ---------------------------------------------------------------- 录唱歌的声音

function singRecOpen(name) {
  if ($("settings")?.open) $("settings").close();   // 设置是置顶的 dialog，不关会盖住录音界面
  sing.name = name; sing.gen++;
  singDropTake();
  let el = $("sgr");
  if (!el) {
    el = document.createElement("div");
    el.id = "sgr"; el.className = "sgr"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "录唱歌的声音");
    document.body.appendChild(el);
    el.addEventListener("click", singRecClick);
  }
  el.hidden = false;
  el.innerHTML = `<div class="sgr-card">
    <button type="button" class="sgr-x" data-s="close" aria-label="关闭">✕</button>
    <h2>录「<span>${esc(name)}</span>」唱歌的声音</h2>
    <div class="sgr-ring"><svg viewBox="0 0 120 120" aria-hidden="true"><circle class="bg" cx="60" cy="60" r="54"/>
      <circle class="fg" id="sgr-arc" cx="60" cy="60" r="54"/></svg>
      <div class="sgr-mid"><b id="sgr-num">${SING_SECS}</b><small id="sgr-unit">秒</small></div></div>
    <canvas id="sgr-wave" class="sgr-wave" width="520" height="56" aria-hidden="true"></canvas>
    <p class="sgr-say" id="sgr-say" aria-live="polite">准备好了就点「开始录」</p>
    <ul class="sgr-tips">${SING_TIPS.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
    <div class="sgr-btns" id="sgr-btns"></div></div>`;
  singPhase("intro");
}

function singPhase(p) {
  const el = $("sgr");
  if (!el) return;
  el.dataset.phase = p;
  const b = {
    intro: '<button type="button" class="go" data-s="start">● 开始录</button><button type="button" data-s="close">取消</button>',
    count: '<button type="button" data-s="again">取消</button>',
    rec: `<button type="button" class="go" data-s="stop" id="sgr-stop" disabled>■ 唱好了</button><button type="button" data-s="again">重来</button>`,
    review: '<button type="button" data-s="listen" id="sgr-listen">▶ 试听</button><button type="button" data-s="start">↺ 重录</button>'
      + '<button type="button" class="go" data-s="save">✓ 存好</button>',
    saving: '<button type="button" class="go" disabled>存着呢…</button>',
  }[p];
  $("sgr-btns").innerHTML = b;
  if (p === "intro") { singRing(0); $("sgr-num").textContent = SING_SECS; $("sgr-unit").textContent = "秒"; }
}

function singRing(frac) {
  const arc = $("sgr-arc");
  if (arc) arc.style.strokeDashoffset = String(339.3 * Math.min(1, Math.max(0, frac)));
}

async function singRecClick(e) {
  const s = e.target.closest("[data-s]")?.dataset.s;
  if (s === "close") singRecClose();
  else if (s === "start") singRecStart();
  else if (s === "again") { await singRecAbort(); singPhase("intro"); $("sgr-say").textContent = "准备好了就点「开始录」"; }
  else if (s === "stop") singRecStop();
  else if (s === "listen") singListen();
  else if (s === "save") singSave();
}

// 开始录：小新闭嘴、自由对话挂断、待命让出麦克风（来电也是这样，见 app.js 的 call）
function singQuiet() {
  if (typeof pracHushXiaoxin === "function") pracHushXiaoxin(); else stopSpeaking();
  sing.cur?.audio?.pause();
  if (talk.on) hangUp(false, true);
  if (standby.on && native?.standby) { sing.wasStandby = true; setStandby(false); }
}
function singResume() {
  if (sing.wasStandby) { sing.wasStandby = false; setStandby(true); }
}

async function singRecStart() {
  const g = ++sing.gen;
  singDropTake();
  singQuiet();
  singPhase("count");
  sing.levels = []; sing.top = 0;
  const wave = $("sgr-wave");
  sing.rec = new Recorder((peak) => {
    sing.levels.push(peak);
    if ($("sgr")?.dataset.phase === "rec") { sing.top = Math.max(sing.top, peak); drawLevels(wave, sing.levels, "#31c27c"); }
  }, null, { raw: true });
  try { await sing.rec.start(); } catch (err) {
    sing.rec = null;
    toast("拿不到麦克风：" + err.message);
    singPhase("intro");
    return;
  }
  for (const n of [3, 2, 1]) {   // 麦克风先开着，倒数完把这段丢掉：开麦要一会儿，不然开头那句会被吃掉
    if (g !== sing.gen) return;
    $("sgr-num").textContent = n; $("sgr-unit").textContent = "准备"; $("sgr-say").textContent = "深吸一口气…";
    await singSleep(850);
  }
  if (g !== sing.gen || !sing.rec) return;
  sing.rec.chunks = []; sing.levels = [];
  singPhase("rec");
  $("sgr-unit").textContent = "秒";
  const t0 = performance.now();
  clearInterval(sing.timer);
  sing.timer = setInterval(() => {
    if (g !== sing.gen) { clearInterval(sing.timer); return; }
    const s = (performance.now() - t0) / 1000;
    $("sgr-num").textContent = Math.max(0, Math.ceil(SING_SECS - s));
    singRing(s / SING_SECS);
    $("sgr-say").textContent = SING_CHEER[Math.min(SING_CHEER.length - 1, Math.floor(s / (SING_SECS / SING_CHEER.length)))];
    if ($("sgr-stop")) $("sgr-stop").disabled = s < SING_MIN;
    if (s >= SING_SECS) singRecStop();
  }, 100);
}

async function singRecAbort() {
  sing.gen++;
  clearInterval(sing.timer);
  const rec = sing.rec;
  sing.rec = null;
  if (rec) await rec.stop().catch(() => {});
}

async function singRecStop() {
  clearInterval(sing.timer);
  const rec = sing.rec;
  if (!rec) return;
  sing.rec = null;
  sing.secs = rec.seconds();
  const blob = await rec.stop();
  if (sing.secs < SING_MIN) { toast(`只录了 ${Math.round(sing.secs)} 秒，至少唱 ${SING_MIN} 秒`); singPhase("intro"); return; }
  sing.blob = blob;
  sing.url = URL.createObjectURL(blob);
  singPhase("review");
  $("sgr-num").textContent = Math.round(sing.secs); $("sgr-unit").textContent = "秒录好了";
  singRing(1);
  $("sgr-say").textContent = sing.top < 0.04 ? "好像没怎么录到声音，试听一下，小的话靠近一点重录" : "听一下，满意就点「存好」";
}

function singListen() {
  const btn = $("sgr-listen");
  if (sing.preview && !sing.preview.paused) { sing.preview.pause(); return; }
  sing.preview = new Audio(sing.url);
  sing.preview.onplay = () => { if (btn) btn.textContent = "❚❚ 停"; };
  sing.preview.onpause = sing.preview.onended = () => { if (btn) btn.textContent = "▶ 试听"; };
  sing.preview.play().catch((err) => toast("放不了：" + err.message));
}

function singDropTake() {
  sing.preview?.pause(); sing.preview = null;
  if (sing.url) URL.revokeObjectURL(sing.url);
  sing.url = ""; sing.blob = null;
}

async function singSave() {
  if (!sing.blob) return;
  singPhase("saving");
  sing.preview?.pause();
  const kid = state.conv?.kid ? `?kid=${encodeURIComponent(state.conv.kid)}` : "";
  try {
    const r = await api(`api/sing/voices${kid}`, { method: "POST", body: sing.blob,
      headers: { "Content-Type": "audio/wav", "x-name": encodeURIComponent(sing.name) } }).then(json);
    toast(`存好了：${r.name}唱歌的声音（${Math.round(r.seconds)} 秒）`);
    singRecClose();
  } catch (err) {
    toast("没存上：" + err.message);
    singPhase("review");
  }
}

async function singRecClose() {
  await singRecAbort();
  singDropTake();
  if ($("sgr")) $("sgr").hidden = true;
  singResume();
}

// ---------------------------------------------------------------- 聊天里的卡片

function singAction(row, a) {
  const bubble = row.querySelector(".bubble");
  const at = row.querySelector(".bubble .text");
  if (/^失败/.test(a.result || "")) {
    const chip = document.createElement("div");
    chip.className = "action-chip bad";
    chip.textContent = "⚠ " + (a.result || "").split("\n")[0].replace(/^失败：/, "没成功：");
    bubble.insertBefore(chip, at);
    return true;
  }
  if (a.name === "record_singing_voice") {
    const name = /\[sing-rec:([^\]]+)\]/.exec(a.result || "")?.[1];
    if (!name) return false;
    const chip = document.createElement("button");
    chip.type = "button"; chip.className = "action-chip sing-chip";
    chip.textContent = `🎤 录「${name}」唱歌的声音`;
    chip.addEventListener("click", () => singRecOpen(name));
    bubble.insertBefore(chip, at);
    if (a.live) singRecOpen(name);
    return true;
  }
  const chip = document.createElement("div");
  chip.className = "action-chip sing-chip";
  chip.textContent = "🎤 在唱了，大约一分钟，好了会发上来";
  bubble.insertBefore(chip, at);
  return true;
}

function singLrc(text) {
  const lines = [];
  let shift = 0;
  for (const raw of (text || "").split(/\r?\n/)) {
    const off = /^\[offset:\s*(-?\d+)\s*\]/i.exec(raw);
    if (off) { shift = Number(off[1]) / 1000; continue; }
    const tags = [...raw.matchAll(/\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]/g)];
    const words = raw.replace(/\[[^\]]*\]/g, "").replace(/&apos;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&").trim();
    if (!tags.length || !words) continue;
    for (const t of tags) lines.push({ t: Number(t[1]) * 60 + parseFloat(t[2].replace(":", ".")) - shift, text: words });
  }
  return lines.sort((x, y) => x.t - y.t);
}

function singCard(row, m) {
  const s = m.sing;
  if (!s?.file) return;
  const cover = s.cover ? `api/sing/cover/${encodeURIComponent(s.cover)}` : "";
  const c = { s, lines: singLrc(s.lrc), idx: -2, off: Number(localGet("sing.off." + (s.mid || s.file)) || 0), audio: null,
    dragging: false, raf: 0, dur: Number(s.seconds) || 0, el: document.createElement("div"), home: null };
  const el = c.el;
  el.className = "sgc";
  el.innerHTML = `<div class="sgc-bg"${cover ? ` style="background-image:url('${cover}')"` : ""}></div>
    <div class="sgc-head">
      <button type="button" class="sgc-disc" data-c="full" aria-label="全屏播放">
        <span class="sgc-vinyl"><span class="sgc-art">${cover ? `<img src="${cover}" alt="">` : "🎵"}</span></span><i class="sgc-arm" aria-hidden="true"></i></button>
      <div class="sgc-meta"><b class="sgc-title">${esc(s.title)}</b>
        <span class="sgc-sub"><em>${esc(s.singer)}</em> 的 AI 翻唱</span>
        <small>原唱 ${esc(s.artist)}${s.album ? ` · ${esc(s.album)}` : ""}</small></div>
      <button type="button" class="sgc-shrink" data-c="full" aria-label="收起">⌄</button>
    </div>
    <div class="sgc-lrc" data-c="lrc"><div class="sgc-lines">${c.lines.length
      ? c.lines.map((l, i) => `<p data-i="${i}">${esc(l.text)}</p>`).join("") : '<p class="none">这首没有歌词，安心听吧</p>'}</div></div>
    <div class="sgc-bar"><span class="sgc-cur">0:00</span>
      <input type="range" class="sgc-seek" min="0" max="${Math.max(1, Math.round(c.dur * 10))}" value="0" step="1" aria-label="播放进度">
      <span class="sgc-dur">${singTime(c.dur)}</span></div>
    <div class="sgc-ctl">
      <button type="button" class="sgc-off" data-c="off-" title="歌词跑在歌声前面：点这个往后挪">词慢点</button>
      <button type="button" class="sgc-skip" data-c="back" aria-label="后退 10 秒">−10s</button>
      <button type="button" class="sgc-play" data-c="play" aria-label="播放">▶</button>
      <button type="button" class="sgc-skip" data-c="fwd" aria-label="快进 10 秒">+10s</button>
      <button type="button" class="sgc-off" data-c="off+" title="歌词落在歌声后面：点这个往前挪">词快点</button>
    </div>
    <div class="sgc-note" hidden></div>`;
  el._sg = c;
  el.addEventListener("click", (e) => singCardClick(c, e));
  const seek = el.querySelector(".sgc-seek");
  seek.addEventListener("input", () => { c.dragging = true; singSync(c, seek.value / 10); });
  seek.addEventListener("change", () => {
    c.dragging = false;
    singEnsureAudio(c);
    c.audio.currentTime = seek.value / 10;
    singSync(c, seek.value / 10);
  });
  singOffNote(c);
  row.querySelector(".bubble").insertBefore(el, row.querySelector(".bubble .text"));
  requestAnimationFrame(() => { singSync(c, 0); if (typeof revealRow === "function") revealRow(row); });
}

function singCardClick(c, e) {
  const p = e.target.closest(".sgc-lines p[data-i]");
  if (p) {   // 点一句歌词：从这句开始放
    const t = c.lines[Number(p.dataset.i)].t - c.off;
    singEnsureAudio(c);
    c.audio.currentTime = Math.max(0, t);
    singSync(c, Math.max(0, t));
    if (c.audio.paused) singPlay(c);
    return;
  }
  const k = e.target.closest("[data-c]")?.dataset.c;
  if (k === "play") singPlay(c);
  else if (k === "full") singFull(c);
  else if (k === "back" || k === "fwd") {
    singEnsureAudio(c);
    const t = Math.min(Math.max(0, c.audio.currentTime + (k === "fwd" ? 10 : -10)), c.dur || 1e9);
    c.audio.currentTime = t;
    singSync(c, t);
  } else if (k === "off-" || k === "off+") {
    c.off = Math.round((c.off + (k === "off+" ? 0.5 : -0.5)) * 10) / 10;
    localSet("sing.off." + (c.s.mid || c.s.file), String(c.off));
    singOffNote(c);
    c.idx = -2;
    singSync(c, c.audio?.currentTime || 0);
  }
}

function singOffNote(c) {
  const n = c.el.querySelector(".sgc-note");
  n.hidden = !c.off;
  n.textContent = c.off ? `歌词${c.off > 0 ? "提前" : "推后"} ${Math.abs(c.off)} 秒` : "";
}

function singEnsureAudio(c) {
  if (c.audio) return c.audio;
  const a = new Audio(fileUrl({ id: c.s.file }));
  a.preload = "auto";
  c.audio = a;
  const btn = c.el.querySelector(".sgc-play");
  a.addEventListener("loadedmetadata", () => {
    if (!Number.isFinite(a.duration)) return;
    c.dur = a.duration;
    c.el.querySelector(".sgc-seek").max = String(Math.round(c.dur * 10));
    c.el.querySelector(".sgc-dur").textContent = singTime(c.dur);
  });
  a.addEventListener("play", () => {
    c.el.classList.add("on");
    btn.textContent = "❚❚"; btn.setAttribute("aria-label", "暂停");
    cancelAnimationFrame(c.raf);
    const tick = () => { if (a.paused) return; singSync(c, a.currentTime); c.raf = requestAnimationFrame(tick); };
    tick();
  });
  a.addEventListener("pause", () => {
    c.el.classList.remove("on");
    btn.textContent = "▶"; btn.setAttribute("aria-label", "播放");
    cancelAnimationFrame(c.raf);
  });
  a.addEventListener("waiting", () => c.el.classList.add("buf"));
  a.addEventListener("playing", () => c.el.classList.remove("buf"));
  a.addEventListener("ended", () => { a.currentTime = 0; singSync(c, 0); });
  a.addEventListener("error", () => { c.el.classList.remove("on", "buf"); toast("这首歌放不了，可能文件没了"); });
  return a;
}

function singPlay(c) {
  const a = singEnsureAudio(c);
  if (!a.paused) { a.pause(); return; }
  if (sing.cur && sing.cur !== c) sing.cur.audio?.pause();
  sing.cur = c;
  stopSpeaking();   // 小新正在念「做好了」：先停
  a.play().catch((err) => toast("放不了：" + err.message));
}

function singSync(c, t) {
  const el = c.el;
  el.querySelector(".sgc-cur").textContent = singTime(t);
  const seek = el.querySelector(".sgc-seek");
  if (!c.dragging) seek.value = String(Math.round(t * 10));
  seek.style.setProperty("--p", `${(Number(seek.value) / Math.max(1, Number(seek.max))) * 100}%`);
  if (!c.lines.length) return;
  const now = t + c.off;
  let i = -1;
  for (let k = 0; k < c.lines.length && c.lines[k].t <= now; k++) i = k;
  if (i === c.idx) return;
  c.idx = i;
  const box = el.querySelector(".sgc-lrc"), list = el.querySelector(".sgc-lines");
  list.querySelector("p.cur")?.classList.remove("cur");
  const p = list.querySelector(`p[data-i="${Math.max(0, i)}"]`);
  if (i >= 0) p?.classList.add("cur");
  if (p) list.style.transform = `translateY(${box.clientHeight / 2 - p.offsetTop - p.offsetHeight / 2}px)`;
}

// 全屏：把卡片整个挪到 body 下（气泡里有 transform，fixed 定位会跟着气泡走），收起时放回原处
function singFull(c) {
  const el = c.el;
  if (el.classList.contains("full")) {
    el.classList.remove("full");
    c.home?.replaceWith(el);
    c.home = null;
    document.removeEventListener("keydown", c.esc);
  } else {
    c.home = document.createComment("sgc");
    el.replaceWith(c.home);
    document.body.appendChild(el);
    el.classList.add("full");
    c.esc = (e) => { if (e.key === "Escape") singFull(c); };
    document.addEventListener("keydown", c.esc);
  }
  c.idx = -2;
  requestAnimationFrame(() => singSync(c, c.audio?.currentTime || 0));
}

// ---------------------------------------------------------------- 设置 → 唱歌

const singQr = { sid: "", timer: 0, kind: "mobile" };
const SING_QR_TIP = {
  mobile: "打开 QQ 音乐 App，点首页的「扫一扫」扫这个码",
  qq: "打开手机 QQ，右上角「+」→「扫一扫」，要用开了会员的那个号",
  wx: "打开微信「扫一扫」（QQ 音乐会员要绑在这个微信上）",
};
const singDate = (ts) => new Date(ts * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

async function loadSingSettings() {
  let d;
  try { d = await api("api/sing").then(json); } catch (err) { $("sing-svc").textContent = err.message; return; }
  $("sing-svc").textContent = d.service ? "开着（gpu2 · SoulX-Singer-SVC）" : "没开：gpu2 上的 SoulX-Singer 服务没起来";
  singQqStatus(d.qq);
  singVoiceList(d.voices);
}

function singQqStatus(q) {
  const parent = state.me?.role !== "kid";
  let t;
  if (!q.installed) t = "QQ 音乐组件没装（重新部署一次 deploy/gpu2.sh）";
  else if (!q.logged_in) t = "还没登录，点不了歌";
  else if (q.expired) t = `登录过期了（${q.account || ""}），重新扫一次码`;
  else t = `已登录 ${q.account || ""}${q.expires_at ? ` · 这次的凭证到 ${singDate(q.expires_at)}，会自动续` : ""}`;
  if (!parent && (!q.logged_in || q.expired)) t += "（请爸爸妈妈来扫码）";
  $("sing-qq").textContent = t;
  $("sing-qq-login").hidden = !parent || !q.installed;
  $("sing-qq-login").textContent = q.logged_in && !q.expired ? "重新扫码" : "扫码登录";
  $("sing-qq-logout").hidden = !parent || !q.logged_in;
}

function singVoiceList(list) {
  const box = $("sing-voices");
  if (!list.length) { box.innerHTML = '<p class="voice-tip">还没有人录。</p>'; return; }
  box.innerHTML = list.map((v) => `<div class="row" data-v="${esc(v.id)}" data-name="${esc(v.name)}">
    <label><b>${esc(v.name)}</b><small>${Math.round(v.seconds)} 秒 · ${singDate(v.created)} 录的</small></label>
    <span class="sing-btns"><button type="button" data-a="play">▶ 试听</button><button type="button" data-a="redo">重录</button>
      <button type="button" class="danger-btn" data-a="del">删</button></span></div>`).join("");
}

$("sing-voices")?.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-a]");
  const rowEl = b?.closest("[data-v]");
  if (!b || !rowEl) return;
  const vid = rowEl.dataset.v;
  if (b.dataset.a === "play") {
    if (sing.preview && !sing.preview.paused) { sing.preview.pause(); if (sing.previewBtn === b) return; }
    sing.preview = new Audio(`api/sing/voices/${vid}/audio`);
    sing.previewBtn = b;
    sing.preview.onplay = () => { b.textContent = "❚❚ 停"; };
    sing.preview.onpause = sing.preview.onended = () => { b.textContent = "▶ 试听"; };
    sing.preview.play().catch((err) => toast("放不了：" + err.message));
  } else if (b.dataset.a === "redo") {
    singRecOpen(rowEl.dataset.name);
  } else if (b.dataset.a === "del") {
    if (b.dataset.sure !== "1") {   // 点两下才删（pad 的 WebView 不弹 confirm）
      b.dataset.sure = "1"; b.textContent = "再点删除";
      setTimeout(() => { b.dataset.sure = ""; b.textContent = "删"; }, 3000);
      return;
    }
    try { await api(`api/sing/voices/${vid}`, { method: "DELETE" }); toast("删掉了"); loadSingSettings(); } catch (err) { toast(err.message); }
  }
});

$("sing-new")?.addEventListener("click", () => {
  const name = $("sing-new-name").value.trim();
  if (!name) { toast("先写上是谁的声音"); $("sing-new-name").focus(); return; }
  $("sing-new-name").value = "";
  singRecOpen(name);
});

$("sing-qq-login")?.addEventListener("click", () => singQrStart(singQr.kind));
$("sing-qr-kind")?.addEventListener("click", (e) => {
  const k = e.target.closest("[data-k]")?.dataset.k;
  if (k && k !== singQr.kind) singQrStart(k);
});
$("sing-qr-cancel")?.addEventListener("click", () => singSettingsLeave());
$("sing-qq-logout")?.addEventListener("click", async (e) => {
  const b = e.currentTarget;
  if (b.dataset.sure !== "1") {
    b.dataset.sure = "1"; b.textContent = "再点退出";
    setTimeout(() => { b.dataset.sure = ""; b.textContent = "退出"; }, 3000);
    return;
  }
  try { await api("api/sing/qq", { method: "DELETE" }); toast("QQ 音乐退出了"); loadSingSettings(); } catch (err) { toast(err.message); }
});

async function singQrStart(kind) {
  singQr.kind = kind;
  for (const b of document.querySelectorAll("#sing-qr-kind [data-k]")) b.setAttribute("aria-selected", String(b.dataset.k === kind));
  clearTimeout(singQr.timer);
  $("sing-qr").hidden = false;
  $("sing-qr-img").hidden = true;
  $("sing-qr-wait").hidden = false;
  $("sing-qr-wait").textContent = "二维码生成中…";
  $("sing-qr-tip").textContent = "";
  try {
    const d = await api("api/sing/qq/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }) }).then(json);
    singQr.sid = d.sid;
    singQrShow(d);
  } catch (err) {
    $("sing-qr-wait").textContent = "没拿到二维码";
    $("sing-qr-tip").textContent = err.message;
  }
}

function singQrShow(d) {
  const img = $("sing-qr-img"), wait = $("sing-qr-wait"), tip = $("sing-qr-tip");
  const live = ["starting", "waiting", "scanned"].includes(d.state);
  if (d.qr) { img.src = d.qr; img.hidden = false; wait.hidden = true; }
  img.classList.toggle("scanned", d.state === "scanned");
  if (d.state === "waiting") tip.textContent = SING_QR_TIP[singQr.kind] + "。二维码 3 分钟内有效";
  else if (d.state === "scanned") tip.textContent = "扫好了，在手机上点「确认登录」";
  else if (d.state === "done") {
    singQr.sid = "";
    $("sing-qr").hidden = true;
    toast("QQ 音乐登录好了，可以点歌了");
    api("api/sing/qq?fresh=1").then(json).then(singQqStatus).catch(() => {});
  } else if (d.state === "refused") tip.textContent = "手机上点了拒绝。要登录就再点一次「扫码登录」";
  else if (d.state === "timeout") { img.hidden = true; wait.hidden = false; wait.textContent = "二维码过期了"; tip.textContent = "再点一次「扫码登录」"; }
  else if (!live) { img.hidden = true; wait.hidden = false; wait.textContent = "没登上"; tip.textContent = d.detail || "再试一次"; }
  if (live && singQr.sid) {
    const sid = singQr.sid;
    singQr.timer = setTimeout(async () => {
      if (singQr.sid !== sid) return;
      try { singQrShow(await api(`api/sing/qq/login/${sid}`).then(json)); } catch (_) { singQrShow({ state: "waiting" }); }
    }, 1500);
  } else singQr.sid = "";
}

function singSettingsLeave() {
  sing.preview?.pause();
  clearTimeout(singQr.timer);
  if (!singQr.sid) { if ($("sing-qr")) $("sing-qr").hidden = true; return; }
  singQr.sid = "";
  $("sing-qr").hidden = true;
  api("api/sing/qq/login", { method: "DELETE" }).catch(() => {});
}
