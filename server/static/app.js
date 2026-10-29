"use strict";
// 小新 web。所有请求用相对路径，这样挂在 /xiaoxin/ 下或直连 8184 都能用。

const $ = (id) => document.getElementById(id);
const state = { conv: null, models: [], model: "", pending: [], sending: false, voiceMode: false };

function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) { showLogin(); throw new Error("要先用免密链接打开一次"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) { /* 不是 json */ }
    throw new Error(detail);
  }
  return r;
}
const json = (r) => r.json();

// ---------------------------------------------------------------- 登录

function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
}

async function boot() {
  let me;
  try { me = await api("api/me").then(json); } catch (_) { return; }
  $("login").hidden = true;
  $("app").hidden = false;
  state.models = me.models;
  state.system = me.system_prompt;
  state.vision = me.vision_model;
  state.fallback = me.fallback_model;
  const saved = localGet("model");
  state.model = me.models.some((m) => m.id === saved) ? saved : me.default_model;
  $("model").innerHTML = me.models.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
  $("model").value = state.model;
  await refreshList();
  const last = localGet("conv");
  const list = state.list || [];
  if (last && list.some((c) => c.id === last)) await openConv(last);
  else renderEmpty();
  if (location.hash.startsWith("#settings")) openSettings(location.hash.split("/")[1]);
}

function localGet(k) { try { return localStorage.getItem("xiaoxin." + k); } catch (_) { return null; } }
function localSet(k, v) { try { localStorage.setItem("xiaoxin." + k, v); } catch (_) { /* 无痕模式 */ } }

// 每个浏览器自己的偏好
const prefs = { autoread: true, enter: true };
try { Object.assign(prefs, JSON.parse(localGet("prefs") || "{}")); } catch (_) { /* 坏数据就用默认 */ }
function savePrefs() { localSet("prefs", JSON.stringify(prefs)); }

$("model").addEventListener("change", () => { state.model = $("model").value; localSet("model", state.model); });

// ---------------------------------------------------------------- 侧栏

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(now) - start(d)) / 86400000);
  if (days === 0) return "今天";
  if (days === 1) return "昨天";
  if (days < 7) return "7 天内";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

async function refreshList() {
  const { conversations } = await api("api/conversations").then(json);
  state.list = conversations;
  const box = $("conv-list");
  box.innerHTML = "";
  let group = "";
  for (const c of conversations) {
    const g = dayLabel(c.updated);
    if (g !== group) {
      group = g;
      box.insertAdjacentHTML("beforeend", `<div class="conv-group">${g}</div>`);
    }
    const b = document.createElement("button");
    b.type = "button";
    b.className = "conv" + (state.conv && state.conv.id === c.id ? " active" : "");
    b.innerHTML = `<span>${esc(c.title)}</span><span class="del" role="button" aria-label="删除" title="删除">✕</span>`;
    b.addEventListener("click", (e) => {
      if (e.target.classList.contains("del")) { removeConv(c.id); return; }
      openConv(c.id);
      closeSide();
    });
    box.appendChild(b);
  }
}

async function removeConv(id) {
  if (!confirm("删掉这段对话？")) return;
  await api(`api/conversations/${id}`, { method: "DELETE" });
  if (state.conv && state.conv.id === id) { state.conv = null; renderEmpty(); }
  refreshList();
}

async function openConv(id) {
  state.conv = await api(`api/conversations/${id}`).then(json);
  localSet("conv", id);
  $("conv-title").textContent = state.conv.title || "新对话";
  $("messages").innerHTML = "";
  for (const m of state.conv.messages) appendMessage(m);
  scrollDown(true);
  refreshList();
}

function renderEmpty() {
  state.conv = null;
  $("conv-title").textContent = "小新";
  $("messages").innerHTML = `<div class="empty"><img src="static/icon.png" alt=""><b>你好，我是小新</b>可以打字、发图片和文件，也可以按住说话</div>`;
}

$("new-chat").addEventListener("click", () => { localSet("conv", ""); renderEmpty(); closeSide(); $("input").focus(); });

function openSide() { $("side").classList.add("open"); $("scrim").hidden = false; }
function closeSide() { $("side").classList.remove("open"); $("scrim").hidden = true; }
$("menu").addEventListener("click", openSide);
$("scrim").addEventListener("click", closeSide);

$("free-talk").addEventListener("click", () => toast("自由对话在做了：全双工、可插话，需要先把回声消除接好"));

// ---------------------------------------------------------------- 消息渲染

// 只做最常用的：``` 代码块、`行内代码`、**粗体**。其余原样显示（white-space: pre-wrap）。
function renderText(s) {
  const out = [];
  const parts = String(s).split(/```(\w*)\n?([\s\S]*?)(?:```|$)/g);
  for (let i = 0; i < parts.length; i += 3) {
    out.push(esc(parts[i]).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>"));
    if (i + 2 < parts.length) out.push(`<pre><code>${esc(parts[i + 2])}</code></pre>`);
  }
  return out.join("");
}

function fileUrl(f, dl) { return `api/files/${f.id}${dl ? "?download=1" : ""}`; }
function sizeLabel(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }

function attachmentsHtml(files) {
  return (files || []).map((f) => f.kind === "image"
    ? `<a href="${fileUrl(f)}" target="_blank" rel="noopener"><img class="att-img" src="${fileUrl(f)}" alt="${esc(f.name)}" loading="lazy"></a>`
    : `<a class="att-file" href="${fileUrl(f, true)}"><b>📄</b><span>${esc(f.name)}<small>${sizeLabel(f.size)}</small></span></a>`).join("");
}

function appendMessage(m) {
  const box = $("messages");
  box.querySelector(".empty")?.remove();
  const row = document.createElement("div");
  row.className = "msg " + (m.role === "user" ? "me" : "ai");
  if (m.role === "user") {
    row.innerHTML = `<div class="bubble">${attachmentsHtml(m.files)}${m.text ? `<div class="text">${esc(m.text)}</div>` : ""}`
      + `${m.via === "voice" ? '<div class="via">🎤 语音输入</div>' : ""}</div>`;
  } else {
    row.innerHTML = `<img src="static/icon.png" alt="" class="avatar sm"><div class="bubble"><div class="text"></div>`
      + `<div class="sources" hidden></div>`
      + `<div class="actions"><button type="button" data-act="copy">复制</button><button type="button" data-act="read">朗读</button></div>`
      + `<div class="meta"></div></div>`;
    row.querySelector(".text").innerHTML = renderText(m.text || "");
    row.querySelector(".meta").textContent = m.model || "";
    renderSources(row, m.searches);
    row.dataset.reply = m.text || "";
    wireActions(row);
  }
  box.appendChild(row);
  return row;
}

// 联网搜过的回答，下面列出处（同一个网址只列一次，最多 6 条），点开是原网页
function renderSources(row, searches) {
  const box = row.querySelector(".sources");
  const seen = new Set();
  const list = [];
  for (const s of searches || []) for (const src of s.sources || []) {
    if (!/^https?:\/\//.test(src.url || "") || seen.has(src.url)) continue;
    seen.add(src.url);
    list.push(src);
  }
  if (!searches?.length) { box.hidden = true; return; }
  const queries = searches.flatMap((s) => s.queries || []);
  box.hidden = false;
  box.innerHTML = `<div class="src-head">🔎 搜了：${esc(queries.join("、"))}</div>`
    + list.slice(0, 6).map((s, i) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">`
      + `${i + 1}. ${esc(s.title || s.url)}${s.site ? ` <small>${esc(s.site)}</small>` : ""}</a>`).join("");
}

// 只绑一次；读的是 row.dataset.reply，流式写完后更新它就行
function wireActions(row) {
  row.querySelector('[data-act="copy"]').addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(row.dataset.reply || ""); toast("复制了"); } catch (_) { toast("这个浏览器不让复制"); }
  });
  row.querySelector('[data-act="read"]').addEventListener("click", (e) => speak(row.dataset.reply || "", e.currentTarget));
}

// 同一时间只放一段。再点正在放的那条就是停止；点别的就切过去。
const player = { audio: null, btn: null, seq: 0, stream: null };
function stopSpeaking() {
  player.seq++;
  player.audio?.pause();
  player.stream?.stop();
  player.stream?.abort?.abort();  // 正在边生成边念：断开那条流，服务端不再合成、不再下发
  if (player.btn) player.btn.textContent = "朗读";
  player.audio = player.btn = player.stream = null;
}

// 边收边放的 PCM：服务端约 100 ms 一块 24 kHz s16le，按时间轴首尾相接排进 Web Audio，块之间不会有缝。
// 服务端最多领先 2 秒发，所以这里排着的也就 2 秒左右。
let audioCtx = null;
function getAudioCtx() {
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}
// 浏览器要求先有一次用户操作才能出声。发消息、按住说话都算，在那时把 AudioContext 解锁。
for (const ev of ["pointerdown", "keydown"]) document.addEventListener(ev, () => { if (prefs.autoread) getAudioCtx(); }, { capture: true });

class PcmStream {
  constructor(rate, onEnd) {
    this.ctx = getAudioCtx();
    this.rate = rate;
    this.onEnd = onEnd;
    this.next = 0;
    this.sources = new Set();
    this.closed = false;
    this.stopped = false;
  }
  push(b64) {
    if (this.stopped) return;
    const bin = atob(b64);
    const n = bin.length >> 1;
    const buf = this.ctx.createBuffer(1, n, this.rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < n; i++) {
      let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
      if (v >= 0x8000) v -= 0x10000;
      ch[i] = v / 32768;
    }
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    // 第一块或者断粮后重新起播时，留 60 ms 余量
    const at = Math.max(this.next, this.ctx.currentTime + 0.06);
    src.start(at);
    this.next = at + buf.duration;
    this.sources.add(src);
    src.onended = () => { this.sources.delete(src); this.maybeEnd(); };
  }
  close() { this.closed = true; this.maybeEnd(); }  // 服务端说没有更多音频了
  maybeEnd() { if (this.closed && !this.sources.size && !this.stopped) { this.stopped = true; this.onEnd?.(); } }
  stop() {
    this.stopped = true;
    for (const s of this.sources) { try { s.stop(); } catch (_) { /* 还没开始 */ } }
    this.sources.clear();
  }
}

async function speak(text, btn) {
  if (player.btn === btn) { stopSpeaking(); return; }
  stopSpeaking();
  if (!text.trim()) { toast("这条没有可读的字"); return; }
  const seq = player.seq;
  player.btn = btn;
  btn.textContent = "生成中…";
  try {
    const r = await api("api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 600) }) });
    const note = decodeURIComponent(r.headers.get("x-voice-note") || "");
    const blob = await r.blob();
    if (seq !== player.seq) return;  // 等的时候被停了或换了一条
    if (note) toast(note);
    const audio = new Audio(URL.createObjectURL(blob));
    player.audio = audio;
    btn.textContent = "停止";
    audio.onended = () => { if (player.audio === audio) stopSpeaking(); };
    await audio.play();
  } catch (err) {
    if (seq !== player.seq) return;
    stopSpeaking();
    // 自动朗读被浏览器拦（没点过页面就不让出声）时，给个能看懂的提示
    toast(err.name === "NotAllowedError" ? "浏览器拦了自动播放，点一下「朗读」就能听" : err.message);
  }
}

function scrollDown(force) {
  const c = $("chat");
  if (force || c.scrollHeight - c.scrollTop - c.clientHeight < 160) c.scrollTop = c.scrollHeight;
}

// ---------------------------------------------------------------- 发送

async function ensureConv() {
  if (state.conv) return state.conv;
  state.conv = await api("api/conversations", { method: "POST" }).then(json);
  localSet("conv", state.conv.id);
  return state.conv;
}

function canSend() { return !state.sending && !state.pending.some((p) => !p.meta) && ($("input").value.trim() || state.pending.length); }
function updateSend() { $("send").disabled = !canSend(); }

async function send(text, via = "text") {
  if (state.sending) return;
  stopSpeaking();
  const files = state.pending.filter((p) => p.meta).map((p) => p.meta);
  if (!text && !files.length) return;
  state.sending = true;
  updateSend();
  const conv = await ensureConv();
  $("input").value = "";
  autoGrow();
  state.pending = [];
  renderPending();

  const row = appendMessage({ role: "assistant", text: "" });
  const textEl = row.querySelector(".text");
  const meta = row.querySelector(".meta");
  textEl.classList.add("cursor");
  textEl.innerHTML = '<span class="thinking">小新在想…</span>';
  let reply = "", reasoning = 0;
  const searches = [];
  // 开着自动朗读就让服务端边出字边合成，音频跟着 SSE 一起下来；打断（停止、发新消息、按住说话）就断开这条流。
  const speakNow = prefs.autoread;
  const readBtn = row.querySelector('[data-act="read"]');
  const abort = new AbortController();
  state.abort = abort;
  if (speakNow) {
    stopSpeaking();
    const seq = player.seq;
    player.btn = readBtn;
    readBtn.textContent = "停止";
    player.stream = new PcmStream(24000, () => { if (player.seq === seq) stopSpeaking(); });
    player.stream.abort = abort;
  }
  try {
    const r = await api(`api/conversations/${conv.id}/messages`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, file_ids: files.map((f) => f.id), model: state.model, via, speak: speakNow }),
      signal: abort.signal });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const ev = /^event: (.*)$/m.exec(block)?.[1];
        const data = JSON.parse(/^data: (.*)$/m.exec(block)?.[1] || "null");
        if (ev === "user") {
          $("messages").insertBefore(appendMessage(data), row);
        } else if (ev === "model") {
          meta.textContent = data;
        } else if (ev === "reasoning") {
          reasoning += data.length;
          if (!reply) textEl.innerHTML = `<span class="thinking">小新在想…（${reasoning} 字）</span>`;
        } else if (ev === "search") {
          if (data.state === "start") {
            if (!reply) textEl.innerHTML = `<span class="thinking">🔎 正在搜：${esc(data.queries.join("、"))}…</span>`;
          } else {
            searches.push(data);
            renderSources(row, searches);
            if (!reply) textEl.innerHTML = `<span class="thinking">${data.ok ? "搜到了，在整理…" : "没搜到，小新先按知道的说…"}</span>`;
          }
        } else if (ev === "delta") {
          reply += data;
          textEl.innerHTML = renderText(reply);
        } else if (ev === "audio") {
          if (player.btn === readBtn) player.stream?.push(data);
        } else if (ev === "audio_end") {
          if (player.btn === readBtn) player.stream?.close();
          if (data.error) toast("朗读出错：" + data.error);
        } else if (ev === "note") {
          toast(data);
        } else if (ev === "done") {
          reply = data.text;
          textEl.innerHTML = renderText(reply);
          if (data.searches) renderSources(row, data.searches);
          finishText();  // 文字完了就能发下一条；音频还在这条流里接着下来
        } else if (ev === "error") {
          throw new Error(data);
        }
        scrollDown();
      }
    }
    if (!reply) throw new Error("没收到回答");
    finishText();
  } catch (err) {
    if (err.name === "AbortError") {
      // 被打断：已经出来的字留着，后面的不要了。服务端会把完整回答存下来，刷新后能看到全文。
      if (!reply) { row.classList.add("error"); textEl.textContent = "（打断了）"; }
    } else {
      row.classList.add("error");
      textEl.textContent = "出错了：" + err.message;
    }
    finishText();
  } finally {
    if (state.abort === abort) state.abort = null;
    // 流结束了还没收到音频（自动朗读没开、服务端 TTS 失败）：关掉空的播放器，按钮恢复成「朗读」
    if (speakNow && player.btn === readBtn && player.stream) player.stream.close();
  }

  // 文字收尾只做一次：存 reply、解锁输入框、刷新侧栏
  function finishText() {
    if (row.dataset.finished) return;
    row.dataset.finished = "1";
    textEl.classList.remove("cursor");
    row.dataset.reply = row.classList.contains("error") ? "" : reply;
    state.sending = false;
    updateSend();
    scrollDown();
    refreshList().then(() => {
      const c = state.list.find((x) => x.id === conv.id);
      if (c) $("conv-title").textContent = c.title;
    });
  }
}

$("send").addEventListener("click", () => canSend() && send($("input").value.trim()));
$("input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  const sendKey = prefs.enter ? !e.shiftKey && !e.ctrlKey && !e.metaKey : e.ctrlKey || e.metaKey;
  if (sendKey) {
    e.preventDefault();
    if (canSend()) send($("input").value.trim());
  }
});
function autoGrow() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; }
$("input").addEventListener("input", () => { autoGrow(); updateSend(); });

// ---------------------------------------------------------------- 附件

$("plus").addEventListener("click", () => {
  const open = $("tray").hidden;
  $("tray").hidden = !open;
  $("plus").setAttribute("aria-expanded", String(open));
});
for (const id of ["pick-camera", "pick-image", "pick-file"]) {
  $(id).addEventListener("change", (e) => {
    for (const f of e.target.files) upload(f);
    e.target.value = "";
    $("tray").hidden = true;
  });
}
$("input").addEventListener("paste", (e) => {
  for (const item of e.clipboardData?.items || []) {
    if (item.kind === "file") { e.preventDefault(); upload(item.getAsFile()); }
  }
});
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  for (const f of e.dataTransfer?.files || []) upload(f);
});

async function upload(file) {
  if (!file) return;
  if (file.size > 50 * 1024 * 1024) { toast(`${file.name} 超过 50 MB`); return; }
  const p = { file, meta: null, preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : "" };
  state.pending.push(p);
  renderPending();
  try {
    p.meta = await api("api/files", { method: "POST", body: file,
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name || "file") } }).then(json);
  } catch (err) {
    state.pending = state.pending.filter((x) => x !== p);
    toast(`${file.name} 上传失败：${err.message}`);
  }
  renderPending();
}

function renderPending() {
  const box = $("pending");
  box.innerHTML = "";
  for (const p of state.pending) {
    const el = document.createElement("div");
    el.className = "pend" + (p.meta ? "" : " uploading");
    el.innerHTML = (p.preview ? `<img src="${p.preview}" alt="">` : `<span class="name">📄 ${esc(p.file.name)}</span>`)
      + '<button type="button" class="x" aria-label="去掉">✕</button>';
    el.querySelector(".x").addEventListener("click", () => { state.pending = state.pending.filter((x) => x !== p); renderPending(); });
    box.appendChild(el);
  }
  updateSend();
}

// ---------------------------------------------------------------- 录音（按住说话 / 录声音共用）

// 麦克风 → 16 kHz 单声道 Int16，边录边回调电平。不用 AudioWorklet，老平板浏览器也能跑。
class Recorder {
  constructor(onLevel, onChunk) { this.onLevel = onLevel; this.onChunk = onChunk; this.chunks = []; this.rate = 16000; }
  async start() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("这个页面拿不到麦克风（要 https）");
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = this.ctx.createScriptProcessor(2048, 1, 1);  // 约 43 ms 一块，攒到 100 ms 发
    const ratio = this.ctx.sampleRate / this.rate;
    let carry = 0;
    this.node.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const out = new Int16Array(Math.floor((input.length - carry) / ratio) + 1);
      let n = 0, peak = 0;
      for (let pos = carry; pos < input.length; pos += ratio) {
        const s = Math.max(-1, Math.min(1, input[Math.floor(pos)]));
        out[n++] = s * 0x7fff;
        peak = Math.max(peak, Math.abs(s));
      }
      carry = (carry + n * ratio) - input.length;
      if (carry < 0) carry = 0;
      this.chunks.push(out.subarray(0, n));
      this.onChunk?.(out.subarray(0, n));
      this.onLevel?.(peak, this.seconds());
    };
    src.connect(this.node);
    this.node.connect(this.ctx.destination);
  }
  seconds() { return this.chunks.reduce((a, c) => a + c.length, 0) / this.rate; }
  async stop() {
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.ctx?.close();
    return wav(this.chunks, this.rate);
  }
}

function wav(chunks, rate) {
  const n = chunks.reduce((a, c) => a + c.length, 0);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) v.setInt16(o, c[i], true);
  return new Blob([buf], { type: "audio/wav" });
}

function drawLevels(canvas, levels, color) {
  const g = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height, bar = 4, gap = 3;
  g.clearRect(0, 0, w, h);
  g.fillStyle = color;
  const count = Math.floor(w / (bar + gap));
  const tail = levels.slice(-count);
  tail.forEach((lv, i) => {
    const bh = Math.max(3, Math.min(1, lv * 2.5) * h);
    g.fillRect(i * (bar + gap), (h - bh) / 2, bar, bh);
  });
}

// ---------------------------------------------------------------- 按住说话

$("mode").addEventListener("click", () => {
  state.voiceMode = !state.voiceMode;
  $("input").hidden = state.voiceMode;
  $("send").hidden = state.voiceMode;
  $("hold").hidden = !state.voiceMode;
  $("mode").textContent = state.voiceMode ? "⌨" : "🎤";
  $("mode").setAttribute("aria-label", state.voiceMode ? "切换到打字" : "切换到按住说话");
  if (!state.voiceMode) $("input").focus();
});

// 按下就连 WebSocket、边说边传 PCM（100 ms 一包），服务端同时转给豆包。
// 松手只发 {"type":"end"}：豆包已经有前面全部音频，最终结果大约 0.1 s 回来。
// 上滑取消发 {"type":"cancel"} 并断开：服务端不发尾包，这段话直接丢掉，不进对话。
const hold = { rec: null, ws: null, startY: 0, cancel: false, levels: [], starting: null, buf: [], bufLen: 0, text: "" };
const holdBtn = $("hold");
const CHUNK = 1600;  // 100 ms @ 16 kHz

function wsUrl(path) {
  const u = new URL(path, location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  return u.href;
}

function flushChunk(force) {
  const ws = hold.ws;
  if (!hold.bufLen || (!force && hold.bufLen < CHUNK)) return;
  const out = new Int16Array(hold.bufLen);
  let o = 0;
  for (const c of hold.buf) { out.set(c, o); o += c.length; }
  hold.buf = []; hold.bufLen = 0;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(out.buffer);
  else hold.pendingAudio.push(out.buffer);  // 连接还没好，先攒着，连上一起补发
}

function showLive(text) {
  hold.text = text;
  $("rec-live").textContent = text || "正在听…";
}

holdBtn.addEventListener("pointerdown", (e) => {
  if (state.sending || hold.rec) return;
  e.preventDefault();
  holdBtn.setPointerCapture(e.pointerId);
  Object.assign(hold, { startY: e.clientY, cancel: false, levels: [], buf: [], bufLen: 0, pendingAudio: [], text: "", final: null });
  holdBtn.classList.add("active");
  holdBtn.textContent = "松开 发送";
  $("rec-overlay").hidden = false;
  $("rec-overlay").querySelector(".rec-card").classList.remove("cancel");
  $("rec-hint").textContent = "松开发送 · 上滑取消";
  showLive("");
  stopSpeaking();

  const ws = new WebSocket(wsUrl("api/asr/stream"));
  ws.binaryType = "arraybuffer";
  hold.ws = ws;
  hold.final = new Promise((resolve, reject) => {
    ws.onopen = () => { for (const b of hold.pendingAudio) ws.send(b); hold.pendingAudio = []; };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "ready") { if (hold.ws === ws && !m.partials && !hold.text) $("rec-live").textContent = "正在听…（Paraformer 松手后出字）"; }
      else if (m.type === "partial") { if (hold.ws === ws) showLive(m.text); }
      else if (m.type === "final") resolve(m);
      else if (m.type === "error") reject(new Error(m.detail));
    };
    ws.onclose = (ev) => reject(new Error(ev.code === 4401 ? "要先用免密链接打开一次" : "连接断了"));
  });
  hold.final.catch(() => {});  // 取消时没人等它

  const rec = new Recorder(
    (peak) => { hold.levels.push(peak); drawLevels($("rec-wave"), hold.levels, hold.cancel ? "#fff" : "#062b16"); },
    (pcm) => { hold.buf.push(pcm); hold.bufLen += pcm.length; flushChunk(false); },
  );
  hold.rec = rec;
  hold.starting = rec.start().catch((err) => { toast(err.message); abortHold(); });
});

holdBtn.addEventListener("pointermove", (e) => {
  if (!hold.rec) return;
  const cancel = hold.startY - e.clientY > 60;
  if (cancel !== hold.cancel) {
    hold.cancel = cancel;
    $("rec-overlay").querySelector(".rec-card").classList.toggle("cancel", cancel);
    $("rec-hint").textContent = cancel ? "松开取消" : "松开发送 · 上滑取消";
  }
});

function endHoldUi() {
  holdBtn.classList.remove("active");
  holdBtn.textContent = "按住 说话";
  $("rec-overlay").hidden = true;
}

function abortHold() {
  const { rec, ws } = hold;
  hold.rec = hold.ws = null;
  endHoldUi();
  rec?.stop().catch(() => {});
  if (ws) {
    try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "cancel" })); } catch (_) { /* 已断 */ }
    ws.close();
  }
}

async function endHold() {
  const { rec, ws } = hold;
  if (!rec) return;
  if (hold.cancel) { abortHold(); return; }
  hold.rec = null;
  endHoldUi();
  await hold.starting;
  const seconds = rec.seconds();
  await rec.stop();
  if (seconds < 0.4) { hold.ws = null; ws.close(); toast("太短了，按住多说一会儿"); return; }
  flushChunk(true);
  holdBtn.textContent = "识别中…";
  holdBtn.disabled = true;
  try {
    if (ws.readyState === WebSocket.CONNECTING) await new Promise((r) => ws.addEventListener("open", r, { once: true }));
    ws.send(JSON.stringify({ type: "end" }));
    const { text } = await hold.final;
    hold.ws = null;
    if (!text) { toast("没听清，再说一次"); return; }
    await send(text, "voice");
  } catch (err) {
    toast("识别失败：" + err.message);
  } finally {
    hold.ws = null;
    holdBtn.textContent = "按住 说话";
    holdBtn.disabled = false;
  }
}
holdBtn.addEventListener("pointerup", endHold);
holdBtn.addEventListener("pointercancel", () => { hold.cancel = true; endHold(); });
holdBtn.addEventListener("contextmenu", (e) => e.preventDefault());

// ---------------------------------------------------------------- 录我的声音

const SAMPLE_TEXT = "今天天气不错，放学回家先把作业写完，再出去玩一会儿。数学练习册第二十三页，"
  + "语文抄写生字两遍，英语读课文十分钟。做完了记得检查一遍，有不会的题就来问我，我们一起想办法。";
const vrec = { rec: null, blob: null, levels: [], timer: null };

// ---------------------------------------------------------------- 设置

const PANES = ["general", "voice", "account"];

function openSettings(pane = "general") {
  closeSide();
  $("set-model").innerHTML = state.models.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
  $("set-model").value = localGet("model") || state.model;
  $("set-autoread").checked = prefs.autoread;
  $("set-system").textContent = state.system || "（没取到）";
  const label = (id) => state.models.find((m) => m.id === id)?.label || id;
  $("set-routing").textContent = state.vision
    ? `带图片的消息，选了不看图的模型会自动换成 ${label(state.vision)}；选的模型连着失败两次，换成 ${label(state.fallback)} 回答。`
    : "";
  $("set-enter").checked = prefs.enter;
  showPane(PANES.includes(pane) ? pane : "general");
  if (!$("settings").open) $("settings").showModal();
}

function showPane(name) {
  for (const b of document.querySelectorAll(".settings-nav button")) b.setAttribute("aria-selected", String(b.dataset.pane === name));
  for (const p of document.querySelectorAll(".pane")) p.hidden = p.dataset.pane !== name;
  if (name === "voice") {
    if (!$("voice-text").value) $("voice-text").value = SAMPLE_TEXT;
    loadEngines();
    loadVoices();
  }
  history.replaceState(null, "", "#settings/" + name);
}

for (const b of document.querySelectorAll(".settings-nav button")) b.addEventListener("click", () => showPane(b.dataset.pane));
$("open-settings").addEventListener("click", () => openSettings());
$("settings-close").addEventListener("click", () => $("settings").close());
$("settings").addEventListener("close", async () => {
  stopPreview();
  if (vrec.rec) { await vrec.rec.stop(); vrec.rec = null; resetVoiceBtn(); }
  history.replaceState(null, "", location.pathname);
});

$("set-model").addEventListener("change", () => {
  state.model = $("set-model").value;
  $("model").value = state.model;
  localSet("model", state.model);
});
$("set-autoread").addEventListener("change", () => setAutoread($("set-autoread").checked));

// 顶栏 🔊 / 🔇：和设置里的「回答完自动朗读」是同一个开关
function setAutoread(on) {
  prefs.autoread = on;
  savePrefs();
  $("set-autoread").checked = on;
  $("autoread").textContent = on ? "🔊" : "🔇";
  $("autoread").setAttribute("aria-pressed", String(on));
  $("autoread").title = on ? "自动朗读：开（点一下关掉）" : "自动朗读：关（点一下打开）";
  if (!on) stopSpeaking();
}
$("autoread").addEventListener("click", () => { setAutoread(!prefs.autoread); toast(prefs.autoread ? "回答完会自动读出来" : "不自动朗读了"); });
setAutoread(prefs.autoread);
$("set-enter").addEventListener("change", () => { prefs.enter = $("set-enter").checked; savePrefs(); });

$("logout").addEventListener("click", async () => {
  if (!confirm("在这个浏览器退出？之后要用免密链接重新打开。")) return;
  try { await api("api/logout", { method: "POST" }); } catch (_) { /* 已经是退出状态 */ }
  $("settings").close();
  showLogin();
});

function resetVoiceBtn() {
  $("voice-rec").classList.remove("on");
  $("voice-rec").textContent = "● 开始录";
  clearInterval(vrec.timer);
}

$("voice-rec").addEventListener("click", async () => {
  if (vrec.rec) {
    const rec = vrec.rec;
    vrec.rec = null;
    resetVoiceBtn();
    vrec.blob = await rec.stop();
    $("voice-audio").src = URL.createObjectURL(vrec.blob);
    $("voice-take").hidden = false;
    $("voice-msg").textContent = `录了 ${rec.seconds().toFixed(1)} 秒，听一下，满意就保存`;
    return;
  }
  vrec.levels = [];
  vrec.blob = null;
  $("voice-take").hidden = true;
  $("voice-msg").textContent = "";
  const rec = new Recorder((peak, s) => {
    vrec.levels.push(peak);
    drawLevels($("voice-wave"), vrec.levels, "#3ddc84");
    $("voice-time").textContent = s.toFixed(1) + " s";
  });
  try {
    await rec.start();
  } catch (err) { $("voice-msg").textContent = err.message; return; }
  vrec.rec = rec;
  $("voice-rec").classList.add("on");
  $("voice-rec").textContent = "■ 停止";
  vrec.timer = setInterval(() => { if (vrec.rec && vrec.rec.seconds() >= 90) $("voice-rec").click(); }, 500);
});

$("voice-redo").addEventListener("click", () => { $("voice-take").hidden = true; $("voice-rec").click(); });

$("voice-save").addEventListener("click", async () => {
  if (!vrec.blob) return;
  $("voice-save").disabled = true;
  $("voice-msg").textContent = "保存中，正在挑参考段…";
  try {
    const meta = await api("api/voice", { method: "POST", body: vrec.blob,
      headers: { "Content-Type": "audio/wav", "X-Text": encodeURIComponent($("voice-text").value.trim()),
        "X-Name": encodeURIComponent($("voice-name").value.trim()) } }).then(json);
    const ref = meta.ref || {};
    $("voice-msg").textContent = `保存了「${meta.name}」（${meta.seconds} 秒，参考段 ${ref.seconds || "-"} 秒）`;
    $("voice-take").hidden = true;
    $("voice-name").value = "";
    vrec.blob = null;
    loadVoices();
  } catch (err) {
    $("voice-msg").textContent = "保存失败：" + err.message;
  } finally { $("voice-save").disabled = false; }
});

// ---------------------------------------------------------------- 音色列表

let previewAudio = null, previewBtn = null;
function stopPreview() {
  previewAudio?.pause();
  previewAudio = null;
  if (previewBtn) { previewBtn.classList.remove("playing"); previewBtn.textContent = "▶"; }
  previewBtn = null;
}

async function preview(btn, voice, src) {
  if (previewBtn === btn) { stopPreview(); return; }
  stopPreview();
  previewBtn = btn;
  btn.textContent = "…";
  try {
    let url = src;
    if (!url) {
      const r = await api("api/voices/preview", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice }) });
      const note = decodeURIComponent(r.headers.get("x-voice-note") || "");
      if (note) toast(note);
      url = URL.createObjectURL(await r.blob());
    }
    if (previewBtn !== btn) return;
    previewAudio = new Audio(url);
    btn.classList.add("playing");
    btn.textContent = "■";
    previewAudio.onended = stopPreview;
    await previewAudio.play();
  } catch (err) { stopPreview(); toast(err.message); }
}

async function selectVoice(id) {
  try {
    const cat = await api("api/voices/current", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: id }) }).then(json);
    renderVoices(cat);
    loadEngines();  // 选录的声音会把引擎切到 CosyVoice
    const v = cat.voices.find((x) => x.id === id);
    if (v && !v.ready) toast("已选上。CosyVoice 用不了的时候，先用豆包念");
  } catch (err) { toast(err.message); }
}

function voiceCard(v, current) {
  const card = document.createElement("div");
  card.className = "vcard";
  card.setAttribute("role", "radio");
  card.setAttribute("aria-checked", String(v.id === current));
  card.tabIndex = 0;
  const tag = !v.ready ? '<span class="tag">CosyVoice 用不了</span>' : "";
  card.innerHTML = `<div class="vtext"><b>${esc(v.name)}</b><small>${esc(v.desc)} ${tag}</small></div>`
    + `<button type="button" class="vbtn" aria-label="试听 ${esc(v.name)}">▶</button>`
    + (v.custom || v.kind === "clone" ? `<button type="button" class="vbtn vmore" aria-label="更多">⋯</button>` : "");
  card.addEventListener("click", (e) => { if (!e.target.closest(".vbtn")) selectVoice(v.id); });
  card.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === card) { e.preventDefault(); selectVoice(v.id); } });
  const [play, more] = card.querySelectorAll(".vbtn");
  // 克隆音色试听的是切好的参考段，听听挑得准不准
  play.addEventListener("click", () => preview(play, v.id, v.kind === "clone" ? `api/voice/${v.sample}.wav?ref=1` : ""));
  more?.addEventListener("click", () => voiceMenu(v));
  return card;
}

async function voiceMenu(v) {
  if (v.kind === "clone") {
    const act = prompt(`「${v.name}」\n输入新名字改名，输入 删除 删掉这段录音：`, v.name);
    if (act === null || act.trim() === "" || act.trim() === v.name) return;
    if (act.trim() === "删除") {
      if (!confirm(`删掉「${v.name}」？`)) return;
      await api(`api/voice/${v.sample}`, { method: "DELETE" });
    } else {
      await api(`api/voice/${v.sample}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: act.trim() }) });
    }
  } else if (confirm(`从列表里去掉「${v.name}」？`)) {
    await api(`api/voices/custom/${encodeURIComponent(v.id.split(":")[1])}`, { method: "DELETE" });
  } else return;
  loadVoices();
}

function renderVoices(cat) {
  const cur = cat.voices.find((v) => v.id === cat.current);
  const cosy = cat.engines?.tts === "cosyvoice";
  $("voice-current").innerHTML = `现在用：<b>${esc(cur?.name || "-")}</b>`
    + `（${cosy ? "CosyVoice" : "豆包"} 念）`
    + (cosy && !cat.clone_ready ? "，CosyVoice 没起来，先用豆包念" : "");
  // 豆包音色两个引擎都能念；录的声音只有 CosyVoice 能念，豆包引擎下点它会切到 CosyVoice
  const groups = [
    ["我的声音" + (cosy ? "" : "（只能 CosyVoice 念，点一下会切过去）"), cat.voices.filter((v) => v.kind === "clone")],
    ["自己加的豆包音色", cat.voices.filter((v) => v.custom)],
    ["豆包音色", cat.voices.filter((v) => v.kind === "doubao" && !v.custom)],
  ];
  const box = $("voice-groups");
  box.innerHTML = "";
  for (const [title, list] of groups) {
    if (!list.length) continue;
    box.insertAdjacentHTML("beforeend", `<div class="vgroup-title">${title}</div>`);
    const grid = document.createElement("div");
    grid.className = "vgrid";
    grid.setAttribute("role", "radiogroup");
    grid.setAttribute("aria-label", title);
    for (const v of list) grid.appendChild(voiceCard(v, cat.current));
    box.appendChild(grid);
  }
}

async function loadVoices() {
  try { renderVoices(await api("api/voices").then(json)); } catch (err) { toast(err.message); }
}

// ---------------------------------------------------------------- 语音引擎

function renderEngines(e) {
  const fill = (sel, options, cur, ready) => {
    $(sel).innerHTML = options.map((o) => `<option value="${o.id}">${esc(o.name)}${ready[o.id] === false ? "（没起来）" : ""}</option>`).join("");
    $(sel).value = cur;
  };
  fill("set-asr", e.asr_options, e.asr, { paraformer: e.paraformer_ready });
  fill("set-tts", e.tts_options, e.tts, { cosyvoice: e.cosyvoice_ready });
  $("asr-desc").textContent = e.asr_options.find((o) => o.id === e.asr)?.desc || "";
  $("tts-desc").textContent = e.tts_options.find((o) => o.id === e.tts)?.desc || "";
}

async function loadEngines() {
  try { renderEngines(await api("api/engines").then(json)); } catch (err) { toast(err.message); }
}

async function setEngine(kind, value) {
  try {
    const e = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [kind]: value }) }).then(json);
    renderEngines(e);
    if (kind === "asr" && value === "paraformer" && !e.paraformer_ready) toast("Paraformer 第一次用要加载十几秒，之后就快了");
    if (kind === "tts") loadVoices();
  } catch (err) { toast(err.message); loadEngines(); }
}
$("set-asr").addEventListener("change", () => setEngine("asr", $("set-asr").value));
$("set-tts").addEventListener("change", () => setEngine("tts", $("set-tts").value));

$("custom-add").addEventListener("click", async () => {
  const speaker = $("custom-speaker").value.trim();
  if (!speaker) { $("custom-speaker").focus(); return; }
  $("custom-add").disabled = true;
  $("custom-msg").textContent = "试合成一句…";
  try {
    renderVoices(await api("api/voices/custom", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker, name: $("custom-name").value.trim() }) }).then(json));
    $("custom-msg").textContent = "加好了，在上面「自己加的豆包音色」里";
    $("custom-speaker").value = "";
    $("custom-name").value = "";
  } catch (err) {
    $("custom-msg").textContent = err.message;
  } finally { $("custom-add").disabled = false; }
});

boot();
