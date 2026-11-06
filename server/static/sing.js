"use strict";
// 翻唱（server/sing/）：
//   录唱歌声音：工具 record_singing_voice 的结果带 [sing-rec:名字] → 全屏录音：右边挑一首跟着唱的歌（出歌词、能先听原唱）
//   → 3、2、1 → 30 秒圆环倒计时，歌词一句句亮
//   （10 秒后能提前结束）→ 试听 / 重录 / 存。点「开始录」时小新先闭嘴、自由对话挂断、待命让出麦克风；关掉录音界面恢复待命。
//   录音用 app.js 的 Recorder（raw：不降噪、不消回声、不自动增益，声卡原生采样率）。
//   音乐卡片：小新后台发来的 via=sing 消息（m.sing）→ 仿 QQ 音乐：转动的唱片 + 封面、播放 / 暂停、能拖的进度条、
//   滚动歌词（当前句高亮，点一句跳过去）、歌词快慢微调（按歌记在本机）。点唱片全屏。同一时间只放一首。
//   设置 → 唱歌：唱歌服务开没开、QQ 音乐扫码登录、录过的声音（试听、重录、删）。

const sing = { gen: 0, rec: null, timer: 0, blob: null, url: "", name: "", secs: 0, top: 0, levels: [], wasStandby: false,
  preview: null, cur: null,
  practice: null, grp: "", song: null, orig: null, lyIdx: -1, lyBase: 0, lyRaf: 0 };
const SING_SECS = 30, SING_MIN = 10;
const SING_LEAD_S = 0.4;   // 听原唱从第一句前一点放，听得到起音
const SING_TIPS = [
  "唱一首你熟悉、有高有低、能拖长音的歌，别选说唱",
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
  sing.song = null; sing.grp = "";
  singOrigStop();
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
    <div class="sgr-body">
      <div class="sgr-main">
        <div class="sgr-ring"><svg viewBox="0 0 120 120" aria-hidden="true"><circle class="bg" cx="60" cy="60" r="54"/>
          <circle class="fg" id="sgr-arc" cx="60" cy="60" r="54"/></svg>
          <div class="sgr-mid"><b id="sgr-num">${SING_SECS}</b><small id="sgr-unit">秒</small></div></div>
        <canvas id="sgr-wave" class="sgr-wave" width="520" height="56" aria-hidden="true"></canvas>
        <p class="sgr-say" id="sgr-say" aria-live="polite">准备好了就点「开始录」</p>
        <div class="sgr-btns" id="sgr-btns"></div>
      </div>
      <section class="sgr-song" id="sgr-song" aria-label="跟着唱的歌"></section>
    </div></div>`;
  singPhase("intro");
  const last = localGet("sing.song." + name);
  if (last === "own") singOwn();
  else if (last && /^[a-z]+$/.test(last)) singLoadSong(last);
  else singChooser();
}

// ---------------------------------------------------------------- 跟着唱的歌（server/sing/practice.py）
// 右边一栏：先挑一首（儿歌 / 流行 / 爸爸妈妈那时候），出这首 30 秒出头的那段歌词；能先听这段原唱找调。
// 录的时候不放原唱（会混进录音），歌词按原唱的快慢一句句亮；唱快了慢了点一下正在唱的那句就对上。唱完一遍从头再来。

function singGroupFor(name) {
  return /爸|妈|爷|奶|姥|公|婆|叔|姨|舅|姑|伯/.test(name) ? "pop" : "kids";
}

async function singChooser() {
  singOrigStop();
  sing.song = null;
  const box = $("sgr-song");
  if (!box) return;
  if (!sing.practice) {
    box.innerHTML = '<p class="sgr-song-wait">歌单拿着呢…</p>';
    try { sing.practice = await api("api/sing/practice").then(json); } catch (err) {
      box.innerHTML = `<p class="sgr-song-wait">歌单没拿到：${esc(err.message)}</p>`;
      return;
    }
  }
  const p = sing.practice;
  sing.grp = sing.grp || singGroupFor(sing.name);
  box.innerHTML = `<h3>挑一首会唱的歌，跟着歌词唱</h3>
    <div class="sgr-tabs" role="tablist">${p.groups.map((g) => `<button type="button" role="tab" data-s="grp" data-g="${g.id}"
      aria-selected="${g.id === sing.grp}">${esc(g.name)}</button>`).join("")}</div>
    <div class="sgr-chips">${p.songs.filter((s) => s.group === sing.grp).map((s) => `<button type="button" data-s="song" data-id="${s.id}">
      <b>${esc(s.title)}</b>${s.artist ? `<small>${esc(s.artist)}</small>` : ""}</button>`).join("")}
      <button type="button" class="own" data-s="own"><b>🎵 自己唱别的</b><small>不看歌词</small></button></div>`;
}

function singOwn() {
  singOrigStop();
  sing.song = null;
  localSet("sing.song." + sing.name, "own");
  const box = $("sgr-song");
  if (!box) return;
  box.innerHTML = `<h3>自己挑一首唱</h3><ul class="sgr-tips">${SING_TIPS.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
    <button type="button" class="sgr-link" data-s="pick">← 还是看着歌词唱</button>`;
}

async function singLoadSong(id) {
  singOrigStop();
  const box = $("sgr-song");
  if (!box) return;
  const g = sing.gen;
  box.innerHTML = '<p class="sgr-song-wait">歌词拿着呢…</p>';
  let d;
  try { d = await api(`api/sing/practice/${id}`).then(json); } catch (err) {
    toast(err.message);
    singChooser();
    return;
  }
  if (g !== sing.gen || !$("sgr-song")) return;
  sing.song = d;
  sing.lyIdx = -1;
  localSet("sing.song." + sing.name, id);
  box.innerHTML = `<div class="sgr-song-head"><div><b>《${esc(d.title)}》</b><small>${esc(d.artist)}</small></div>
      ${d.listen ? '<button type="button" data-s="orig" id="sgr-orig">▶ 先听原唱</button>' : ""}
      <button type="button" data-s="pick">换一首</button></div>
    <div class="sgr-ly" id="sgr-ly">${d.lines.map((x, i) => `<p data-s="line" data-i="${i}">${esc(x.text)}</p>`).join("")}</div>
    <p class="sgr-ly-tip">${d.listen ? "不会起调就先听一遍原唱；" : ""}录的时候歌词会一句句亮，唱快了慢了点一下正在唱的那句</p>`;
  singLyAt(0);
}

// 亮第几句：t 是从这段开头算的秒
function singLyAt(t) {
  const d = sing.song, box = $("sgr-ly");
  if (!d || !box) return;
  let i = 0;
  while (i + 1 < d.lines.length && d.lines[i + 1].t <= t) i++;
  if (i === sing.lyIdx) return;
  sing.lyIdx = i;
  box.querySelectorAll("p.cur").forEach((p) => p.classList.remove("cur"));
  const p = box.children[i];
  if (!p) return;
  p.classList.add("cur");
  box.scrollTo({ top: p.offsetTop - box.clientHeight / 2 + p.clientHeight / 2, behavior: "smooth" });
}

// 录音时的歌词时钟：从 base 起算；唱完这段（最后一句再留 4 秒）从头再来
function singLyStart() {
  const d = sing.song;
  cancelAnimationFrame(sing.lyRaf);
  if (!d?.lines.length) return;
  sing.lyBase = performance.now();
  sing.lyIdx = -1;
  const span = d.lines[d.lines.length - 1].t + 4;
  const tick = () => {
    if ($("sgr")?.dataset.phase !== "rec") return;
    let t = (performance.now() - sing.lyBase) / 1000;
    if (t >= span) { sing.lyBase = performance.now(); t = 0; }
    singLyAt(t);
    sing.lyRaf = requestAnimationFrame(tick);
  };
  tick();
}

// 点一句：录的时候 = 「我正唱到这句」，歌词时钟对过来；没在录 = 只是亮一下
function singLyTap(i) {
  const d = sing.song;
  if (!d?.lines[i]) return;
  if ($("sgr")?.dataset.phase === "rec") { sing.lyBase = performance.now() - d.lines[i].t * 1000; sing.lyIdx = -1; }
  else if (sing.orig && !sing.orig.paused) { sing.orig.currentTime = Math.max(0, d.start + d.lines[i].t - SING_LEAD_S); }
  else singLyAt(d.lines[i].t);
}

function singOrig() {
  const d = sing.song;
  if (!d) return;
  if (sing.orig && !sing.orig.paused) { singOrigStop(); return; }
  if (typeof pracHushXiaoxin === "function") pracHushXiaoxin(); else stopSpeaking();
  sing.cur?.audio?.pause();
  const btn = $("sgr-orig");
  const a = sing.orig = new Audio(`api/sing/practice/${d.id}/audio`);
  a.preload = "auto";
  if (btn) btn.textContent = "加载中…";
  a.addEventListener("loadedmetadata", () => {
    a.currentTime = Math.max(0, d.start - SING_LEAD_S);
    a.play().catch((err) => { toast("放不了：" + err.message); singOrigStop(); });
  }, { once: true });
  a.addEventListener("playing", () => { if (btn && sing.orig === a) btn.textContent = "❚❚ 停"; });
  a.addEventListener("timeupdate", () => {
    if (sing.orig !== a) return;
    singLyAt(a.currentTime - d.start);
    if (a.currentTime >= d.end) singOrigStop();
  });
  a.addEventListener("error", () => { if (sing.orig === a) { toast("原唱放不了，可能 QQ 音乐登录过期了"); singOrigStop(); } });
}

function singOrigStop() {
  const a = sing.orig;
  sing.orig = null;
  if (a) { a.pause(); a.removeAttribute("src"); a.load(); }
  const btn = $("sgr-orig");
  if (btn) btn.textContent = "▶ 先听原唱";
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
  if (p !== "rec") cancelAnimationFrame(sing.lyRaf);
  if (p === "intro" || p === "count") { sing.lyIdx = -1; singLyAt(0); }
  if (p === "intro") { singRing(0); $("sgr-num").textContent = SING_SECS; $("sgr-unit").textContent = "秒"; }
}

function singRing(frac) {
  const arc = $("sgr-arc");
  if (arc) arc.style.strokeDashoffset = String(339.3 * Math.min(1, Math.max(0, frac)));
}

async function singRecClick(e) {
  const b = e.target.closest("[data-s]");
  const s = b?.dataset.s;
  if (s === "close") singRecClose();
  else if (s === "start") singRecStart();
  else if (s === "again") { await singRecAbort(); singPhase("intro"); $("sgr-say").textContent = "准备好了就点「开始录」"; }
  else if (s === "stop") singRecStop();
  else if (s === "listen") singListen();
  else if (s === "save") singSave();
  else if (s === "grp") { sing.grp = b.dataset.g; singChooser(); }
  else if (s === "song") singLoadSong(b.dataset.id);
  else if (s === "own") singOwn();
  else if (s === "pick") singChooser();
  else if (s === "orig") singOrig();
  else if (s === "line") singLyTap(Number(b.dataset.i));
}

// 开始录：小新闭嘴、自由对话挂断、待命让出麦克风（来电也是这样，见 app.js 的 call）
function singQuiet() {
  if (typeof pracHushXiaoxin === "function") pracHushXiaoxin(); else stopSpeaking();
  sing.cur?.audio?.pause();
  singOrigStop();
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
  singLyStart();
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
  singOrigStop();
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
  const c = { s, mid: m.id, lines: singLrc(s.lrc), idx: -2, off: Number(localGet("sing.off." + (s.mid || s.file)) || 0), audio: null,
    dragging: false, raf: 0, dur: Number(s.seconds) || 0, el: document.createElement("div"), home: null, ver: "ai", kara: null };
  const el = c.el;
  el.className = "sgc";
  el.innerHTML = `<div class="sgc-bg"${cover ? ` style="background-image:url('${cover}')"` : ""}></div>
    <div class="sgc-head">
      <button type="button" class="sgc-disc" data-c="full" aria-label="全屏播放">
        <span class="sgc-vinyl"><span class="sgc-art">${cover ? `<img src="${cover}" alt="">` : "🎵"}</span></span><i class="sgc-arm" aria-hidden="true"></i></button>
      <div class="sgc-meta"><b class="sgc-title">${esc(s.title)}</b>
        <span class="sgc-sub"><em>${esc(s.singer)}</em> 的 AI 翻唱</span>
        <small>原唱 ${esc(s.artist)}${s.album ? ` · ${esc(s.album)}` : ""}</small>
        <div class="sgc-ver" role="tablist" aria-label="听哪个版本" hidden></div>
        <div class="sgc-acts">${navigator.mediaDevices?.getUserMedia ? '<button type="button" class="sgc-me" data-c="k-open">🎤 我来唱</button>' : ""}
          ${singWxBridge() ? '<button type="button" class="sgc-wx" data-c="wx">发到微信</button>' : ""}</div></div>
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
    <div class="sgc-kara" hidden><div class="sgk-who"></div><p class="sgk-say"></p>
      <div class="sgk-btns"><button type="button" class="sgk-x" data-c="k-x">不唱了</button><button type="button" class="sgk-go" data-c="k-go">● 开始唱</button></div></div>
    <div class="sgc-note" hidden></div>`;
  el._sg = c;
  singVerRender(c);
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
  if (c.kara && p) { singKaraLine(c, Number(p.dataset.i)); return; }
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
  else if (k === "wx") singShareOpen(c);
  else if (k === "v-ai" || k === "v-real") singVer(c, k.slice(2));
  else if (k === "k-open") singKaraOpen(c);
  else if (k === "k-x") singKaraClose(c);
  else if (k === "k-go") singKaraGo(c);
  else if (k === "k-who") { c.kara.who = e.target.closest("[data-who]").dataset.who; singKaraRender(c); }
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
  const a = new Audio(fileUrl({ id: c.ver === "real" && c.s.real ? c.s.real.file : c.s.file }));
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
    if (c.kara) singKaraClose(c);
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

// ---------------------------------------------------------------- 跟着伴奏唱、AI / 真人对比
// 「我来唱」：卡片进全屏，放 AI 翻唱那首去掉人声的伴奏（/api/sing/karaoke，和 AI 版一个调）、滚歌词，同时录原声；
// 唱完传上去（/api/sing/real）对齐混成真人版，和 AI 版同一条时间轴，卡片上「AI 唱 / 亲自唱」切换时停在同一句。
// 录音不开回声消除：外放的伴奏也录进去，服务端拿它对出延迟（戴耳机也能唱，按常见延迟对）。

const SING_K_RATE = 24000;
const SING_K_PREROLL = 3;   // 从中间某句开始唱：伴奏提前几秒放，好跟上拍子
const SING_K_MIN = 5;

function singVerRender(c) {
  const box = c.el.querySelector(".sgc-ver"), r = c.s.real;
  box.hidden = !r;
  if (!r) c.ver = "ai";
  c.el.querySelector(".sgc-sub").innerHTML = c.ver === "real" ? `<em>${esc(r.singer)}</em> 亲自唱` : `<em>${esc(c.s.singer)}</em> 的 AI 翻唱`;
  if (!r) { box.innerHTML = ""; return; }
  box.innerHTML = `<button type="button" role="tab" data-c="v-ai" aria-selected="${c.ver !== "real"}">AI 唱</button>`
    + `<button type="button" role="tab" data-c="v-real" aria-selected="${c.ver === "real"}">${esc(r.singer)}亲自唱</button>`;
}

/** 换版本：接着放同一个位置；真人版没唱到的地方，跳到开始唱的那句前面一点。 */
function singVer(c, v, play) {
  if ((v === "real" && !c.s.real) || (c.ver === v && c.audio)) return;
  const old = c.audio, t = old ? old.currentTime : 0, playing = play ?? !!(old && !old.paused);
  old?.pause();
  c.audio = null;
  c.ver = v;
  singVerRender(c);
  const r = c.s.real;
  const to = v === "real" && (t < r.start - 2 || t > r.end) ? Math.max(0, r.start - 1) : t;
  const a = singEnsureAudio(c);
  a.currentTime = to;
  c.idx = -2;
  singSync(c, to);
  if (playing) singPlay(c);
}

window.singCardUpdate = (m) => {
  for (const el of document.querySelectorAll(".sgc")) {
    const c = el._sg;
    if (c?.mid !== m.id || c.kara?.phase === "mix") continue;
    c.s = m.sing;
    if (c.ver === "real" && c.audio) { c.audio.pause(); c.audio = null; }   // 真人版换了一份
    singVerRender(c);
  }
};

/** 谁来唱：先是自己（孩子的 pad 用管理员登录：算这个群的孩子），再是 AI 用的那个声音、这个群的孩子。 */
function singKaraWho(c) {
  const kid = state.kids?.find((k) => k.id === state.conv?.kid)?.name || "";
  const me = state.me?.kind === "admin" ? kid : state.kids?.find((k) => k.id === state.me?.id)?.name || state.me?.name || "";
  return [...new Set([me, c.s.singer, kid].filter(Boolean))];
}

async function singKaraOpen(c) {
  c.audio?.pause();
  if (!c.el.classList.contains("full")) singFull(c);
  c.el.classList.add("kara");
  const k = c.kara = { phase: "prep", gen: 0, from: -1, who: singKaraWho(c)[0] || c.s.singer, samples: [], acc: null, rec: null };
  singKaraRender(c);
  try {
    for (;;) {
      const st = await api("api/sing/karaoke", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: c.s.file }) }).then(json);
      if (c.kara !== k) return;
      if (st.state === "ready") { k.url = st.url; break; }
      if (st.state === "error") throw new Error(st.error || "伴奏没做出来");
      await singSleep(1000);
      if (c.kara !== k) return;
    }
  } catch (err) {
    if (c.kara !== k) return;
    k.phase = "fail"; k.msg = err.message;
    singKaraRender(c);
    return;
  }
  k.acc = new Audio(new URL(k.url, location.href).href);
  k.acc.preload = "auto";
  k.phase = "ready";
  c.idx = -2;
  singSync(c, 0);
  singKaraRender(c);
}

function singKaraLine(c, i) {
  const k = c.kara;
  if (k.phase !== "ready") return;
  k.from = k.from === i ? -1 : i;
  c.idx = -2;
  singSync(c, k.from >= 0 ? Math.max(0, c.lines[i].t - c.off) : 0);
  singKaraRender(c);
}

function singKaraRender(c) {
  const k = c.kara, box = c.el.querySelector(".sgc-kara");
  box.hidden = !k;
  if (!k) return;
  const say = box.querySelector(".sgk-say"), go = box.querySelector(".sgk-go"), who = box.querySelector(".sgk-who");
  const names = singKaraWho(c);
  who.hidden = k.phase !== "ready" || names.length < 2;
  who.innerHTML = names.map((n) => `<button type="button" data-c="k-who" data-who="${esc(n)}" aria-pressed="${n === k.who}">${esc(n)}唱</button>`).join("");
  box.dataset.phase = k.phase;
  go.disabled = !["ready", "rec", "fail"].includes(k.phase);
  go.textContent = k.phase === "rec" ? "■ 唱完了" : k.phase === "fail" ? "再试一次" : "● 开始唱";
  if (k.phase === "prep") say.textContent = "正在把 AI 唱的人声去掉，做成伴奏…";
  else if (k.phase === "ready") say.textContent = k.from >= 0
    ? `从「${c.lines[k.from].text}」开始唱（再点一下这句就从头唱）` : "从头开始唱；点一句歌词，就从那句开始";
  else if (k.phase === "count") say.textContent = `${k.count}… 准备好`;
  else if (k.phase === "rec") singKaraClock(c);
  else if (k.phase === "mix") say.textContent = "正在把你的声音和伴奏合在一起…";
  else if (k.phase === "fail") say.textContent = k.msg;
}

function singKaraClock(c) {
  const k = c.kara, secs = k.rec ? k.rec.seconds() : 0;
  const say = c.el.querySelector(".sgk-say"), go = c.el.querySelector(".sgk-go");
  say.textContent = `● 录音中 ${singTime(secs)}　${k.who}跟着伴奏唱，唱到哪儿都可以停`;
  say.style.setProperty("--lv", String(Math.min(1, (k.peak || 0) * 3)));
  go.disabled = secs < SING_K_MIN;
}

async function singKaraGo(c) {
  const k = c.kara;
  if (!k) return;
  if (k.phase === "fail") { singKaraClose(c); singKaraOpen(c); return; }
  if (k.phase === "rec") { singKaraStop(c); return; }
  if (k.phase !== "ready") return;
  const g = ++k.gen;
  singQuiet();
  k.phase = "count"; k.count = 3;
  singKaraRender(c);
  k.rec = new Recorder((peak) => { k.peak = peak; }, null, { raw: true, rate: SING_K_RATE });
  try { await k.rec.start(); } catch (err) {
    k.rec = null;
    toast("拿不到麦克风：" + err.message);
    k.phase = "ready"; singKaraRender(c); singResume();
    return;
  }
  const at = k.from >= 0 ? Math.max(0, c.lines[k.from].t - c.off - SING_K_PREROLL) : 0;
  k.acc.currentTime = at;
  for (const n of [3, 2, 1]) {   // 麦克风先开着，倒数这段丢掉（开麦要一会儿）
    if (g !== k.gen) return;
    k.count = n; singKaraRender(c);
    await singSleep(800);
  }
  if (g !== k.gen || !k.rec) return;
  k.rec.chunks = []; k.samples = [];
  k.phase = "rec";
  singKaraRender(c);
  try { await k.acc.play(); } catch (err) { toast("伴奏放不了：" + err.message); singKaraClose(c); return; }
  const from = k.acc.currentTime;
  k.acc.onended = () => { if (g === k.gen && k.phase === "rec") singKaraStop(c); };
  const tick = () => {
    if (g !== k.gen || k.phase !== "rec") return;
    const t = k.acc.currentTime;
    if (!k.acc.paused && t > from + 0.3) k.samples.push(t - k.rec.seconds());   // 录音第 0 个点对着歌里第几秒
    singSync(c, t);
    singKaraClock(c);
    k.raf = requestAnimationFrame(tick);
  };
  tick();
}

async function singKaraStop(c) {
  const k = c.kara;
  if (!k?.rec || k.phase !== "rec") return;
  if (k.rec.seconds() < SING_K_MIN) { toast(`再多唱一会儿，至少 ${SING_K_MIN} 秒`); return; }
  k.gen++;
  cancelAnimationFrame(k.raf);
  k.acc.pause();
  const rec = k.rec;
  k.rec = null;
  const ss = k.samples.slice().sort((x, y) => x - y);
  let s0 = ss.length ? ss[ss.length >> 1] : 0;
  if (s0 < 0) {   // 伴奏是从头放的，录音比歌开头还早一点：把多出来的那截去掉
    let drop = Math.round(-s0 * rec.rate);
    while (drop > 0 && rec.chunks.length) {
      const ch = rec.chunks[0];
      if (ch.length <= drop) { drop -= ch.length; rec.chunks.shift(); } else { rec.chunks[0] = ch.subarray(drop); drop = 0; }
    }
    s0 = 0;
  }
  const blob = await rec.stop();
  singResume();
  k.phase = "mix";
  singKaraRender(c);
  try {
    const q = `file=${encodeURIComponent(c.s.file)}&s0=${s0.toFixed(3)}&singer=${encodeURIComponent(k.who)}`;
    const r = await api(`api/sing/real?${q}`, { method: "POST", body: blob, headers: { "Content-Type": "audio/wav" } }).then(json);
    c.s = r.sing;
    singKaraClose(c);
    if (c.audio) { c.audio.pause(); c.audio = null; }
    c.ver = "ai";
    singVer(c, "real", true);
    toast("真人版好了，点上面「AI 唱 / 亲自唱」切换着听");
  } catch (err) {
    if (c.kara !== k) return;
    k.phase = "fail"; k.msg = "没合成好：" + err.message;
    singKaraRender(c);
  }
}

function singKaraClose(c) {
  const k = c.kara;
  if (!k) return;
  k.gen++;
  cancelAnimationFrame(k.raf);
  k.acc?.pause();
  if (k.rec) { k.rec.stop().catch(() => {}); k.rec = null; singResume(); }
  c.kara = null;
  c.el.classList.remove("kara");
  singKaraRender(c);
  c.idx = -2;
  singSync(c, c.audio?.currentTime || 0);
}

// ---------------------------------------------------------------- 发到微信
// 整首或者选一段，服务端做成视频（/api/sing/video：封面 + 转唱片 + 滚动歌词），APK 下载进相册「Movies/小新」再拉起微信发。
// 选一段：点一句当开头、再点一句当结尾（结尾那句唱完），也可以拖两头。

const SING_VIDEO_MAX = 299;   // 微信聊天视频最长 5 分钟，和 server/sing/video.py 的 MAX_S 一样
const SING_CLIP_MIN = 3;
const SING_CREDIT = /^[^：:]{0,14}[：:]|^.{0,40} - .{0,40}$/;   // 「词：某某」「泡沫 - 邓紫棋」，同 server/sing/practice.py
const singShare = { c: null, mode: "all", a: 0, b: 0, dur: 0, pick: -1, gen: 0, audio: null, idx: [], ver: "ai" };
const singWxBridge = () => { const b = window.XiaoxinNative || window.XiaoxinParent; return b?.shareVideo ? b : null; };

function singShareOpen(c) {
  const sh = singShare;
  sh.c = c; sh.gen++; sh.pick = -1;
  sh.dur = c.dur || Number(c.s.seconds) || 0;
  sh.a = 0; sh.b = sh.dur;
  c.audio?.pause();
  sh.idx = c.lines.map((l, i) => i).filter((i) => !SING_CREDIT.test(c.lines[i].text));
  $("sgs-lines").innerHTML = sh.idx.length
    ? sh.idx.map((i) => `<p data-i="${i}"><span>${singTime(singShareT(i))}</span>${esc(c.lines[i].text)}</p>`).join("")
    : '<p class="none">这首没有歌词，拖下面的两头选</p>';
  $("sgs-go").disabled = false;
  $("sgs-ver").hidden = !c.s.real;
  if (c.s.real) $("sgs-ver").querySelector('[data-v="real"]').textContent = `${c.s.real.singer}亲自唱`;
  sh.mode = "all";
  singShareVer(c.s.real && c.ver === "real" ? "real" : "ai");
  $("sing-share").showModal();
}

/** 发 AI 版还是真人版：真人版是从中间唱的，默认只框唱了的那段（前面没唱的只有伴奏）。 */
function singShareVer(v) {
  const sh = singShare, c = sh.c, r = c.s.real;
  sh.ver = v === "real" && r ? "real" : "ai";
  $("sgs-song").textContent = `《${c.s.title}》 · ${sh.ver === "real" ? `${r.singer} 亲自唱` : `${c.s.singer} 的 AI 翻唱`}`;
  for (const b of document.querySelectorAll("#sgs-ver [data-v]")) b.setAttribute("aria-selected", String(b.dataset.v === sh.ver));
  sh.dur = sh.ver === "real" ? r.seconds : c.dur || Number(c.s.seconds) || 0;
  for (const id of ["sgs-a", "sgs-b"]) $(id).max = String(Math.round(sh.dur * 10));
  sh.pick = -1;
  sh.a = 0; sh.b = sh.dur;
  if (sh.ver === "real" && r.start > 5) {
    sh.a = Math.max(0, r.start - 1); sh.b = Math.min(sh.dur, r.end + 1);
    singShareMode("part");
  } else singShareMode(sh.mode);
}

/** 第 i 句在 mp3 里的秒（卡片上调过「词快点 / 词慢点」也算上）。 */
function singShareT(i) {
  const c = singShare.c;
  return Math.max(0, c.lines[i].t - c.off);
}

function singShareMode(m) {
  const sh = singShare;
  sh.mode = m;
  for (const b of document.querySelectorAll("#sgs-mode [data-m]")) b.setAttribute("aria-selected", String(b.dataset.m === m));
  $("sgs-part").hidden = m !== "part";
  if (m === "part" && sh.a <= 0 && sh.b >= sh.dur && sh.ver === "ai") {   // 刚切过来：先给一段（第一句起 30 秒），再让人改
    sh.a = sh.idx.length ? Math.max(0, singShareT(sh.idx[0]) - 0.3) : 0;
    sh.b = Math.min(sh.dur, sh.a + 30);
  }
  singShareSync();
}

function singShareLine(i) {
  const sh = singShare, n = sh.c.lines.length;
  const end = (j) => (j + 1 < n ? Math.min(sh.dur, singShareT(j + 1) + 0.8) : sh.dur);
  if (sh.pick < 0 || i < sh.pick) {
    sh.pick = i;
    sh.a = Math.max(0, singShareT(i) - 0.3);
    sh.b = end(i);
  } else {
    sh.a = Math.max(0, singShareT(sh.pick) - 0.3);
    sh.b = end(i);
    sh.pick = -1;
  }
  singShareStopTry();
  singShareSync();
}

function singShareSync() {
  const sh = singShare, part = sh.mode === "part";
  const len = part ? sh.b - sh.a : sh.dur;
  $("sgs-a").value = String(Math.round(sh.a * 10));
  $("sgs-b").value = String(Math.round(sh.b * 10));
  const pct = (t) => `${(t / Math.max(1, sh.dur)) * 100}%`;
  $("sgs-fill").style.left = pct(sh.a);
  $("sgs-fill").style.width = pct(sh.b - sh.a);
  $("sgs-sum").textContent = `${singTime(sh.a)} – ${singTime(sh.b)} · ${Math.round(sh.b - sh.a)} 秒`;
  $("sgs-hint").textContent = sh.pick < 0 ? "点一句当开头，再点一句当结尾" : "再点一句当结尾（点的这句唱完）";
  document.querySelectorAll("#sgs-lines p[data-i]").forEach((p) => {
    const i = Number(p.dataset.i), t = singShareT(i);
    p.classList.toggle("in", t >= sh.a + 0.2 && t < sh.b - 0.85);   // 开头往前让了 0.3 秒，结尾多留了 0.8 秒
    p.classList.toggle("pick", i === sh.pick);
  });
  $("sgs-note").textContent = len > SING_VIDEO_MAX
    ? `微信里的视频最长 5 分钟，只发${part ? "这段" : ""}前 ${singTime(SING_VIDEO_MAX)}，结尾淡出`
    : (part ? "" : `整首 ${singTime(sh.dur)}`);
}

function singShareDrag(which) {
  const sh = singShare;
  const v = Number($(which === "a" ? "sgs-a" : "sgs-b").value) / 10;
  if (which === "a") sh.a = Math.min(v, sh.b - SING_CLIP_MIN);
  else sh.b = Math.max(v, sh.a + SING_CLIP_MIN);
  sh.a = Math.max(0, sh.a); sh.b = Math.min(sh.dur, sh.b);
  sh.pick = -1;
  singShareSync();
}

function singShareTry() {
  const sh = singShare;
  if (sh.audio && !sh.audio.paused) { singShareStopTry(); return; }
  const fid = sh.ver === "real" ? sh.c.s.real.file : sh.c.s.file;
  if (sh.audioFile !== fid) { sh.audio = new Audio(fileUrl({ id: fid })); sh.audioFile = fid; }
  const a = sh.audio;
  a.ontimeupdate = () => { if (a.currentTime >= sh.b) singShareStopTry(); };
  a.currentTime = sh.mode === "part" ? sh.a : 0;
  stopSpeaking();
  a.play().then(() => { $("sgs-try").textContent = "❚❚ 停"; }).catch((err) => toast("放不了：" + err.message));
}

function singShareStopTry() {
  singShare.audio?.pause();
  $("sgs-try").textContent = "▶ 试听这段";
}

async function singShareGo() {
  const sh = singShare, c = sh.c, gen = ++sh.gen, part = sh.mode === "part";
  const bridge = singWxBridge();
  if (!bridge) return;
  singShareStopTry();
  const go = $("sgs-go"), note = $("sgs-note");
  go.disabled = true;
  note.textContent = "正在做视频…";
  const real = sh.ver === "real" ? c.s.real : null;
  const body = JSON.stringify({ file: real ? real.file : c.s.file, off: c.off, ...(part ? { start: sh.a, end: sh.b } : {}) });
  try {
    for (;;) {
      const st = await api("api/sing/video", { method: "POST", headers: { "Content-Type": "application/json" }, body }).then(json);
      if (gen !== sh.gen) return;
      if (st.state === "ready") {
        const name = `${real ? real.singer + "亲自唱" : c.s.singer + "唱"}的《${c.s.title}》${part ? "（片段）" : ""}.mp4`;
        note.textContent = "视频好了，正在打开微信…";
        bridge.shareVideo(new URL(st.url, location.href).href, name);
        return;
      }
      if (st.state === "error") throw new Error(`${st.error || "视频没做出来"}，再点一次试试`);
      note.textContent = `正在做视频… ${st.pct || 0}%`;
      await new Promise((r) => setTimeout(r, 1000));
      if (gen !== sh.gen) return;
    }
  } catch (err) {
    if (gen !== sh.gen) return;
    note.textContent = err.message;
    go.disabled = false;
  }
}

/** 原生下载完、拉起了微信（open），或者没成（error + msg）。 */
window.xiaoxinShareVideo = (e) => {
  const d = $("sing-share");
  if (!d?.open) return;
  if (e?.state === "open") { d.close(); toast("视频也存进相册了"); return; }
  $("sgs-note").textContent = e?.msg || "没发出去，再试一次";
  $("sgs-go").disabled = false;
};

$("sgs-mode")?.addEventListener("click", (e) => {
  const m = e.target.closest("[data-m]")?.dataset.m;
  if (m && m !== singShare.mode) singShareMode(m);
});
$("sgs-ver")?.addEventListener("click", (e) => {
  const v = e.target.closest("[data-v]")?.dataset.v;
  if (v && v !== singShare.ver) { singShareStopTry(); singShareVer(v); }
});
$("sgs-lines")?.addEventListener("click", (e) => {
  const p = e.target.closest("p[data-i]");
  if (p) singShareLine(Number(p.dataset.i));
});
$("sgs-a")?.addEventListener("input", () => singShareDrag("a"));
$("sgs-b")?.addEventListener("input", () => singShareDrag("b"));
$("sgs-try")?.addEventListener("click", singShareTry);
$("sgs-go")?.addEventListener("click", singShareGo);
$("sgs-no")?.addEventListener("click", () => $("sing-share").close());
$("sing-share")?.addEventListener("close", () => { singShare.gen++; singShareStopTry(); });

// ---------------------------------------------------------------- 设置 → 唱歌

const singQr = { sid: "", timer: 0, kind: "wx" };
const SING_QR_TIP = {
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
