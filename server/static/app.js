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
  state.me = me.who;
  state.kids = me.kids;
  state.people = me.people || {};
  const vm = localGet("voiceMode");
  setVoiceMode(vm ? vm === "1" : (!!native && me.who.role !== "parent"), false);
  state.sendOrig = localGet("orig") === "1";
  // 设置页签按角色：小新本身的（语音引擎、唤醒词、音色、提示词、成员）只给管理员；家长、孩子只看「对话」和「登录与设备」
  for (const pane of ["model", "talk", "wake", "voice", "prompt", "members"]) {
    document.querySelector(`.settings-nav [data-pane="${pane}"]`).hidden = me.who.kind !== "admin";
  }
  state.models = me.models;
  state.system = me.system_prompt;
  state.vision = me.vision_model;
  state.fallback = me.fallback_model;
  const saved = localGet("model");
  state.model = me.models.some((m) => m.id === saved) ? saved : me.default_model;
  // 家长：先看聊天列表（每个孩子一个群），点进去才是群聊；孩子的 pad 和管理员宽屏直接进群
  if (state.me.role === "parent" || (state.me.kind === "admin" && matchMedia("(max-width: 820px)").matches && state.kids.length > 1)) {
    document.body.classList.add("parent-mode");
    $("ask").hidden = false;
    $("call-kid").hidden = false;
    $("input").placeholder = "发给孩子…（开头说「小新，」就是问小新）";
    await showChats();
    setTimeout(() => window.xiaoxinShareArrived(), 0);  // 被微信「分享」拉起来的：弹「发给孩子」
  } else {
    await openThread();
  }
  if (location.hash.startsWith("#settings")) openSettings(location.hash.split("/")[1]);
  if (native) api("api/voices").then(json).then((c) => syncVoiceToNative(c.current)).catch(() => {});
  if (state.me.role !== "parent") pollClock();  // clock.js：长轮询闹钟状态（家长端没有闹钟面板，不占连接）
}

function localGet(k) { try { return localStorage.getItem("xiaoxin." + k); } catch (_) { return null; } }
function localSet(k, v) { try { localStorage.setItem("xiaoxin." + k, v); } catch (_) { /* 无痕模式 */ } }

// 每个浏览器自己的偏好
const prefs = { autoread: true, enter: true };
try { Object.assign(prefs, JSON.parse(localGet("prefs") || "{}")); } catch (_) { /* 坏数据就用默认 */ }
function savePrefs() { localSet("prefs", JSON.stringify(prefs)); }

// 换模型（设置 →「模型」）：记住、待命的唤醒地址跟着换；自由对话中下一句起生效
function setModel(id) {
  state.model = id;
  localSet("model", id);
  syncStandbyUrl();
  if (talk.on) talkSend({ type: "model", model: id });
}

function renderModels() {
  const box = $("model-list");
  box.innerHTML = state.models.map((m) => `<label class="model-opt"><input type="radio" name="model" value="${esc(m.id)}"`
    + `${m.id === state.model ? " checked" : ""}><span><b>${esc(m.label)}</b><small>${esc(m.id)}`
    + `${m.id === state.vision ? " · 看图片" : ""}${m.id === state.fallback ? " · 失败时兜底" : ""}</small></span></label>`).join("");
  for (const r of box.querySelectorAll("input")) r.addEventListener("change", () => { setModel(r.value); toast(`换成 ${state.models.find((m) => m.id === r.value)?.label}`); });
  const label = (id) => state.models.find((m) => m.id === id)?.label || id;
}

// ---------------------------------------------------------------- 侧栏

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 小新是家里一个成员：全家只有一条线程（像微信里只有一个家庭群），没有「新对话」。
// 上下文太长或乱了，在设置里「开始新篇章」换一个干净的记忆窗口，聊天记录一条不动，只多一条分界线。
async function openThread(kid) {
  state.conv = await api("api/thread" + (kid ? `?kid=${encodeURIComponent(kid)}` : "")).then(json);
  markRead(state.conv);
  localSet("conv", state.conv.id);
  renderThread();
  loadCtx();
  syncStandbyUrl();
  hubConnect();
}

// ---------------------------------------------------------------- 实时总线：别的设备说的话马上出现（server/family/hub.py）
// 自己这台发的、小新流式答的，本地已经画了（行上有 data-mid），hub 再推来按 id 跳过。
// 断了 2→4→…→30 秒重连，连上先按最后一条 id 补齐（GET api/thread?since=）。
const hub = { ws: null, wait: 2000, timer: null };

function hubConnect() {
  if (hub.ws && hub.ws.readyState <= 1) return;
  clearTimeout(hub.timer);
  const url = new URL("api/hub", location.href).href.replace(/^http/, "ws");
  const ws = hub.ws = new WebSocket(url);
  ws.onopen = () => { hub.wait = 2000; hubCatchUp(); printCatchUp(); };
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "ping") { ws.send('{"type":"pong"}'); return; }
    if (ev.type === "msg" && document.body.classList.contains("in-chats")) hubMessage(ev.message, ev.type);
    else if ((ev.type === "msg" || ev.type === "msg.update") && ev.thread === state.conv?.id) hubMessage(ev.message, ev.type);
    if (ev.type === "presence") { state.online = new Set(ev.online); renderSide(); }
    if (ev.type === "print") onPrintEvent(ev.job);
    if (ev.type?.startsWith("call.")) onCallEvent(ev);
    if (ev.type === "print.summary" && ev.thread === state.conv?.id) onPrintSummary(ev);
    if (ev.type === "file" && ev.thread === state.conv?.id) hubFile(ev.file);
  };
  ws.onclose = () => {
    if (hub.ws !== ws) return;
    hub.timer = setTimeout(hubConnect, hub.wait);
    hub.wait = Math.min(hub.wait * 2, 30000);
  };
}

// ---------------------------------------------------------------- 侧栏：群信息（成员、下一个闹钟、爸妈发的文件）

function renderSide() {
  const conv = state.conv;
  if (!conv) return;
  // 群成员：这条线程的孩子 + 家长 + 小新（自己排第一个，小新排最后）
  const people = Object.entries(state.people || {})
    .filter(([id, p]) => p.role === "parent" || id === conv.kid)
    .map(([id, p]) => ({ id, ...p }))
    .sort((a, b) => (a.id === state.me?.id ? -1 : b.id === state.me?.id ? 1 : a.role === b.role ? 0 : a.role === "kid" ? -1 : 1));
  const dot = (id) => state.online?.has(id) ? '<i class="on" title="在线"></i>' : "";
  $("side-people").innerHTML = people.map((p) => `<li><button type="button" data-to="${esc(p.id)}" ${p.id === state.me?.id ? "disabled" : ""}>`
    + `<span class="pa">${esc(p.avatar || (p.role === "kid" ? "🧒" : "🧑"))}${dot(p.id)}</span><span class="pn">${esc(p.name)}`
    + `${p.id === state.me?.id ? "<small>（我）</small>" : ""}</span><small class="pr">${p.role === "kid" ? "" : "家长"}</small></button></li>`).join("")
    + `<li><button type="button" disabled><span class="pa"><img src="static/icon.png" alt=""></span><span class="pn">小新</span><small class="pr">AI</small></button></li>`;
  for (const b of $("side-people").querySelectorAll("[data-to]:not([disabled])")) b.addEventListener("click", () => {
    const p = state.people[b.dataset.to];
    if (!p) return;
    closeSide();
    // 孩子点爸爸：输入框里填好「告诉爸爸：」，说完小新会留言给他（leave_message 工具，阶段 5）；家长点孩子就是普通发消息
    if (state.me?.role === "kid" || state.me?.kind === "admin") $("input").value = `告诉${p.name}：`;
    $("input").focus();
    updateSend();
  });
  // 爸妈发的文件（像微信「群文件」）：最近 5 个，新的在上
  const files = [];
  for (const m of [...conv.messages].reverse()) {
    if (m.role !== "user" || !m.files?.length || (state.people?.[m.from]?.role !== "parent")) continue;
    for (const f of m.files) files.push({ ...f, from: m.from, at: m.created });
    if (files.length >= 5) break;
  }
  $("side-files-sec").hidden = !files.length;
  $("side-files-h").textContent = state.me?.role === "parent" ? "发给孩子的" : "爸妈发的";
  $("side-files").innerHTML = files.slice(0, 5).map((f) => `<li><a href="${fileUrl(f, f.kind !== "image")}" ${f.kind === "image" ? `data-fid="${f.id}"` : ""}>`
    + (f.kind === "image" ? `<img src="api/files/${f.id}?v=thumb" alt="" loading="lazy">` : '<span class="fi">📄</span>')
    + `<span>${esc(f.name)}<small>${esc(person(f.from).name)} · ${esc(dayLabel2(f.at))}</small></span></a></li>`).join("");
  renderSideAlarm();
}

function dayLabel2(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.round((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  const hm = d.toTimeString().slice(0, 5);
  return days === 0 ? hm : days === 1 ? `昨天 ${hm}` : `${d.getMonth() + 1}月${d.getDate()}日`;
}

// 下一个闹钟：小朋友最常问「明天几点起」，侧栏一眼看到，点了打开闹钟面板
function renderSideAlarm() {
  const snap = typeof clk !== "undefined" ? clk.snap : null;
  const next = (snap?.alarms || []).filter((a) => a.enabled && a.next).sort((a, b) => a.next - b.next)[0];
  $("side-alarm-sec").hidden = !next;
  if (!next) return;
  const d = new Date(next.next * 1000), now = new Date();
  const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 86400000);
  const when = days === 0 ? "今天" : days === 1 ? "明天" : days === 2 ? "后天" : `${d.getMonth() + 1}月${d.getDate()}日`;
  $("side-alarm").innerHTML = `<b>${esc(when)} ${esc(next.time)}</b><span>${esc(next.label || next.repeat_label || "")}</span>`;
}
$("side-alarm").addEventListener("click", () => { closeSide(); openClock(); });

function hubFile(f) {
  // 原图传完 / 大图到了：本地记一下，气泡上「原图上传中」换成「查看原图」（图片分档前端接上后用）
  for (const m of state.conv?.messages || []) for (const x of m.files || []) if (x.id === f.id) Object.assign(x, f);
  renderSide();
}

function hubMessage(m, kind) {
  if (document.body.classList.contains("in-chats")) { if (kind === "msg") showChats(); return; }
  if (!state.conv) return;
  const known = state.conv.messages.findIndex((x) => x.id === m.id);
  if (known >= 0) state.conv.messages[known] = { ...state.conv.messages[known], ...m };
  else if (kind === "msg") state.conv.messages.push(m);
  if (kind !== "msg" || m.role === "note") return;
  if ($("messages").querySelector(`[data-mid="${CSS.escape(m.id)}"]`)) return;  // 本地已经画了
  if (m.role === "assistant" && m.via !== "print" && (state.sending || talk.row)) return;  // 自己这台正在流式出字，答完那条就是它
  if (m.role === "user" && m.via === "talk" && talk.on) return;      // 自己这台在自由对话：那句由 heard 画（带原声小喇叭）
  if (m.role === "user" && state.sending) return;                     // 自己刚发的，SSE 的 user 事件会画
  const row = appendMessage(m);
  scrollDown();
  markRead(state.conv);
  if (m.files?.length) renderSide();
  // 孩子的 pad：爸爸妈妈发来的话，用他们设的声音读一遍（docs/architecture.md 6.2 的 speak）
  if (m.role === "user" && m.text && m.speak !== false && state.me?.role !== "parent"
      && state.people?.[m.from]?.role === "parent" && prefs.autoread) sayLater(m, row);
}

// 读家长消息：小新正在说、在自由对话里、正在发消息，就等它说完再读，不打断
const sayQueue = [];
function sayLater(m, row, self = false) {
  sayQueue.push({ m, row, self });
  if (sayQueue.length === 1) sayNext();
}
function sayNext() {
  const job = sayQueue[0];
  if (!job) return;
  if (talk.on || state.sending || player.audio || player.stream) { setTimeout(sayNext, 1500); return; }
  const btn = job.row.querySelector('[data-act="say"]') || document.createElement("button");
  const p = person(job.m.from);
  speakAs(job.self ? job.m.text : `${p.name}说：${job.m.text}`, btn, job.self ? {} : { member: job.m.from }).finally(() => {
    const wait = () => { if (player.audio) { setTimeout(wait, 500); return; } sayQueue.shift(); sayNext(); };
    wait();
  });
}

async function hubCatchUp() {
  const last = [...(state.conv?.messages || [])].reverse().find((m) => m.id)?.id;
  if (!state.conv || !last) return;
  try {
    const d = await api(`api/thread?since=${encodeURIComponent(last)}${state.conv.kid ? "&kid=" + state.conv.kid : ""}`).then(json);
    if (d.id !== state.conv.id) return;
    if (!d.partial) { state.conv = d; renderThread(); return; }  // 太久没连，直接整条重画
    for (const m of d.messages) hubMessage(m, "msg");
  } catch (_) { /* 下次重连再补 */ }
}

function renderThread() {
  const conv = state.conv;
  const kid = (state.kids || []).find((k) => k.id === conv.kid);
  $("conv-title").textContent = kid && state.me?.role !== "kid" ? `${kid.name} 的群` : "我们家";
  state.online = new Set(conv.online || []);
  renderSide();
  $("messages").innerHTML = "";
  state.replaying = true;
  const marks = new Map((conv.epochs || []).filter((e) => e.start > 0).map((e) => [e.start, e]));
  conv.messages.forEach((m, i) => {
    if (marks.has(i)) appendDivider(marks.get(i));
    if (m.role === "note") return;  // [system] 现状消息只给模型看
    appendMessage(m);
  });
  if (marks.has(conv.messages.length)) appendDivider(marks.get(conv.messages.length));  // 刚翻页、还没说话
  if (!conv.messages.some((m) => m.role !== "note")) renderEmpty();
  scrollDown(true);
  requestAnimationFrame(() => { state.replaying = false; scrollDown(true); });
}

// 分界线：上面的小新已经不整段记着了（原文都在，压缩是写成摘要带过来，新篇章是重新开始）
function appendDivider(e) {
  const d = document.createElement("div");
  d.className = "epoch-divider";
  const label = e.kind === "new" ? "小新翻开了新的一页" : e.kind === "compact" ? "以上小新记成了摘要" : "新的一页";
  const when = e.at ? new Date(e.at * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  d.innerHTML = `<span>${label}${when ? ` · ${esc(when)}` : ""}</span>`;
  $("messages").appendChild(d);
}

function renderEmpty() {
  if ($("messages").querySelector(".msg")) return;
  $("messages").insertAdjacentHTML("beforeend", `<div class="empty"><img src="static/icon.png" alt=""><b>你好，我是小新</b>可以打字、发图片和文件，也可以按住说话</div>`);
}


function openSide() { $("side").classList.add("open"); $("scrim").hidden = false; }
function closeSide() { $("side").classList.remove("open"); $("scrim").hidden = true; }
$("menu").addEventListener("click", openSide);
$("scrim").addEventListener("click", closeSide);

$("free-talk").addEventListener("click", () => (talk.on ? hangUp() : startTalk()));

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
    // 气泡里只拉 480 的缩略图（~30 KB，ECS 只有 5 Mbps）；点开才拿大图（没原图就是 2048 的 screen）
    ? `<a href="${fileUrl(f)}" data-fid="${f.id}"><img class="att-img" src="api/files/${f.id}?v=thumb" alt="${esc(f.name)}" loading="lazy">`
      + `${origQueue.has(f.id) ? '<span class="orig">原图 0%</span>' : ""}</a>`
    : `<a class="att-file" href="${fileUrl(f, true)}"><b>📄</b><span>${esc(f.name)}<small>${sizeLabel(f.size)}</small></span></a>`).join("");
}

function appendMessage(m) {
  const box = $("messages");
  box.querySelector(".empty")?.remove();
  const row = document.createElement("div");
  // 群聊：自己（这台设备的主人）发的在右边；别人（爸爸、妈妈、孩子）发的在左边，带头像和名字，像微信群
  const mine = m.role === "user" && isMine(m);
  row.className = "msg " + (m.role !== "user" ? "ai" : mine ? "me" : "peer");
  if (m.id) row.dataset.mid = m.id;  // hub 推来的同一条就不再画一遍
  if (m.role === "user" && !mine) {
    const p = person(m.from || state.conv?.kid);  // 老消息没有 from：是这个群的孩子说的
    row.classList.add(p.role === "parent" ? "from-parent" : "from-kid");
    row.innerHTML = `<span class="avatar sm emoji" aria-hidden="true">${esc(p.avatar)}</span><div class="bubble">`
      + `<div class="who">${esc(p.name)}</div>${attachmentsHtml(m.files)}${m.text ? `<div class="text">${esc(m.text)}</div>` : ""}`
      + `${m.to === "xiaoxin" ? '<div class="via">问小新</div>' : ""}`
      + `${m.text && p.role === "parent" ? '<div class="actions"><button type="button" data-act="say">朗读</button></div>' : ""}</div>`;
    row.querySelector('[data-act="say"]')?.addEventListener("click", (e) => speakAs(m.text, e.currentTarget, { member: m.from }));
  } else if (m.role === "user") {
    // 自由对话的句子：气泡上方一个小喇叭，点了回放服务端收到的原声，并摊开端侧（pad 本机）认出的字，对照云端识别查输入问题
    const replay = m.via === "talk" && (m.audio || m.local != null)
      ? `<button type="button" class="replay" title="回放原声、看端侧识别">🔊</button>` : "";
    row.innerHTML = `<div class="bubble">${replay}${attachmentsHtml(m.files)}${m.text ? `<div class="text">${esc(m.text)}</div>` : ""}`
      + `<div class="asr-cmp" hidden></div>`
      + `${m.via === "voice" ? '<div class="via">🎤 语音输入</div>' : m.via === "talk" ? '<div class="via">📞 自由对话</div>' : ""}</div>`;
    if (replay) row.querySelector(".replay").addEventListener("click", (e) => replayUtterance(m, row, e.currentTarget));
  } else {
    row.innerHTML = `<img src="static/icon.png" alt="" class="avatar sm"><div class="bubble"><div class="text"></div>`
      + `<div class="sources" hidden></div>`
      + `<div class="actions"><button type="button" data-act="copy">复制</button><button type="button" data-act="read">朗读</button></div>`
      + `<div class="meta"></div></div>`;
    row.querySelector(".text").innerHTML = renderText(m.text || "");
    const t = m.timing;  // 自由对话存下来的首响（服务端视角，从说完算起）
    row.querySelector(".meta").textContent = (m.model || "")
      + (t ? `  文字首响 ${t.first_text ?? "-"}s · 语音首响 ${t.first_audio ?? "-"}s（服务端）` : "");
    renderSources(row, m.searches);
    for (const a of m.actions || []) showAction(row, a);
    row.dataset.reply = m.text || "";
    wireActions(row);
  }
  box.appendChild(row);
  return row;
}

// 这条是不是「我」发的：家长、孩子按 from 和自己的 id 比；管理员看的是孩子的视角（孩子的话在右边）。
// 老消息没有 from：当作这条线程的孩子说的。
function isMine(m) {
  const me = state.me;
  const from = m.from || state.conv?.kid;
  if (!me || me.kind === "admin") return !from || from === state.conv?.kid;
  return from === me.id;
}

function person(id) {
  const p = (state.people || {})[id];
  if (p) return { name: p.name, avatar: p.avatar || (p.role === "kid" ? "🧒" : "🧑"), role: p.role };
  return { name: id === "admin" ? "管理员" : "家人", avatar: "🧑", role: "parent" };
}

function replayUtterance(m, row, btn) {
  const cmp = row.querySelector(".asr-cmp");
  const local = (m.local || "").trim();
  cmp.innerHTML = `<div><b>云端</b>${esc(m.text || "")}</div>`
    + `<div><b>端侧</b>${local ? esc(local) : '<i>没有（端侧没断出这句，或不是 pad）</i>'}</div>`;
  cmp.hidden = false;
  if (player.btn === btn) { stopSpeaking(); return; }
  stopSpeaking();
  const cid = m.conv || state.conv?.id;
  if (!m.audio || !cid) { toast("这句没存原声"); return; }
  const audio = new Audio(`api/conversations/${cid}/audio/${m.id}`);
  player.audio = audio;
  player.btn = btn;
  btn.textContent = "⏹";
  audio.onended = () => { if (player.audio === audio) stopSpeaking(); };
  audio.play().catch((err) => { if (player.audio === audio) { stopSpeaking(); toast("放不了：" + err.message); } });
}

// 调了闹钟类工具：回答上方一张小卡片写清做了什么，点开闹钟面板
const ACTION_TAB = { alarm_set: "alarms", alarm_update: "alarms", alarm_delete: "alarms", timer_start: "timers",
  timer_control: "timers", stopwatch: "stopwatch" };
// ---------------------------------------------------------------- 打印：孩子的 pad 收到任务交给原生（Printer.java）打；
// 所有端在聊天里那条回答下面显示进度：排队 → 打印中 → 打好了 / 出错原因。

function onPrintEvent(job) {
  if (job.state === "queued" && state.me?.role !== "parent" && native?.print) native.print(JSON.stringify(job));
  if (job.state === "queued" && !native?.print && state.me?.role === "kid") toast("这台设备连不了打印机");
  for (const el of document.querySelectorAll(`[data-job="${CSS.escape(job.id)}"]`)) paintJob(el, job);
  state.jobs = { ...(state.jobs || {}), [job.id]: job };
  // 每个任务不单独提示：一批全部结束后小新说一句总结（print.summary）

}

// 一批打完（全部结束）：小新用自己的声音说一次总结。孩子说完「打印」可能跑去打印机那儿了，不一定看屏幕。
// 都成功、而且孩子正在跟小新说话（自由对话）就不打断；有失败的一定说。
function onPrintSummary(ev) {
  // 气泡由 hub 的 msg 事件画（这是小新的一条回复）；这里只负责在 pad 上念
  if (state.me?.role === "parent" || !prefs.autoread) return;
  if (ev.ok && talk.on) return;
  const row = $("messages").querySelector(`[data-mid="${CSS.escape(ev.id || "")}"]`) || document.createElement("div");
  sayLater({ text: ev.text, from: "xiaoxin", role: "assistant" }, row, true);
}

function paintJob(el, job) {
  const label = { queued: "⏳ 发给打印机…", printing: "🖨 打印中…", done: "✅ 打好了", error: "⚠️ " + (job.error || "没打成") }[job.state] || job.state;
  el.className = `print-job ${job.state}`;
  el.innerHTML = `<span>${esc(label)}</span><small>${esc(job.name)} · ${job.color ? "彩色" : "黑白"} · ${job.sheets} 张${job.auto_color ? "（自动选的）" : ""}</small>`;
}

// 练习纸卡片：标题 + 预览缩略图（点开大图）+ 「打印」按钮；家长多一个「答案」
async function worksheetCard(row, wid) {
  let d;
  try { d = await api(`api/worksheets/${wid}`).then(json); } catch (_) { return; }
  const card = document.createElement("div");
  card.className = "ws-card";
  card.innerHTML = `<a href="api/files/${d.page}" data-fid="${d.page}"><img src="api/files/${d.page}?v=thumb" alt=""></a>`
    + `<div class="ws-info"><b>${esc(d.title)}</b><small>${d.count} ${d.kind === "math" ? "道题" : d.kind === "essay" ? "篇" : "个词"}</small>`
    + `<div class="ws-btns"><button type="button" class="primary" data-ws-print>🖨 打印</button>`
    + (d.answers ? `<a href="api/files/${d.answers}" data-fid="${d.answers}" class="ws-ans">答案</a>` : "") + `</div></div>`;
  // 预览图是服务端排的页：查看器要能找到它（findFile 只看消息里的 files），记一下
  state.extraFiles = { ...(state.extraFiles || {}), [d.page]: { id: d.page, name: d.title, kind: "image" },
                       ...(d.answers ? { [d.answers]: { id: d.answers, name: d.title + "（答案）", kind: "image" } } : {}) };
  card.querySelector("[data-ws-print]").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try { await api("api/print", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: "w_" + wid, kid: state.conv?.kid }) }); } catch (err) { toast(err.message); }
    e.currentTarget.disabled = false;
  });
  row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
  requestAnimationFrame(() => revealRow(row));
}

function showAction(row, a) {
  if (a.name === "make_exercise" || a.name === "make_worksheet") {
    const wid = /w_([0-9a-f]{16})/.exec(a.result || "")?.[1];
    if (wid) { worksheetCard(row, wid); return; }
  }
  if (a.name === "print_file" && !/^失败/.test(a.result || "")) {
    const el = document.createElement("div");
    el.className = "print-job queued";
    // 任务 id 不在工具结果里：按这轮之后最新的任务认（hub 来了 print 事件就会刷新）
    el.dataset.pending = "1";
    el.innerHTML = `<span>🖨 ${esc((a.result || "").split("\n")[0].replace(/^已发给打印机：/, "").replace(/。?任务 .*$/, ""))}</span>`;
    row.querySelector(".bubble").insertBefore(el, row.querySelector(".bubble .text"));
    const jid = /j_([0-9a-f]{16})/.exec(a.result || "")?.[1];  // 老消息没有：只显示那句话，不猜是哪个任务
    if (jid) claimJob(el, jid);
    return;
  }
  if (a.name === "list_printables") return;
  if (a.name === "alarm_list") {  // 闹钟清单：插一张能改能删的卡（alarmcard.js），不出文字条
    if (typeof alarmCard === "function" && clk.snap) {
      row.querySelector(".bubble").insertBefore(alarmCard(), row.querySelector(".bubble .text"));
      requestAnimationFrame(() => revealRow(row));
    }
    return;
  }
  if (a.name === "query_time") {  // 报时：插一个会走的钟（timecard.js），不出文字条
    if (typeof timeCardFor === "function") {
      row.querySelector(".bubble").insertBefore(timeCardFor(a), row.querySelector(".bubble .text"));
      requestAnimationFrame(() => revealRow(row));
    }
    return;
  }
  if (a.name === "show_word_writing" && !/^失败/.test(a.result || "") && typeof englishCard === "function") {
    try {
      const args = JSON.parse(a.arguments || "{}");
      const card = englishCard(String(args.word || ""), String(args.meaning || ""));
      row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
      requestAnimationFrame(() => revealRow(row));
      return;
    } catch (_) { /* 参数坏了就显示文字 */ }
  }
  if (a.name === "show_stroke_order") {  // 笔顺：直接插一张能动的卡（hanzi.js）
    const card = typeof strokeCardFor === "function" ? strokeCardFor(a) : null;
    if (card) { row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text")); requestAnimationFrame(() => revealRow(row)); return; }
  }
  const ok = !a.result.startsWith("失败");
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "action-chip" + (ok ? "" : " bad");
  chip.textContent = (ok ? "⏰ " : "⚠ ") + a.result.replace(/^已/, "").replace(/^失败：/, "没成功：");
  chip.addEventListener("click", () => openClock(ACTION_TAB[a.name]));
  row.querySelector(".bubble").insertBefore(chip, row.querySelector(".bubble .text"));
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
  if (player.btn) player.btn.textContent = player.btn.classList.contains("replay") ? "🔊" : "朗读";
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
    this.schedule(buf);
  }
  pushBytes(arrayBuf) {  // 自由对话走 WebSocket 二进制帧，不是 base64
    if (this.stopped) return;
    const pcm = new Int16Array(arrayBuf);
    const buf = this.ctx.createBuffer(1, pcm.length, this.rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    this.schedule(buf);
  }
  schedule(buf) {
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    // 第一块或者断粮后重新起播时，留 60 ms 余量
    const at = Math.max(this.next, this.ctx.currentTime + 0.06);
    src.start(at);
    if (!this.started) {  // 第一块真正出声的时刻（performance.now 时间轴），首响计时用
      this.started = true;
      this.onStart?.(performance.now() + (at - this.ctx.currentTime) * 1000);
    }
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

function speak(text, btn) { return speakAs(text, btn, {}); }

// opts.voice：指定音色（试听）；opts.member：用这位家长设的音色（孩子 pad 读爸爸妈妈的消息）
async function speakAs(text, btn, opts) {
  if (player.btn === btn) { stopSpeaking(); return; }
  stopSpeaking();
  if (!text.trim()) { toast("这条没有可读的字"); return; }
  const seq = player.seq;
  player.btn = btn;
  btn.textContent = "生成中…";
  try {
    const r = await api("api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 600), ...opts }) });
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

// 聊天区「粘底」，和微信一样：默认一直跟着最新的往下走（流式出字、TTS 念的时候、卡片插进来、图片加载完都跟）；
// 只有用户手指往上翻才停，右下角出「↓ 回到最新」；翻回底部或者开始新一轮就又粘上。
// 原来按「离底 < 160px 才跟」判断：一张 200 多 px 的卡插进来就超了，之后整轮都不跟（2026-10-31 两次反馈）。
const chatStick = { on: true, programmatic: 0 };

function chatToBottom(smooth) {
  const c = $("chat");
  chatStick.programmatic = performance.now();
  c.scrollTo({ top: c.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

function scrollDown(force) {
  if (force) chatStick.on = true;
  if (chatStick.on) chatToBottom(false);
  $("chat-latest").hidden = chatStick.on;
}

// 卡片插进来：卡比屏高（笔顺卡）就从卡的开头看起，并且这一轮不再粘底（不然会把卡顶出去）；放得下就照常粘底
function revealRow(row) {
  const c = $("chat");
  if (!row || !c.contains(row) || state.replaying) return;  // 打开时回放历史：不追着老卡跑，停在最新
  const cr = c.getBoundingClientRect(), rr = row.getBoundingClientRect();
  if (rr.height <= c.clientHeight - 16) { scrollDown(); return; }
  if (!chatStick.on) return;
  chatStick.on = false;
  chatStick.programmatic = performance.now();
  c.scrollTo({ top: c.scrollTop + (rr.top - cr.top) - 8, behavior: "smooth" });
  $("chat-latest").hidden = false;
}

(() => {
  const c = $("chat");
  // 判断「用户在往上翻」看手指 / 滚轮本身，不看 scroll 事件：流式出字时程序每几十 ms 就滚一次，
  // 用 scroll 事件区分不了是谁滚的（原来 300ms 窗口在出字时一直有效，手指划了也被拉回去）。
  const off = () => { chatStick.on = false; $("chat-latest").hidden = false; };
  let touchY = null, touching = false;
  c.addEventListener("touchstart", (e) => { touching = true; touchY = e.touches[0].clientY; }, { passive: true });
  c.addEventListener("touchmove", (e) => {
    const y = e.touches[0].clientY;
    if (touchY != null && y - touchY > 6) off();  // 手指往下拖 = 内容往上翻
    touchY = y;
  }, { passive: true });
  const settle = () => {  // 松手 / 惯性停了：回到了底部就重新粘上
    touching = false;
    setTimeout(() => {
      if (touching) return;
      if (c.scrollHeight - c.scrollTop - c.clientHeight < 40) { chatStick.on = true; $("chat-latest").hidden = true; }
    }, 350);
  };
  c.addEventListener("touchend", settle, { passive: true });
  c.addEventListener("touchcancel", settle, { passive: true });
  c.addEventListener("wheel", (e) => { if (e.deltaY < 0) off(); else settle(); }, { passive: true });
  // 拖滚动条、键盘翻页：scroll 事件兜底，只在不粘的时候看要不要重新粘
  c.addEventListener("scroll", () => {
    if (!chatStick.on && !touching && c.scrollHeight - c.scrollTop - c.clientHeight < 40) { chatStick.on = true; $("chat-latest").hidden = true; }
  }, { passive: true });
  // 内容变高（流式出字、TTS 念到后面、卡片、图片加载完）：粘着就补一下
  const ro = new ResizeObserver(() => { if (chatStick.on && !touching) chatToBottom(false); });
  ro.observe($("messages"));
  $("chat-latest").addEventListener("click", () => { chatStick.on = true; chatToBottom(true); $("chat-latest").hidden = true; });
})();

// ---------------------------------------------------------------- 发送

async function ensureConv() {
  if (!state.conv) await openThread();
  return state.conv;
}

function canSend() { return !state.sending && !state.pending.some((p) => !p.meta) && ($("input").value.trim() || state.pending.length); }
function updateSend() { $("send").disabled = !canSend(); }

async function send(text, via = "text") {
  if (state.sending) return;
  stopSpeaking();
  const files = state.pending.filter((p) => p.meta).map((p) => p.meta);
  if (!text && !files.length) return;
  // 家长在孩子的群里：默认是发给孩子（小新不接话），点亮「问小新」才是问小新
  // 话以「小新，」「@小新」开头也算问小新（和服务端 CALL_XIAOXIN 一样）
  const calls = /^\s*[@＠]?\s*小新(同学)?[\s,，:：、!！]/.test(text);
  if (state.me?.role === "parent" && !askOn() && !calls) return postOnly(text, files, via);
  state.sending = true;
  updateSend();
  const conv = await ensureConv();
  $("input").value = "";
  autoGrow();
  startOriginals(state.pending);
  state.pending = [];
  renderPending();

  const row = appendMessage({ role: "assistant", text: "" });
  scrollDown(true);  // 自己刚发了一句：不管之前翻到哪，回到底部跟着这一轮
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
  // 首响：从点发送算起。文字 = 第一个字上屏，语音 = 第一声出来（开了自动朗读才有）
  const tm = { t0: performance.now(), text: null, audio: null, model: "" };
  const showMeta = () => {
    const f = (x) => (x == null ? "…" : x.toFixed(1) + "s");
    const parts = [`文字首响 ${f(tm.text)}`];
    if (speakNow) parts.push(`语音首响 ${f(tm.audio)}`);
    meta.textContent = [tm.model, parts.join(" · ")].filter(Boolean).join("  ");
  };
  state.abort = abort;
  if (speakNow) {
    stopSpeaking();
    const seq = player.seq;
    player.btn = readBtn;
    readBtn.textContent = "停止";
    player.stream = new PcmStream(24000, () => { if (player.seq === seq) stopSpeaking(); });
    player.stream.abort = abort;
    player.stream.onStart = (at) => { tm.audio = (at - tm.t0) / 1000; showMeta(); };
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
          tm.model = data;
          showMeta();
        } else if (ev === "reasoning") {
          reasoning += data.length;
          if (!reply) textEl.innerHTML = `<span class="thinking">小新在想…（${reasoning} 字）</span>`;
        } else if (ev === "tool") {
          showAction(row, data);
        } else if (ev === "search") {
          if (data.state === "start") {
            if (!reply) textEl.innerHTML = `<span class="thinking">🔎 正在搜：${esc(data.queries.join("、"))}…</span>`;
          } else {
            searches.push(data);
            renderSources(row, searches);
            if (!reply) textEl.innerHTML = `<span class="thinking">${data.ok ? "搜到了，在整理…" : "没搜到，小新先按知道的说…"}</span>`;
          }
        } else if (ev === "delta") {
          if (tm.text == null) { tm.text = (performance.now() - tm.t0) / 1000; showMeta(); }
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
          if (data.id) row.dataset.mid = data.id;
          reply = data.text;
          textEl.innerHTML = renderText(reply);
          if (data.searches) renderSources(row, data.searches);
          if (data.ctx) renderCtx(data.ctx);  // 这一轮实际用了多少上下文
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
    if (row.querySelector(".ac, .hz, .tc-wrap")) requestAnimationFrame(() => revealRow(row));  // 卡 + 那句话都露出来
    if (row.dataset.finished) return;
    row.dataset.finished = "1";
    textEl.classList.remove("cursor");
    row.dataset.reply = row.classList.contains("error") ? "" : reply;
    state.sending = false;
    updateSend();
    scrollDown();
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
  if (file.size > 200 * 1024 * 1024) { toast(`${file.name} 超过 200 MB`); return; }
  const isImg = /^image\/(jpeg|png|webp|heic|heif|gif)$/i.test(file.type);
  const p = { file, meta: null, preview: isImg ? URL.createObjectURL(file) : "", isImg };
  state.pending.push(p);
  renderPending();
  try {
    p.meta = isImg ? await uploadImage(file) : await api("api/files", { method: "POST", body: file,
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name || "file") } }).then(json);
  } catch (err) {
    state.pending = state.pending.filter((x) => x !== p);
    toast(`${file.name} 上传失败：${err.message}`);
  }
  renderPending();
}

// ---------------------------------------------------------------- 图片分档上传（docs/architecture.md 6.5）
// 本地先压出 480 的缩略图和 2048 的大图（~0.4 MB）传上去，消息马上能发；原图只有勾了「原图」才传，
// 发出去之后在后台 512 KB 一片续传（断网、App 被杀，下次从断点接着传）。ECS 只有 5 Mbps，这样一张 12MP 照片多半只占 0.4 MB。

async function scaleJpeg(file, edge, q) {
  const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
  const k = Math.min(1, edge / Math.max(bmp.width, bmp.height));
  const w = Math.max(1, Math.round(bmp.width * k)), h = Math.max(1, Math.round(bmp.height * k));
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const g = c.getContext("2d");
  g.fillStyle = "#fff";  // 透明 PNG 转 JPEG：底色白
  g.fillRect(0, 0, w, h);
  g.drawImage(bmp, 0, 0, w, h);
  const out = { blob: await new Promise((r) => c.toBlob(r, "image/jpeg", q)), w: bmp.width, h: bmp.height };
  bmp.close?.();
  return out;
}

async function uploadImage(file) {
  let thumb, screen;
  try {
    [thumb, screen] = await Promise.all([scaleJpeg(file, 480, 0.7), scaleJpeg(file, 2048, 0.82)]);
  } catch (_) {  // 这台浏览器解不开（老系统的 HEIC）：整张传，服务端去做缩略图
    return api("api/files", { method: "POST", body: file,
      headers: { "Content-Type": file.type || "image/jpeg", "X-Filename": encodeURIComponent(file.name || "image.jpg") } }).then(json);
  }
  const meta = await api("api/files/image", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: file.name || "image.jpg", size: file.size, mime: file.type, w: thumb.w, h: thumb.h }) }).then(json);
  await api(`api/files/${meta.id}/thumb`, { method: "PUT", body: thumb.blob });
  await api(`api/files/${meta.id}/screen`, { method: "PUT", body: screen.blob });
  return { ...meta, variants: { thumb: thumb.blob.size, screen: screen.blob.size } };
}

const origQueue = new Map();  // fid → File：勾了原图、还没传完的（发出去以后后台传）
const ORIG_CHUNK = 512 * 1024;

async function sendOriginal(fid, file) {
  origQueue.set(fid, file);
  try {
    let o = await api(`api/files/${fid}/original`, { method: "POST" }).then(json);
    while (o.received < o.size) {
      if (talk.on) { await new Promise((r) => setTimeout(r, 3000)); continue; }  // 通话中先不传，让带宽
      const part = file.slice(o.received, Math.min(o.size, o.received + ORIG_CHUNK));
      o = await api(`api/files/${fid}/original?offset=${o.received}`, { method: "PUT", body: part }).then(json);
      origProgress(fid, o);
      await new Promise((r) => setTimeout(r, 1500));  // ≈ 2.7 Mbps 上限，不挤占聊天和语音
    }
    origQueue.delete(fid);
  } catch (e) {
    origProgress(fid, null, e.message);
    setTimeout(() => origQueue.has(fid) && sendOriginal(fid, file), 10000);  // 断网了：10 秒后从断点接着传
  }
}

function origProgress(fid, o, err) {
  for (const el of document.querySelectorAll(`[data-fid="${CSS.escape(fid)}"] .orig`)) {
    el.textContent = err ? "原图暂停，联网后继续" : o.received >= o.size ? "原图 ✓" : `原图 ${Math.floor(o.received / o.size * 100)}%`;
  }
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
  // 「原图」：和微信一样，默认不勾；勾了发出去以后后台传原图，对方才有「查看原图」。设置里可以改成默认勾
  const imgs = state.pending.filter((p) => p.isImg && p.meta?.variants);
  if (imgs.length) {
    const total = imgs.reduce((n, p) => n + p.file.size, 0);
    const lab = document.createElement("label");
    lab.className = "orig-pick";
    lab.innerHTML = `<input type="checkbox" ${state.sendOrig ? "checked" : ""}> 原图 <small>${sizeLabel(total)}</small>`;
    lab.querySelector("input").addEventListener("change", (e) => { state.sendOrig = e.target.checked; });
    box.appendChild(lab);
  }
  updateSend();
}

// 发出去以后：勾了原图的那几张开始后台传
function startOriginals(pending) {
  if (!state.sendOrig) return;
  for (const p of pending) if (p.isImg && p.meta?.variants) sendOriginal(p.meta.id, p.file);
  state.sendOrig = localGet("orig") === "1";
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

// 说话 / 打字两种输入栏。孩子的 pad 默认说话（大按钮常驻），家长和电脑默认打字；切过就记住（这台设备）
function setVoiceMode(on, focus = true) {
  state.voiceMode = on;
  document.body.classList.toggle("voice-first", on);
  $("mode").textContent = on ? "⌨" : "🎤";
  $("mode").setAttribute("aria-label", on ? "打字" : "按住说话");
  localSet("voiceMode", on ? "1" : "0");
  if (!on && focus) $("input").focus();
  if (on) $("input").blur();
}
$("mode").addEventListener("click", () => setVoiceMode(!state.voiceMode));
// 打字时内容空了、键盘收起：回到说话（孩子 pad 上）
$("input").addEventListener("blur", () => {
  if (state.me?.role !== "parent" && native && !$("input").value.trim() && !state.pending.length && localGet("voiceMode") !== "0")
    setTimeout(() => { if (document.activeElement !== $("input")) setVoiceMode(true, false); }, 200);
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
  holdBtn.querySelector(".hold-label").textContent = "松开 发送";
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
    holdBtn.classList.toggle("cancel", cancel);
    holdBtn.querySelector(".hold-label").textContent = cancel ? "松开 取消" : "松开 发送";
    $("rec-hint").textContent = cancel ? "松开取消" : "松开发送 · 上滑取消";
  }
});

function endHoldUi() {
  holdBtn.classList.remove("active", "cancel");
  holdBtn.querySelector(".hold-label").textContent = "按住 说话";
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
  holdBtn.querySelector(".hold-label").textContent = "识别中…";
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
    holdBtn.querySelector(".hold-label").textContent = "按住 说话";
    holdBtn.disabled = false;
  }
}
holdBtn.addEventListener("pointerup", endHold);
holdBtn.addEventListener("pointercancel", () => { hold.cancel = true; endHold(); });
holdBtn.addEventListener("contextmenu", (e) => e.preventDefault());

// ---------------------------------------------------------------- 自由对话
// 一条 /api/talk WebSocket 全双工：麦克风一直送，服务端断句、识别、回答、朗读，用户插话就打断（协议见 server/talk.py）。
// 在 pad 的「小新」APK 里，麦克风和喇叭交给原生的 window.XiaoxinNative（系统 AEC），网页只收事件、画界面；
// 在电脑浏览器里，就用 getUserMedia（浏览器自带 AEC）和 Web Audio。

const native = window.XiaoxinNative || null;
const talk = { on: false, ws: null, rec: null, stream: null, row: null, live: null, text: "", searches: [] };

function talkUi(on) {
  $("free-talk").classList.toggle("on", on);
  $("free-talk").textContent = on ? "■ 挂断" : "📞 自由对话";
  $("talk-bar").hidden = !on;
}
function talkStatus(s) { $("talk-status").textContent = s; }

async function startTalk() {
  if (talk.on) return;
  stopSpeaking();
  talk.on = true;
  talkUi(true);
  talkStatus("连接中…");
  const q = new URLSearchParams({ model: state.model });
  if (state.conv) q.set("conversation", state.conv.id);
  if (native) {
    native.start(new URL(`api/talk?${q}`, location.href).href);  // 原生那边连、录、放，事件回 window.xiaoxinTalkEvent
    return;
  }
  try {
    const ws = new WebSocket(wsUrl(`api/talk?${q}`));
    ws.binaryType = "arraybuffer";
    talk.ws = ws;
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") { talk.stream?.pushBytes(ev.data); return; }
      onTalkEvent(JSON.parse(ev.data));
    };
    ws.onclose = (ev) => { if (talk.on) { toast(ev.code === 4401 ? "要先用免密链接打开一次" : "自由对话断开了"); hangUp(); } };
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error("连不上")); });
    talk.rec = new Recorder(
      (peak) => drawTalkLevel(peak),
      (pcm) => { if (ws.readyState === WebSocket.OPEN) ws.send(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength)); },
    );
    await talk.rec.start();
  } catch (err) {
    toast(err.message);
    hangUp();
  }
}

function talkSend(obj) {
  const s = JSON.stringify(obj);
  if (native) native.send(s);
  else if (talk.ws?.readyState === WebSocket.OPEN) talk.ws.send(s);
}

// ---------------------------------------------------------------- 待命唤醒（只在 pad 的 APK 里有）
// 原生用本机 Paraformer 一直听，认出唤醒词开头才连 /api/talk，并把那句发过去（one-shot：「小新同学现在几点」直接答）。
// 唤醒后就是一通普通的自由对话，20 秒没动静自动挂断回待命。
const standby = { on: false };

// 待命中换模型 / 换对话：告诉原生，唤醒后用新的
function syncStandbyUrl() { if (native?.setTalkUrl) native.setTalkUrl(standbyUrl()); }

function standbyUrl() {
  const q = new URLSearchParams({ model: state.model });
  if (state.conv) q.set("conversation", state.conv.id);
  return new URL(`api/talk?${q}`, location.href).href;
}

async function setStandby(on) {
  standby.on = on;
  localSet("standby", on ? "1" : "");
  $("standby").setAttribute("aria-pressed", String(on));
  $("standby").textContent = on ? "👂 待命中" : "💤 待命";
  const e = await loadVoiceSettings();
  native.setWakeVad?.(e.wake_vad_ms || 300);  // 先设好，再开待命（老 APK 没这个方法就用它自己的默认）
  native.standby(on, e.wake_words.join(","), e.exit_words.join(","), standbyUrl(), e.wake_idle_s, e.vad_end_ms, e.hangup_tone);
  if (on) toast(`说「${e.wake_words.join("」或「")}」就能叫我，比如「${e.wake_words[0]}，现在几点」`);
}

// 语音相关设置（唤醒词、退出词、超时、提示音…）：读一次缓存起来，改了就推给 pad 原生，立刻生效
let voiceSettings = null;
async function loadVoiceSettings() {
  try { voiceSettings = await api("api/engines").then(json); }
  catch (_) { voiceSettings = voiceSettings || { wake_words: ["小新同学", "你好小新"], exit_words: ["小新退下", "没事了小新"], wake_idle_s: 30, vad_end_ms: 800, wake_vad_ms: 300, hangup_tone: "down" }; }
  return voiceSettings;
}
function pushVoiceSettings(e) {
  voiceSettings = e;
  native?.setWords?.(e.wake_words.join(","), e.exit_words.join(","), e.wake_idle_s, e.vad_end_ms, e.hangup_tone);
  native?.setWakeVad?.(e.wake_vad_ms || 300);
}

function onStandby(ev) {
  switch (ev.state) {
    case "loading": if (!talk.on) { $("talk-bar").hidden = false; talkStatus("加载本机识别…"); } break;
    case "listening": if (!talk.on) { $("talk-bar").hidden = false; talkStatus(`待命中，说「${ev.wake_word}」叫我`); } break;
    case "heard": break;  // 没唤醒的话不显示，免得屏幕上一直滚别人的话
    case "woken":
      // 原生已经在连了：网页这边切到「通话中」，事件照常从 xiaoxinTalkEvent 来
      talk.on = true;
      talkUi(true);
      talkStatus("我在…");
      break;
    case "error": toast(ev.detail); break;
    case "off": if (!talk.on) $("talk-bar").hidden = true; break;
    default: break;
  }
}

if (native?.standby) {
  $("standby").hidden = false;
  $("standby").addEventListener("click", () => setStandby(!standby.on));
  if (localGet("standby")) setTimeout(() => setStandby(true), 800);  // 上次开着，打开 app 就接着待命
  // 切到别的 App（分享、看相册）再回来：原生在后台停了监听，网页这边按记着的状态重新开（原来只在启动时恢复）
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && localGet("standby") && !talk.on) setTimeout(() => setStandby(true), 300);
  });
}

// 唤醒后一直没人说话：最后 10 秒在通话条上倒计时，说一句话就清掉
function showCountdown(left) {
  const el = $("talk-countdown");
  if (left == null || left < 0 || !talk.on) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = left > 0 ? `${left} 秒后回去待命` : "回去待命";
}

function hangUp(fromNative = false, quiet = false) {
  showCountdown(-1);
  if (!fromNative && !quiet && talk.on && !native) playCue("bye");
  if (!talk.on) return;
  talk.on = false;
  dropLive();
  talkUi(false);
  if (native && !fromNative) native.stop();
  if (standby.on) { $("talk-bar").hidden = false; talkStatus("待命中"); }
  try { talk.ws?.send(JSON.stringify({ type: "stop" })); } catch (_) { /* 已断 */ }
  talk.ws?.close();
  talk.rec?.stop().catch(() => {});
  talk.stream?.stop();
  talk.ws = talk.rec = talk.stream = null;
  finishTalkRow();
}

function drawTalkLevel(peak) {
  $("talk-level").style.transform = `scaleX(${Math.max(0.04, Math.min(1, peak * 3))})`;
}

// 首响：从你说完算起，文字首响 = 第一个字出现在屏幕上，语音首响 = 第一声从喇叭出来。
// 写在这一条回答的 meta 行里（模型名后面），服务端的分解（识别 / 搜索）也一起列上。
function showTiming() {
  const row = talk.row;
  if (!row) return;
  const f = (x) => (x == null ? "…" : x.toFixed(1) + "s");
  const s = talk.server;
  const parts = [`文字首响 ${f(talk.firstText)}`, `语音首响 ${f(talk.firstAudio)}`];
  if (s) parts.push(`识别 ${f(s.asr)}` + (s.search ? ` · 搜索 ${f(s.search)}` : ""));
  const model = s?.model || row.querySelector(".meta").dataset.model || "";
  row.querySelector(".meta").dataset.model = model;
  row.querySelector(".meta").textContent = [model, parts.join(" · ")].filter(Boolean).join("  ");
  talk.server = null;
}

// 挂断时还在上屏的那句（没等到定稿）：灰掉
function dropLive() {
  const row = talk.live;
  talk.live = null;
  if (row) { row.classList.remove("live"); row.classList.add("rejected"); }
}

function finishTalkRow() {
  const row = talk.row;
  if (!row) return;
  if (row.querySelector(".ac, .hz, .tc-wrap")) requestAnimationFrame(() => revealRow(row));
  row.querySelector(".text").classList.remove("cursor");
  row.dataset.reply = talk.text;
  if (!talk.text) row.remove();
  talk.row = null;
}

// 服务端事件。浏览器自己连时从 ws.onmessage 来；APK 里由原生调 window.xiaoxinTalkEvent(json)
function onTalkEvent(ev) {
  switch (ev.type) {
    case "ready":
      talkStatus("在听，直接说");
      if (!state.conv || state.conv.id !== ev.conversation) {
        state.conv = { id: ev.conversation, messages: [] };
        localSet("conv", ev.conversation);
        if (!$("messages").querySelector(".msg")) $("messages").innerHTML = "";
      }
      break;
    case "listening": talkStatus("在听…"); break;
    case "partial": {
      // 一开口就上屏（豆包流式中间结果），和 carHer2 一样；说完由 heard 定稿，拒识（rejected）变灰
      if (!talk.live) {
        talk.live = appendMessage({ role: "user", text: "", via: "talk" });
        talk.live.classList.add("live");
        if (talk.row) $("messages").insertBefore(talk.live, talk.row.nextSibling);  // 跟在小新正在说的那句后面
      }
      talk.live.querySelector(".text") || talk.live.querySelector(".bubble").prepend(Object.assign(document.createElement("div"), { className: "text" }));
      talk.live.querySelector(".text").textContent = ev.text;
      scrollDown();
      break;
    }
    case "rejected": {
      const row = talk.live;
      talk.live = null;
      if (!row) break;
      row.classList.remove("live");
      row.classList.add("rejected");
      if (!ev.text && !row.querySelector(".text")?.textContent) { row.remove(); break; }
      row.querySelector(".via").textContent = ev.reason === "short" ? "📞 只听到一个字，没当回事" : "📞 没听清，没当回事";
      break;
    }
    case "interrupt":
      talkStatus("你说，我听着");
      talk.stream?.stop();
      talk.stream = null;
      finishTalkRow();
      break;
    case "heard":
      finishTalkRow();
      // 首响计时的起点：用户说完。本地收到 heard 的时刻往回推识别花的时间（服务端给的 t_asr）
      talk.t0 = performance.now() - (ev.t_asr || 0) * 1000;
      talk.firstText = talk.firstAudio = null;
      {
        const final = appendMessage({ role: "user", text: ev.text, via: "talk", id: ev.id, local: ev.local, audio: ev.audio,
          conv: state.conv?.id });
        if (talk.live) { talk.live.replaceWith(final); talk.live = null; }  // 上屏的那条定稿（带小喇叭）
      }
      talk.row = appendMessage({ role: "assistant", text: "" });
      talk.row.querySelector(".text").classList.add("cursor");
      talk.row.querySelector(".text").innerHTML = '<span class="thinking">小新在想…</span>';
      talk.text = "";
      talk.searches = [];
      talkStatus("想一想…");
      scrollDown(true);
      break;
    case "tool": if (talk.row) showAction(talk.row, ev); break;
    case "search":
      if (!talk.row) break;
      if (ev.state === "start") {
        if (!talk.text) talk.row.querySelector(".text").innerHTML = `<span class="thinking">🔎 正在搜：${esc(ev.queries.join("、"))}…</span>`;
        talkStatus("上网查一下…");
      } else {
        talk.searches.push(ev);
        renderSources(talk.row, talk.searches);
      }
      break;
    case "delta":
      if (!talk.row) break;
      if (talk.firstText == null) { talk.firstText = (performance.now() - talk.t0) / 1000; showTiming(); }
      talk.text += ev.text;
      talk.row.querySelector(".text").innerHTML = renderText(talk.text);
      scrollDown();
      break;
    case "reply":
      if (ev.ctx) renderCtx(ev.ctx);
      if (talk.row) { talk.row.dataset.mid = ev.message.id || ""; talk.row.querySelector(".meta").dataset.model = ev.message.model || ""; showTiming(); }
      break;
    case "model": toast(`下一句用 ${state.models.find((m) => m.id === ev.model)?.label || ev.model}`); break;
    case "speaking":
      talkStatus("小新在说（可以直接打断）");
      if (!native) {
        talk.stream = new PcmStream(ev.sample_rate || 24000);
        talk.stream.onStart = (at) => { talk.firstAudio = (at - talk.t0) / 1000; showTiming(); };
      }
      break;
    case "native_play":  // APK：第一块音频真正从喇叭出来了，delay_ms 是收到到出声的时间
      if (talk.t0 != null && talk.firstAudio == null) {
        talk.firstAudio = (performance.now() - talk.t0) / 1000;
        showTiming();
      }
      break;
    case "idle":
      talkStatus("在听，直接说");
      talk.stream?.close();
      if (ev.timing) { talk.server = ev.timing; showTiming(); }
      finishTalkRow();
      break;
    case "note": toast(ev.detail); break;
    case "error": toast(ev.detail); talkStatus(ev.detail); break;
    // 原生那边自己的状态
    case "native_level": drawTalkLevel(ev.level / 100); break;
    case "native_closed":
      if (talk.on) { toast(ev.reason || "自由对话断开了"); hangUp(true); }
      break;
    case "standby": onStandby(ev); break;
    case "exit": talkStatus("好的，退下了"); break;
    case "bye": if (!native) setTimeout(() => { if (talk.on) { toast("退下了，有事再叫我"); hangUp(); } }, 2500); break;
    case "wake_countdown": showCountdown(ev.left); break;
    case "asr_debug": onAsrDebug(ev); break;
    default: break;
  }
}
window.xiaoxinTalkEvent = onTalkEvent;

// ---------------------------------------------------------------- 录我的声音

const SAMPLE_TEXT = "今天天气不错，放学回家先把作业写完，再出去玩一会儿。数学练习册第二十三页，"
  + "语文抄写生字两遍，英语读课文十分钟。做完了记得检查一遍，有不会的题就来问我，我们一起想办法。";
const vrec = { rec: null, blob: null, levels: [], timer: null };

// ---------------------------------------------------------------- 设置

const PANES = ["general", "model", "talk", "wake", "voice", "prompt", "members", "account"];

function openSettings(pane = "general") {
  closeSide();
  $("set-autoread").checked = prefs.autoread;
  $("set-enter").checked = prefs.enter;
  showPane(PANES.includes(pane) ? pane : "general");
  if (!$("settings").open) $("settings").showModal();
}

function showPane(name) {
  for (const b of document.querySelectorAll(".settings-nav button")) b.setAttribute("aria-selected", String(b.dataset.pane === name));
  for (const p of document.querySelectorAll(".pane")) p.hidden = p.dataset.pane !== name;
  if (name !== "wake" && typeof asrDebug !== "undefined" && asrDebug.on) $("asr-debug").click();
  if (name === "talk" || name === "wake") loadEngines();
  if (name === "prompt") loadPrompts();
  if (name === "members") loadMembers();
  if (name === "model") renderModels();
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
  syncVoiceToNative(cat.current);
  const cur = cat.voices.find((v) => v.id === cat.current);
  const cosy = cat.engines?.tts === "cosyvoice";
  $("voice-current").innerHTML = `现在用：<b>${esc(cur?.name || "-")}</b>`
    + `（${cosy ? "CosyVoice" : "豆包"} 念）`
    + (cosy && !cat.clone_ready ? "，CosyVoice 没起来，先用豆包念" : "");
  // 每个音色只属于一个引擎，点哪个就切到它那边
  const groups = [
    ["CosyVoice（本机）", cat.voices.filter((v) => v.engine === "cosyvoice")],
    ["自己加的豆包音色", cat.voices.filter((v) => v.custom)],
    ["豆包音色（云端）", cat.voices.filter((v) => v.kind === "doubao" && !v.custom)],
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
  try {
    const cat = await api("api/voices").then(json);
    renderVoices(cat);
    renderParentVoices(cat);
  } catch (err) { toast(err.message); }
}

// 爸爸妈妈的声音：每位注册的家长一个下拉，选音色目录里的任意一个（豆包、CosyVoice 男声、录过的声音）；存在成员档案里
async function renderParentVoices(cat) {
  const box = $("parent-voices");
  if (state.me?.kind !== "admin") { box.innerHTML = '<p class="voice-tip">管理员才能改。</p>'; return; }
  let ms;
  try { ms = await api("api/members").then(json); } catch (e) { box.textContent = e.message; return; }
  const parents = ms.parents.filter((m) => m.status === "active");
  if (!parents.length) { box.innerHTML = '<p class="voice-tip">还没有注册家长。在「成员」里添加。</p>'; return; }
  const opts = (sel) => `<option value="">小新的声音</option>` + cat.voices.map((v) =>
    `<option value="${esc(v.id)}" ${v.id === sel ? "selected" : ""} ${v.ready ? "" : "disabled"}>${esc(v.name)} · ${v.engine === "doubao" ? "豆包" : "本机"}${v.ready ? "" : "（现在用不了）"}</option>`).join("");
  box.innerHTML = parents.map((m) => `<div class="row pv" data-id="${esc(m.id)}"><label><b>${esc(m.avatar || "🧑")} ${esc(m.name)}</b></label>`
    + `<span class="pv-ctl"><select aria-label="${esc(m.name)}的声音">${opts(m.voice || "")}</select>`
    + `<button type="button" class="pv-try" title="试听">▶</button></span></div>`).join("");
  for (const row of box.querySelectorAll(".pv")) {
    const sel = row.querySelector("select");
    sel.addEventListener("change", async () => {
      try {
        await api(`api/members/${row.dataset.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ voice: sel.value }) });
        if (state.people?.[row.dataset.id]) state.people[row.dataset.id].voice = sel.value;
        toast("改好了");
      } catch (e) { toast(e.message); }
    });
    row.querySelector(".pv-try").addEventListener("click", (e) => {
      const name = row.querySelector("b").textContent.trim().split(" ").pop();
      speakAs(`${state.kids?.[0]?.call || "宝贝"}，我是${name}，作业做完了吗？`, e.currentTarget, { voice: sel.value || undefined });
    });
  }
}

// ---------------------------------------------------------------- 唤醒词 / 退出词：一行一个，＋ 添加，命中任意一个就算

const WORD_KIND = { "wake-list": { key: "wake_words", name: "唤醒词", hint: "比如：小*同学" },
                    "exit-list": { key: "exit_words", name: "退出词", hint: "比如：小新退下" } };

function renderWords(id, words) {
  const box = $(id);
  if (!box || box.contains(document.activeElement)) return;  // 正在输入就别刷掉
  const k = WORD_KIND[id];
  box.innerHTML = "";
  (words || []).forEach((w, i) => box.appendChild(wordRow(id, w, i, words.length)));
  const add = document.createElement("button");
  add.type = "button";
  add.className = "word-add";
  add.textContent = `＋ 添加${k.name}`;
  add.addEventListener("click", () => {
    const row = wordRow(id, "", box.querySelectorAll(".word").length, 99);
    box.insertBefore(row, add);
    row.querySelector("input").focus();
  });
  box.appendChild(add);
}

function wordRow(id, word, i, total) {
  const k = WORD_KIND[id];
  const row = document.createElement("div");
  row.className = "word";
  row.innerHTML = `<input type="text" maxlength="8" autocomplete="off" spellcheck="false" aria-label="${k.name} ${i + 1}" placeholder="${k.hint}">`
    + `<button type="button" class="word-del" aria-label="删掉这个${k.name}">✕</button>`;
  const input = row.querySelector("input");
  input.value = word;
  input.addEventListener("change", () => saveWords(id));
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); input.blur(); } });
  row.querySelector(".word-del").addEventListener("click", () => {
    if ($(id).querySelectorAll(".word").length <= 1) { $("words-msg").textContent = `至少留一个${k.name}`; return; }
    row.remove();
    saveWords(id);
  });
  return row;
}

async function saveWords(id) {
  const k = WORD_KIND[id];
  const words = [...$(id).querySelectorAll(".word input")].map((x) => x.value.trim()).filter(Boolean);
  if (!words.length) return;
  try {
    const e = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [k.key]: words }) }).then(json);
    $("words-msg").textContent = `${k.name}：${e[k.key].join("、")}`;
    pushVoiceSettings(e);
    document.activeElement?.blur?.();
    renderWords(id, e[k.key]);
  } catch (err) { $("words-msg").textContent = err.message; }
}

// 挂断提示音：和 pad 原生（Earcon.java）同一组参数，[起始 Hz, 结束 Hz, 秒, 之后停顿秒]
const TONES = {
  down: [[659.25, 659.25, 0.14, 0.03], [440, 440, 0.20, 0]],
  drop: [[1200, 500, 0.18, 0]],
  soft: [[440, 440, 0.12, 0]],
  chime: [[783.99, 783.99, 0.11, 0.02], [659.25, 659.25, 0.11, 0.02], [523.25, 523.25, 0.22, 0]],
  none: [],
};
function playTone(id) {
  if (native?.playTone) { native.playTone(id); return; }
  const ctx = getAudioCtx();
  let t = ctx.currentTime + 0.02;
  for (const [f0, f1, dur, gap] of TONES[id] || TONES.down) {
    const osc = ctx.createOscillator(), gain = ctx.createGain();
    osc.frequency.setValueAtTime(f0, t);
    osc.frequency.linearRampToValueAtTime(f1, t + dur);
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.3, t + 0.012);
    gain.gain.linearRampToValueAtTime(0, t + dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + dur);
    t += dur + gap;
  }
}

function renderTones(e) {
  const box = $("tone-pick");
  if (!box) return;
  box.innerHTML = "";
  for (const tone of e.hangup_tones || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tone";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(tone.id === e.hangup_tone));
    b.textContent = (tone.id === "none" ? "" : "▶ ") + tone.name;
    b.addEventListener("click", async () => {
      playTone(tone.id);
      try {
        const n = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ hangup_tone: tone.id }) }).then(json);
        pushVoiceSettings(n);
        renderTones(n);
      } catch (err) { toast(err.message); }
    });
    box.appendChild(b);
  }
}

// 「在」「拜拜」：当前音色说的一个字。pad 上原生本机缓存着放；浏览器里现拉 /api/cue 放
const cueCache = {};
async function playCue(name) {
  if (native?.playCue) { native.playCue(name); return; }
  try {
    const voice = voiceSettings?.current_voice || "";
    const key = `${name}|${voice}`;
    if (!cueCache[key]) cueCache[key] = await api(`api/cue/${name}`).then((r) => r.arrayBuffer());
    const s = new PcmStream(24000);
    s.pushBytes(cueCache[key].slice(0));
    s.close();
  } catch (err) { toast(err.message); }
}
// ---------------------------------------------------------------- 端侧模型调试（只在 pad 的「小新」里有）
const asrDebug = { on: false };
if (native?.debugAsr) {
  $("asr-debug-box").hidden = false;
  $("asr-debug-na").hidden = true;
  $("asr-debug").addEventListener("click", () => {
    asrDebug.on = !asrDebug.on;
    $("asr-debug").textContent = asrDebug.on ? "停止" : "开始调试";
    if (asrDebug.on) $("asr-debug-log").innerHTML = "";
    native.debugAsr(asrDebug.on);
  });
  // 关掉设置或切到别的页就停，免得一直开着麦克风
  $("settings").addEventListener("close", () => { if (asrDebug.on) $("asr-debug").click(); });
}

function onAsrDebug(ev) {
  const set = (a, b = "") => { $("asr-debug-state").textContent = a; $("asr-debug-sub").textContent = b; };
  if (ev.state === "loading") set("加载本机模型…");
  else if (ev.state === "listening") set("在听，说吧", `停 ${ev.end_ms} 毫秒算一句说完`);
  else if (ev.state === "off") set("没开");
  else if (ev.state === "error") set("出错了", ev.detail);
  else if (ev.state === "heard") {
    // 只显示转写，去掉标点空格（和匹配唤醒 / 退出词时用的是同一串）
    const text = (ev.text || "").replace(/[，。！？、；：,.!?;:\s　]/g, "");
    if (!text) return;
    const li = document.createElement("li");
    li.innerHTML = `<span class="t">${esc(text)}</span>`;
    $("asr-debug-log").prepend(li);
  }
}

$("cue-zai").addEventListener("click", () => playCue("zai"));
$("cue-bye").addEventListener("click", () => playCue("bye"));

// 当前音色告诉 pad 原生，让它提前把这个音色的「在」「拜拜」存好
function syncVoiceToNative(voiceId) { native?.setVoice?.(voiceId || ""); }

// ---------------------------------------------------------------- 语音引擎

function renderEngines(e) {
  const fill = (sel, options, cur, ready) => {
    $(sel).innerHTML = options.map((o) => `<option value="${o.id}">${esc(o.name)}${ready[o.id] === false ? "（没起来）" : ""}</option>`).join("");
    $(sel).value = cur;
  };
  $("set-vad-end").value = e.vad_end_ms;
  if (document.activeElement !== $("set-hotwords")) {
    $("set-hotwords").value = (e.asr_hotwords || []).join("\n");
    $("set-hotwords").dataset.saved = $("set-hotwords").value;
    $("set-hotwords-save").hidden = true;
  }
  $("hotwords-count").textContent = `${(e.asr_hotwords || []).length} 个`;
  renderWords("wake-list", e.wake_words);
  renderWords("exit-list", e.exit_words);
  renderTones(e);
  $("set-wake-idle").value = e.wake_idle_s;
  $("set-wake-vad").value = e.wake_vad_ms ?? 300;
  [$("set-wake-vad").min, $("set-wake-vad").max] = e.wake_vad_ms_range || [150, 1500];
  [$("set-wake-idle").min, $("set-wake-idle").max] = e.wake_idle_s_range || [10, 300];
  [$("set-vad-end").min, $("set-vad-end").max] = e.vad_end_ms_range || [300, 2000];
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
$("set-wake-idle").addEventListener("change", async () => {
  try {
    const e = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wake_idle_s: Number($("set-wake-idle").value) }) }).then(json);
    renderEngines(e);
    pushVoiceSettings(e);
    toast(`${e.wake_idle_s} 秒没说话就挂断`);
  } catch (err) { toast(err.message); loadEngines(); }
});
$("set-vad-end").addEventListener("change", async () => {
  try {
    const e = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vad_end_ms: Number($("set-vad-end").value) }) }).then(json);
    renderEngines(e);
    pushVoiceSettings(e);
    toast(`停 ${e.vad_end_ms} 毫秒算说完，下一通电话起生效`);
  } catch (err) { toast(err.message); loadEngines(); }
});
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


// ---------------------------------------------------------------- 设置：提示词与工具

const promptView = { data: null, view: "parts", voice: false };

async function loadPrompts() {
  const q = state.conv?.id ? `?conversation=${state.conv.id}` : "";
  try { promptView.data = await api("api/prompts" + q).then(json); } catch (e) { $("prompt-view").textContent = e.message; return; }
  try { const c = state.conv?.id ? await api(`api/conversations/${state.conv.id}/ctx`).then(json) : null; $("set-ctx-limit").value = Math.round((c?.limit || 60000) / 1000); } catch (_) { /* 拿不到就空着 */ }
  renderPrompts();
}

$("set-ctx-limit").addEventListener("change", async () => {
  try {
    const r = await api("api/ctx_limit", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: Math.round(+$("set-ctx-limit").value * 1000) }) }).then(json);
    toast(`压缩阈值改成 ${r.limit / 1000}k`);
    loadCtx();
  } catch (e) { toast(e.message); }
});

function renderPrompts() {
  const d = promptView.data, box = $("prompt-view");
  if (!d) return;
  for (const b of document.querySelectorAll('[data-pane="prompt"] .seg button')) b.setAttribute("aria-selected", String(b.dataset.view === promptView.view));
  const chars = (t) => `<small>${t.length} 字</small>`;
  if (promptView.view === "parts") {
    box.innerHTML = d.parts.map((p) => `<details class="pm" open><summary><b>${esc(p.name)}.md</b>`
      + `<span class="pm-when">${esc(p.when)}</span>${chars(p.text)}</summary><pre class="sys-prompt">${esc(p.text)}</pre></details>`).join("");
  } else if (promptView.view === "system") {
    const text = promptView.voice ? d.system_voice : d.system;
    box.innerHTML = `<label class="pm-toggle"><input type="checkbox" class="switch" id="pm-voice" ${promptView.voice ? "checked" : ""}>`
      + `<span>语音通话时（多拼 voice.md）</span>${chars(text)}</label><pre class="sys-prompt">${esc(text)}</pre>`;
    $("pm-voice").addEventListener("change", (e) => { promptView.voice = e.target.checked; renderPrompts(); });
  } else if (promptView.view === "summary") {
    const s = d.summary;
    if (!s) { box.innerHTML = '<p class="voice-tip">先打开一段对话，这里显示它上一次压缩出来的摘要。</p>'; return; }
    const when = s.compacted_at ? new Date(s.compacted_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
    box.innerHTML = `<p class="voice-tip">小新的记忆翻到了第 <b>${s.epoch + 1}</b> 页${when ? `，最近一次 ${esc(when)}` : ""}。`
      + `下一轮发给主脑的是：system prompt + 这份摘要 + 摘要之后的原文。</p>`
      + (s.text ? `<pre class="sys-prompt">${esc(s.text)}</pre>` : '<p class="voice-tip">还没压缩过。左侧栏「压缩」按钮可以马上压一次。</p>');
  } else if (promptView.view === "compact") {
    box.innerHTML = `<p class="voice-tip">后台压缩时发给主脑（关思考）的提示词，<code>server/prompts/compact.md</code>。第一次走 INITIAL，之后走 UPDATE（旧摘要放在 &lt;previous-summary&gt; 里），6 个标题缺一个就算失败、不换上。</p>`
      + `<pre class="sys-prompt">${esc(d.compact || "")}</pre>`;
  } else {
    box.innerHTML = d.tools.map((t) => {
      const props = t.parameters.properties || {}, req = new Set(t.parameters.required || []);
      const rows = Object.entries(props).map(([k, v]) => `<li><code>${esc(k)}</code>${req.has(k) ? '<span class="pm-req">必填</span>' : ""}`
        + `<span>${esc(v.description || (v.enum ? v.enum.join(" / ") : v.type || ""))}</span></li>`).join("");
      return `<details class="pm"><summary><b>${esc(t.name)}</b><span class="pm-desc">${esc(t.description.split("\n")[0])}</span></summary>`
        + (rows ? `<ul class="pm-args">${rows}</ul>` : "") + `<pre class="sys-prompt">${esc(JSON.stringify(t.parameters, null, 2))}</pre></details>`;
    }).join("");
  }
}

for (const b of document.querySelectorAll('[data-pane="prompt"] .seg button')) {
  b.addEventListener("click", () => { promptView.view = b.dataset.view; renderPrompts(); });
}

// 识别热词：改了出「保存」，存好下一句识别就用
$("set-hotwords").addEventListener("input", () => {
  $("set-hotwords-save").hidden = $("set-hotwords").value.trim() === ($("set-hotwords").dataset.saved || "").trim();
});
$("set-hotwords-save").addEventListener("click", async () => {
  try {
    const words = $("set-hotwords").value.split(/[\n,，、;；]+/).map((w) => w.trim()).filter(Boolean);
    const e = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asr_hotwords: words }) }).then(json);
    $("set-hotwords").blur();
    renderEngines(e);
    toast(`热词存好了，${e.asr_hotwords.length} 个，下一句就生效`);
  } catch (err) { toast(err.message); }
});


// ---------------------------------------------------------------- 上下文电量（左侧栏）
// 和电池一样：绿 → 琥珀（过 60%）→ 红（过 85%）；压缩中变成流动的琥珀条加「压缩中…」，压完数字跳回去、旁边次数 +1
let ctxPoll = null;
async function loadCtx() {
  if (!state.conv?.id) { renderCtx(null); return; }
  try { renderCtx(await api(`api/conversations/${state.conv.id}/ctx`).then(json)); } catch (_) { renderCtx(null); }
}

function renderCtx(c) {
  const box = $("ctx");
  if (!c) { box.hidden = true; clearTimeout(ctxPoll); return; }
  box.hidden = false;
  const pct = Math.min(100, Math.round((c.tokens / c.limit) * 100));
  const busy = c.state === "running" || c.state === "ready";
  const level = busy ? "busy" : pct >= 85 ? "high" : pct >= 60 ? "mid" : "low";
  box.dataset.level = level;
  $("ctx-fill").style.width = `${busy ? 100 : Math.max(pct, 3)}%`;
  const fresh = !busy && !c.tokens && c.epoch;  // 刚压完，下一句答完才知道新的用量
  $("ctx-pct").textContent = busy ? "压缩中…" : fresh ? "刚压缩" : `${pct}%`;
  $("ctx-batt").setAttribute("aria-valuenow", String(pct));
  $("ctx-compact").disabled = busy || !c.tokens;
  const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));
  const ep = c.epoch ? ` · 第 ${c.epoch + 1} 页` : "";
  $("ctx-note").textContent = busy ? `正在后台写摘要，照常聊${ep}`
    : c.error && !/太短/.test(c.error) ? `上次压缩没成功：${c.error}`
    : fresh ? `下一句后更新用量${ep}` : `${k(c.tokens)} / ${k(c.limit)} token${ep}`;
  clearTimeout(ctxPoll);
  if (busy) ctxPoll = setTimeout(loadCtx, 2000);  // 压缩中：每 2 秒看一眼，压好就更新
  if (state.conv && (state.conv.epoch || 0) < c.epoch && !busy) {  // 刚压完：聊天里补一条分界线
    state.conv.epoch = c.epoch;
    appendDivider({ kind: "compact", at: Date.now() / 1000 });
    toast("小新把前面的聊天记成了摘要，设置里能看");
  }
}

$("ctx-compact").addEventListener("click", () => $("compact-dialog").showModal());

// 设置里「开始新篇章」：换一个干净的记忆窗口（可以带上摘要），聊天记录不动，只多一条分界线
$("new-chapter").addEventListener("click", () => $("chapter-dialog").showModal());
$("chapter-no").addEventListener("click", () => $("chapter-dialog").close());
$("chapter-yes").addEventListener("click", async () => {
  const yes = $("chapter-yes");
  yes.disabled = true;
  yes.textContent = $("chapter-keep").checked ? "在写摘要…" : "翻页中…";
  try {
    await api("api/thread/new_chapter", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_summary: $("chapter-keep").checked }) }).then(json);
    $("chapter-dialog").close();
    $("settings").close?.();
    await openThread(state.conv?.kid);
    toast("翻开了新的一页，小新从这里重新记");
  } catch (e) { toast(e.message, 4000); }
  finally { yes.disabled = false; yes.textContent = "翻开新的一页"; }
});
$("compact-no").addEventListener("click", () => $("compact-dialog").close());
$("compact-yes").addEventListener("click", async () => {
  $("compact-dialog").close();
  if (!state.conv?.id) return;
  try {
    const r = await api(`api/conversations/${state.conv.id}/compact`, { method: "POST" }).then(json);
    renderCtx(r);
    toast(r.started ? "开始压缩了，压好后下一句起生效" : (r.error || "现在压不了"));
  } catch (e) { toast(e.message); }
});


// ---------------------------------------------------------------- 调试面板（照 carHer2「调试」）
// 每一步：最近 20 轮，每轮里每次发给主脑的请求——用的模型、耗时、token、回了什么、调了什么工具；
// 点开看这次请求的原样：system、messages（[system] 现状消息、压缩摘要、工具结果都在里面）。
// 上下文：电量、压缩次数、上次摘要。提示词：分模块 / 完整 system / 压缩提示词。工具：注册的工具和参数。
const dbg = { view: "turns", data: null, prompts: null, timer: null, turns: [], hasMore: false, loading: false };

async function loadDebug() {
  const body = $("debug-body");
  if (!state.conv?.id) { body.innerHTML = '<p class="voice-tip">先打开或开始一段对话。</p>'; return; }
  try {
    const [d, p] = await Promise.all([api(`api/debug/${state.conv.id}?limit=3`).then(json),
      dbg.prompts ? Promise.resolve(dbg.prompts) : api(`api/prompts?conversation=${state.conv.id}`).then(json)]);
    dbg.data = d; dbg.prompts = p; dbg.turns = d.turns; dbg.hasMore = d.has_more;
  } catch (e) { body.textContent = e.message; return; }
  renderDebug();
}

// 往上翻到顶：再拿更早的 3 轮插在上面，滚动位置不跳
async function dbgLoadOlder() {
  if (dbg.loading || !dbg.hasMore || !dbg.turns?.length) return;
  dbg.loading = true;
  try {
    const d = await api(`api/debug/${state.conv.id}?limit=3&before=${dbg.turns[0].id}`).then(json);
    const body = $("debug-body"), flow = body.querySelector(".dflow");
    if (!flow) return;
    const keep = body.scrollHeight - body.scrollTop;
    flow.insertAdjacentHTML("afterbegin", d.turns.map(dbgTurn).join(""));
    dbg.turns = [...d.turns, ...dbg.turns];
    dbg.hasMore = d.has_more;
    body.querySelector(".dmore").hidden = !dbg.hasMore;
    body.querySelector(".dstart").hidden = dbg.hasMore;
    wireDbgFull(flow);
    body.scrollTop = body.scrollHeight - keep;
  } catch (e) { toast(e.message); } finally { dbg.loading = false; }
}

function wireDbgFull(root) {
  for (const b of root.querySelectorAll("[data-full]:not([data-wired])")) {
    b.dataset.wired = "1";
    b.addEventListener("click", () => dbgFull(b));
  }
}

function dbgMsg(m) {
  const role = m.role === "tool" ? "tool" : m.role;
  let text = typeof m.content === "string" ? m.content : Array.isArray(m.content) ? m.content.map((c) => c.text || "[图片]").join("\n") : "";
  let tag = role;
  if (role === "user" && text.startsWith("[system]")) tag = "system·现状";
  else if (role === "user" && text.startsWith("前面的对话已经压缩")) tag = "压缩摘要";
  if (m.tool_calls) text = m.tool_calls.map((c) => `→ ${c.function.name}(${c.function.arguments})`).join("\n") + (text ? "\n" + text : "");
  const cls = tag === "system·现状" ? "sys" : tag === "压缩摘要" ? "sum" : role;
  return `<div class="dm dm-${cls}"><span class="dm-role">${esc(tag)}</span><pre>${esc(text)}</pre></div>`;
}

// 一轮 = 时间线：参数行 → system / 工具（折叠）→ 这一轮的入参（[system] 现状、user）→ iter 1 模型输出 → tool 结果 → iter 2 …
// 每次 iter 的入参 = 上一次的入参 + 上一次的输出 + 工具结果，所以只画「新增」的部分，和 carHer2 调试器一样一步一步往下读
function dbgPre(text, max = 600) {
  const t = String(text ?? "");
  if (t.length <= max) return `<pre>${esc(t)}</pre>`;
  return `<details class="dl"><summary><pre>${esc(t.slice(0, max))}…</pre><span>展开（${t.length} 字）</span></summary><pre>${esc(t)}</pre></details>`;
}
function dbgContent(m) {
  if (typeof m.content === "string") return m.content;
  if (Array.isArray(m.content)) return m.content.map((c) => c.text || "[图片]").join("\n");
  return "";
}
function dbgLine(icon, label, cls, inner) {
  return `<div class="dl-row ${cls}"><span class="dl-lab">${icon} ${label}</span><div class="dl-box">${inner}</div></div>`;
}
function dbgInput(m) {
  const text = dbgContent(m);
  if (m.role === "user" && text.startsWith("[system]")) return dbgLine("🛈", "system·现状", "is-sys", dbgPre(text));
  if (m.role === "user" && text.startsWith("前面的对话")) return dbgLine("📦", "压缩摘要", "is-sum", dbgPre(text, 200));
  if (m.role === "user") return dbgLine("👤", "user", "is-user", dbgPre(text));
  return "";
}
// 一轮在对话流里 = 这一轮的输入（[system] 现状、👤 user）+ 🤖 assistant 一个块：里面 iter 1 → tool → iter 2 …
function dbgTurn(t) {
  const when = new Date(t.at * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  const c0 = t.calls[0] || {};
  let input = "";
  for (const m of c0.tail || []) input += dbgInput(m);
  let steps = "";
  t.calls.forEach((c, i) => {
    const u = c.usage || {};
    const meta = `<span class="dl-meta">${esc(c.served || c.model)} · ${c.seconds ?? "…"}s · ${u.prompt_tokens ? `${u.prompt_tokens.toLocaleString()} 入 / ${u.completion_tokens || 0} 出` : ""}`
      + `${c.reasoning_chars ? ` · 思考 ${c.reasoning_chars} 字` : ""} · 入参 ${c.n_messages} 条`
      + ` <button type="button" class="dl-full" data-full="${t.id}/${i}">完整入参</button></span>`;
    let out = "";
    if (c.error) out += `<div class="dl-err">失败：${esc(c.error)}</div>`;
    for (const tc of c.tool_calls || []) out += `<div class="dl-call">→ <b>${esc(tc.name)}</b> <code>${esc(tc.arguments)}</code></div>`;
    if (c.reply) out += dbgPre(c.reply, 800);
    if (!out) out = '<i class="dl-none">（没有输出）</i>';
    steps += dbgLine("🧠", `iter ${i + 1}`, "is-iter", meta + out);
    const next = t.calls[i + 1];
    if (c.tool_calls?.length) {
      const results = next ? (next.tail || []).filter((m) => m.role === "tool").slice(-c.tool_calls.length) : [];
      c.tool_calls.forEach((tc, k) => {
        const r = results[k];
        const text = r ? dbgContent(r) : ((t.actions || []).find((a) => a.name === tc.name)?.result || "（结果没记下）");
        steps += dbgLine("📥", "tool", "is-tool", `<div class="dl-tname">${esc(tc.name)} · ${String(text).length} 字</div>${dbgPre(text, 500)}`);
      });
    }
  });
  const tok = t.calls.reduce((n, c) => Math.max(n, c.usage?.prompt_tokens || 0), 0);
  const head = `<div class="da-head"><b>🤖 assistant</b><span>${when}</span><span>${t.voice ? "📞 语音" : "⌨ 文字"}</span>`
    + `<span>${esc(t.served || t.model)}</span><span>⟳ ${t.calls.length} iter</span>${tok ? `<span>${(tok / 1000).toFixed(1)}k token</span>` : ""}`
    + `${t.error ? `<i class="bad">${esc(t.error)}</i>` : ""}${t.done ? "" : '<span class="dl-live">进行中…</span>'}</div>`;
  return `<div class="dturn" data-tid="${t.id}">${input}<div class="da">${head}<div class="da-steps">${steps}</div></div></div>`;
}

// 顶上固定：本对话用的模型参数、system_prompt、tools（system 不变，取最新一轮的就行）
function dbgTop(t) {
  const c0 = t?.calls?.[0] || {};
  return `<div class="dl-params">${esc(c0.model || "")} · thinking=${esc(c0.thinking || "")} · max_tokens=${c0.max_tokens || ""} · ${esc(c0.provider || "")}</div>`
    + `<details class="pm"><summary><b>system_prompt</b><small>${(t?.system || "").length} 字（完整原文）</small></summary><pre class="sys-prompt">${esc(t?.system || "")}</pre></details>`
    + `<details class="pm"><summary><b>tools</b><small>${(c0.tools || []).length} 个</small></summary><div class="dl-tools">${(c0.tools || []).map((x) => `<code>${esc(x)}</code>`).join(" ")}</div></details>`;
}

async function dbgFull(btn) {
  const [tid, k] = btn.dataset.full.split("/");
  try {
    const f = await api(`api/debug/${state.conv.id}/${tid}/${k}`).then(json);
    const box = document.createElement("div");
    box.className = "dl-fullbox";
    box.innerHTML = `<div class="dm dm-system"><span class="dm-role">system</span><pre>${esc(f.system)}</pre></div>` + f.messages.map(dbgMsg).join("");
    btn.closest(".dl-box").appendChild(box);
    btn.remove();
  } catch (e) { toast(e.message); }
}

function renderDebug() {
  const body = $("debug-body"), d = dbg.data, p = dbg.prompts;
  for (const b of document.querySelectorAll("[data-dv]")) b.setAttribute("aria-selected", String(b.dataset.dv === dbg.view));
  if (!d) return;
  if (dbg.view === "turns") {
    // 对话流：最上面 system，往下 user / assistant（含 iter）交替，最新的在最下面；打开就在最底下，往上翻懒加载更早的
    const turns = dbg.turns || [];
    if (!turns.length) { body.innerHTML = '<p class="voice-tip">这段对话还没有记录（服务重启后清空；说一句就有）。</p>'; return; }
    body.innerHTML = `<div class="dtop">${dbgTop(turns[turns.length - 1])}</div>`
      + `<div class="dmore"${dbg.hasMore ? "" : " hidden"}>往上翻加载更早的…</div><div class="dstart"${dbg.hasMore ? " hidden" : ""}>—— 这段对话的记录从这里开始 ——</div>`
      + `<div class="dflow">${turns.map(dbgTurn).join("")}</div>`;
    wireDbgFull(body);
    body.scrollTop = body.scrollHeight;
    body.onscroll = () => { if (body.scrollTop < 120) dbgLoadOlder(); };
  } else if (dbg.view === "ctx") {
    const c = d.ctx || {};
    body.innerHTML = `<div class="dk"><span>已用</span><b>${(c.tokens || 0).toLocaleString()} token</b><span>阈值</span><b>${(c.limit || 0).toLocaleString()}</b>`
      + `<span>压缩过</span><b>${c.epoch || 0} 次</b><span>状态</span><b>${esc(c.state || "idle")}${c.error ? `（${esc(c.error)}）` : ""}</b>`
      + `<span>这个 epoch 从第</span><b>${d.epoch_start} 条消息起</b></div>`
      + `<h4>上次压缩的摘要</h4>` + (d.summary ? `<pre class="sys-prompt">${esc(d.summary)}</pre>` : '<p class="voice-tip">还没压缩过。</p>');
  } else if (dbg.view === "prompt") {
    body.innerHTML = `<h4>完整 system prompt（文字对话）</h4><pre class="sys-prompt">${esc(p.system)}</pre>`
      + `<h4>语音通话多拼的</h4><pre class="sys-prompt">${esc((p.parts.find((x) => x.name === "voice") || {}).text || "")}</pre>`
      + p.parts.filter((x) => x.name !== "voice").map((x) => `<details class="pm"><summary><b>${esc(x.name)}.md</b><small>${x.text.length} 字</small></summary><pre class="sys-prompt">${esc(x.text)}</pre></details>`).join("")
      + `<details class="pm"><summary><b>compact.md</b><small>压缩用</small></summary><pre class="sys-prompt">${esc(p.compact || "")}</pre></details>`;
  } else {
    body.innerHTML = p.tools.map((t) => `<details class="pm"><summary><b>${esc(t.name)}</b><span class="pm-desc">${esc(t.description.split("\n")[0])}</span></summary>`
      + `<pre class="sys-prompt">${esc(JSON.stringify(t.parameters, null, 2))}</pre></details>`).join("");
  }
}

function openDebug() {
  $("debug").hidden = false;
  document.body.classList.add("debug-on");
  dbg.prompts = null;
  loadDebug();
  clearInterval(dbg.timer);
  // 自动刷新（4 秒）：只在没有展开任何东西时刷，免得正在看的被冲掉
  dbg.timer = setInterval(() => {
    // 只在有新一轮时刷新；正在看的（展开了 system / 完整入参）不动
    if ($("debug").hidden || dbg.view !== "turns" || $("debug-body").querySelector(".pm[open], .dl[open], .dl-fullbox")) return;
    api(`api/debug/${state.conv?.id}?limit=2`).then(json).then((d) => {
      const body = $("debug-body"), flow = body.querySelector(".dflow");
      if (!flow) { dbg.turns = d.turns; dbg.hasMore = d.has_more; renderDebug(); return; }
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
      let changed = false;
      for (const t of d.turns) {
        const i = dbg.turns.findIndex((x) => x.id === t.id);
        if (i >= 0 && JSON.stringify(dbg.turns[i]) === JSON.stringify(t)) continue;
        changed = true;
        const html = dbgTurn(t);
        if (i >= 0) { dbg.turns[i] = t; flow.querySelector(`[data-tid="${t.id}"]`).outerHTML = html; }
        else { dbg.turns.push(t); flow.insertAdjacentHTML("beforeend", html); }
      }
      if (changed) { wireDbgFull(flow); if (atBottom) body.scrollTop = body.scrollHeight; }  // 在底下就跟着新的走；往上翻着看就不打扰
    }).catch(() => {});
  }, 4000);
}
function closeDebug() { $("debug").hidden = true; document.body.classList.remove("debug-on"); clearInterval(dbg.timer); }
$("open-debug").addEventListener("click", () => ($("debug").hidden ? openDebug() : closeDebug()));
$("debug-close").addEventListener("click", closeDebug);
$("debug-refresh").addEventListener("click", () => { dbg.prompts = null; loadDebug(); });
for (const b of document.querySelectorAll("[data-dv]")) b.addEventListener("click", () => { dbg.view = b.dataset.dv; renderDebug(); });


// ---------------------------------------------------------------- 配对（登录页）和成员管理（设置 → 成员，管理员才有）

$("pair-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("pair-code").value.trim();
  $("pair-msg").textContent = "配对中…";
  try {
    const r = await fetch("api/pair", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    // APK 里：token 交给原生存着（pad 的自由对话、闹钟也用它；家长端下次打开直接登）
    (window.XiaoxinNative || window.XiaoxinParent)?.setDeviceToken?.(d.token);
    $("pair-msg").textContent = `好了，这是${d.member.name}的设备`;
    setTimeout(() => location.reload(), 600);
  } catch (err) { $("pair-msg").textContent = err.message; }
});

const ROLE = { kid: "孩子", parent: "家长" };
const KIND = { pad: "平板", phone: "手机", web: "浏览器" };
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 120 ? "刚刚" : s < 3600 ? `${Math.round(s / 60)} 分钟前` : s < 86400 ? `${Math.round(s / 3600)} 小时前` : `${Math.round(s / 86400)} 天前`; };

async function loadMembers() {
  const box = $("mem-list");
  let d;
  try { d = await api("api/members").then(json); } catch (e) { box.textContent = e.message; return; }
  const row = (m) => {
    const off = m.status !== "active";
    const devs = m.devices.map((x) => `<li><span>${esc(KIND[x.kind] || x.kind)} · ${esc(x.name)}</span><small>${esc(ago(x.last_seen))}</small>`
      + `<button type="button" data-revoke="${x.id}">踢下线</button></li>`).join("") || '<li class="none">还没有设备</li>';
    return `<div class="mem${off ? " off" : ""}" data-id="${m.id}"><div class="mem-head"><span class="mem-av">${esc(m.avatar || (m.role === "kid" ? "🧒" : "🧑"))}</span>`
      + `<b>${esc(m.name)}</b><span class="mem-role">${ROLE[m.role]}${m.grade ? " · " + esc(m.grade) : ""}${off ? " · 已停用" : ""}</span>`
      + `<span class="mem-btns">${off ? `<button type="button" data-act="on">恢复</button>`
        : `<button type="button" data-act="pair">加设备</button><button type="button" data-act="edit">改</button><button type="button" data-act="off">停用</button>`}</span></div>`
      + `<div class="mem-code" hidden></div><ul class="mem-devs">${devs}</ul></div>`;
  };
  box.innerHTML = `<h4>孩子</h4>${d.kids.map(row).join("") || '<p class="voice-tip">还没有</p>'}<h4>家长</h4>${d.parents.map(row).join("") || '<p class="voice-tip">还没有，下面添加</p>'}`;
  for (const el of box.querySelectorAll(".mem")) {
    const id = el.dataset.id;
    el.querySelector(".mem-btns").addEventListener("click", async (e) => {
      const act = e.target.dataset.act;
      try {
        if (act === "pair") {
          const kind = (state.members_kind = prompt("什么设备？输入 pad / phone / web", el.querySelector(".mem-role").textContent.startsWith("孩子") ? "pad" : "phone") || "").trim();
          if (!kind) return;
          const p = await api(`api/members/${id}/pair`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind }) }).then(json);
          const c = el.querySelector(".mem-code");
          c.hidden = false;
          c.innerHTML = `配对码 <b>${p.code}</b>，5 分钟内在那台设备的小新登录页输入`;
        } else if (act === "edit") {
          const name = prompt("名字", el.querySelector("b").textContent);
          if (name == null) return;
          const call = prompt("小新怎么叫他", "") ?? "";
          await api(`api/members/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(call ? { name, call } : { name }) });
          loadMembers();
        } else if (act === "off") {
          if (!confirm("停用后他的设备都会退出，聊天记录和闹钟都留着，可以恢复。停用？")) return;
          await api(`api/members/${id}`, { method: "DELETE" });
          loadMembers();
        } else if (act === "on") {
          await api(`api/members/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: true }) });
          loadMembers();
        }
      } catch (err) { toast(err.message); }
    });
    for (const b of el.querySelectorAll("[data-revoke]")) b.addEventListener("click", async () => {
      if (!confirm("让这台设备退出？要重新配对才能用。")) return;
      try { await api(`api/devices/${b.dataset.revoke}`, { method: "DELETE" }); loadMembers(); } catch (err) { toast(err.message); }
    });
  }
}

$("mem-role").addEventListener("change", () => { $("mem-grade").hidden = $("mem-role").value !== "kid"; });
$("mem-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("api/members", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: $("mem-role").value, name: $("mem-name").value, call: $("mem-call").value, grade: $("mem-grade").value }) });
    $("mem-name").value = $("mem-call").value = $("mem-grade").value = "";
    loadMembers();
  } catch (err) { toast(err.message); }
});


// ---------------------------------------------------------------- 家长端：聊天列表（每个孩子一个群）
// 未读 = 这台设备上次看这个群之后，别人（孩子、小新、另一个家长）发的条数；记在本机（localStorage），各设备各算各的。

function lastSeen(tid) { return +(localGet("seen." + tid) || 0); }
function markRead(conv) { const last = conv?.messages?.at(-1); if (last) localSet("seen." + conv.id, String(last.created || 0)); }

async function showChats() {
  $("chats").hidden = false;
  document.body.classList.add("in-chats");
  $("back-chats").hidden = false;
  hubConnect();
  const rows = await Promise.all((state.kids || []).map(async (k) => {
    try {
      const t = await api(`api/thread?kid=${encodeURIComponent(k.id)}`).then(json);
      const msgs = t.messages.filter((m) => m.role !== "note");
      const last = msgs.at(-1);
      if (!localGet("seen." + t.id)) markRead(t);  // 这台设备第一次见这个群：以前的不算未读（不然一装好就 99+）
      const unread = msgs.filter((m) => m.created > lastSeen(t.id) && m.from !== state.me.id).length;
      return { k, last, unread };
    } catch (_) { return { k, last: null, unread: 0 }; }
  }));
  $("chat-list").innerHTML = rows.map(({ k, last, unread }) => {
    const who = !last ? "" : last.role === "assistant" ? "小新：" : last.from && last.from !== k.id ? `${person(last.from).name}：` : "";
    const what = !last ? "还没有消息" : last.text ? last.text.replace(/\s+/g, " ").slice(0, 40) : last.files?.length ? "[图片 / 文件]" : "";
    return `<li><button type="button" data-kid="${esc(k.id)}"><span class="ca">${esc(k.avatar || "🧒")}</span>`
      + `<span class="cb"><span class="cn">${esc(k.name)}</span><span class="cl">${esc(who + what)}</span></span>`
      + `<span class="cr"><small>${last ? esc(dayLabel2(last.created)) : ""}</small>${unread ? `<i>${unread > 99 ? "99+" : unread}</i>` : ""}</span></button></li>`;
  }).join("") || '<li class="none">还没有孩子账号。管理员在「设置 → 成员」里添加。</li>';
  for (const b of $("chat-list").querySelectorAll("[data-kid]")) b.addEventListener("click", () => enterChat(b.dataset.kid));
}

async function enterChat(kid) {
  $("chats").hidden = true;
  document.body.classList.remove("in-chats");
  await openThread(kid);
}

$("back-chats").addEventListener("click", () => { markRead(state.conv); stopSpeaking(); showChats(); });
$("chats-settings").addEventListener("click", () => openSettings());


// ---------------------------------------------------------------- 家长发给孩子（不叫小新）

function askOn() { return $("ask").getAttribute("aria-pressed") === "true"; }
$("ask").addEventListener("click", () => {
  const on = !askOn();
  $("ask").setAttribute("aria-pressed", String(on));
  $("input").placeholder = on ? "问小新…（小新会回答，孩子也看得到）" : "发给孩子…（开头说「小新，」就是问小新）";
});

async function postOnly(text, files, via) {
  state.sending = true;
  updateSend();
  $("input").value = "";
  autoGrow();
  const sent = state.pending;
  state.pending = [];
  renderPending();
  try {
    const r = await api(`api/conversations/${state.conv.id}/messages`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, file_ids: files.map((f) => f.id), via, ask: false }) });
    const body = await r.text();
    const user = JSON.parse(/event: user\ndata: (.*)/.exec(body)?.[1] || "null");
    if (user && !$("messages").querySelector(`[data-mid="${CSS.escape(user.id)}"]`)) {
      state.conv.messages.push(user);
      appendMessage(user);
      markRead(state.conv);
    }
    startOriginals(sent);
    scrollDown(true);
  } catch (e) { toast(e.message); }
  finally { state.sending = false; updateSend(); }
}


// ---------------------------------------------------------------- 看大图（页内，不跳页）
// 先给 2048 的大图；有原图（勾了「原图」、已传完）才出「查看原图」，点了再下原图（几 MB，ECS 只有 5 Mbps）
function openViewer(f) {
  const o = f.original;
  const img = $("viewer-img");
  // 和微信一样瞬间打开：先放已经在缓存里的缩略图（气泡、侧栏刚显示过），大图下完再无缝换上，不出现黑屏等图
  img.src = `api/files/${f.id}?v=thumb`;
  const big = new Image();
  big.onload = () => { if ($("viewer").open && img.dataset.fid === f.id) img.src = big.src; };
  big.src = `api/files/${f.id}?v=screen`;
  img.dataset.fid = f.id;
  $("viewer-orig").hidden = !(o && o.received >= o.size);
  $("viewer-orig").href = `api/files/${f.id}`;
  $("viewer-orig").textContent = o ? `查看原图（${sizeLabel(o.size)}）` : "查看原图";
  $("viewer-note").textContent = o && o.received < o.size ? `对方原图上传中 ${Math.floor(o.received / o.size * 100)}%` : "";
  $("viewer").showModal();
}
$("viewer-close").addEventListener("click", () => $("viewer").close());
$("viewer").addEventListener("click", (e) => { if (e.target === $("viewer")) $("viewer").close(); });
$("viewer-orig").addEventListener("click", (e) => {
  e.preventDefault();
  $("viewer-img").src = $("viewer-orig").href;
  $("viewer-orig").hidden = true;
});
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-fid]");
  if (!a) return;
  e.preventDefault();
  const f = findFile(a.dataset.fid);
  if (f) openViewer(f);
}, true);  // 捕获阶段：抢在任何 target=_blank 的默认行为之前
function findFile(id) {
  for (const m of state.conv?.messages || []) for (const f of m.files || []) if (f.id === id) return f;
  return state.extraFiles?.[id] || null;
}

// 安卓返回键（小新家长 APK 先问这里）：关大图 > 关设置 > 群里回聊天列表；返回 true 表示处理了
window.xiaoxinBack = () => {
  if ($("viewer").open) { $("viewer").close(); return true; }
  if ($("settings").open) { $("settings").close(); return true; }
  if (document.body.classList.contains("parent-mode") && !document.body.classList.contains("in-chats")) { $("back-chats").click(); return true; }
  return false;
};


// ---------------------------------------------------------------- 微信等分享进来（小新家长 APK）
// 原生把分享的文件拷到缓存，这里经 bridge 按 1 MB 一段取回来拼成 File，再走和「相册选图」一样的上传
// （图片本地压 480 / 2048 先传，原图勾了才传；别的文件整个传）。

const shareBridge = () => window.XiaoxinParent?.sharePending ? window.XiaoxinParent : null;

async function shareFile(b, f) {
  const parts = [];
  for (let off = 0; off < f.size; off += 1 << 20) {
    const b64 = b.shareChunk(f.id, off, 1 << 20);
    if (!b64) throw new Error(`读不到 ${f.name}`);
    const bin = atob(b64), u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    parts.push(u);
    if (off % (8 << 20) === 0) await new Promise((r) => setTimeout(r));  // 大文件别卡住界面
  }
  return new File(parts, f.name, { type: f.mime });
}

let shareState = null;

async function checkShare() {
  const b = shareBridge();
  const raw = b?.sharePending();
  if (!raw) return;
  const d = JSON.parse(raw);
  if (!state.kids?.length) { toast("还没有孩子账号"); b.shareDone(); return; }
  shareState = { d, kid: state.conv?.kid && !document.body.classList.contains("in-chats") ? state.conv.kid : state.kids[0].id };
  $("share-files").innerHTML = d.files.map((f) => `<div class="sf">${/^image\//.test(f.mime) ? "🖼" : "📄"}<span>${esc(f.name)}<small>${sizeLabel(f.size)}</small></span></div>`).join("")
    || `<div class="sf">💬<span>${esc(d.text.slice(0, 80))}</span></div>`;
  $("share-text").value = d.text || "";  // 纯文字分享：就是要发的那段话；带文件的：一般是标题，可以改
  const imgs = d.files.filter((f) => /^image\/(jpeg|png|webp|heic|heif|gif)$/i.test(f.mime));
  $("share-orig-row").hidden = !imgs.length;
  $("share-orig").checked = localGet("orig") === "1";
  $("share-orig-size").textContent = imgs.length ? sizeLabel(imgs.reduce((n, f) => n + f.size, 0)) : "";
  const many = state.kids.length > 1;
  $("share-kids").hidden = !many;
  $("share-title").textContent = many ? "发给哪个孩子" : `发给${state.kids[0].name}`;
  $("share-kids").innerHTML = state.kids.map((k) => `<button type="button" role="radio" data-kid="${esc(k.id)}" aria-checked="${k.id === shareState.kid}">`
    + `<span>${esc(k.avatar || "🧒")}</span>${esc(k.name)}</button>`).join("");
  for (const btn of $("share-kids").querySelectorAll("[data-kid]")) btn.addEventListener("click", () => {
    shareState.kid = btn.dataset.kid;
    for (const x of $("share-kids").children) x.setAttribute("aria-checked", String(x === btn));
  });
  if (!$("share").open) $("share").showModal();
}
window.xiaoxinShareArrived = () => { checkShare().catch((e) => toast(e.message)); };

$("share-no").addEventListener("click", () => { $("share").close(); shareBridge()?.shareDone(); shareState = null; });
$("share-yes").addEventListener("click", async () => {
  if (!shareState) return;
  const b = shareBridge(), { d, kid } = shareState;
  const btn = $("share-yes");
  btn.disabled = true;
  try {
    btn.textContent = "准备中…";
    // 进到那个孩子的群（列表里的话就点进去），然后像自己选了图一样上传、发送
    if (document.body.classList.contains("in-chats") || state.conv?.kid !== kid) await enterChat(kid);
    state.pending = [];
    for (const f of d.files) {
      btn.textContent = `上传 ${f.name.slice(0, 12)}…`;
      await upload(await shareFile(b, f));
    }
    if (state.pending.some((p) => !p.meta)) throw new Error("有文件没传上去");
    state.sendOrig = $("share-orig").checked;
    $("share").close();
    if (askOn()) $("ask").click();  // 分享来的是给孩子看的，不问小新
    await send($("share-text").value.trim() || "");
    b.shareDone();
    shareState = null;
  } catch (e) { toast(e.message, 4000); }
  finally { btn.disabled = false; btn.textContent = "发送"; }
});

// pad 刚连上（开机、回到前台、断网重连）：把还没打的任务按顺序交给原生队列（原生按 id 去重，不会打两遍）
async function printCatchUp() {
  if (!native?.print || state.me?.role === "parent") return;
  try {
    const { jobs } = await api(`api/print/jobs?pending=1${state.conv?.kid ? "&kid=" + state.conv.kid : ""}`).then(json);
    for (const j of jobs) native.print(JSON.stringify(j));
  } catch (_) { /* 下次连上再补 */ }
}

// 打印条认领任务：最新的、还没被别的条认领的那个
// 打印条：按工具结果里的任务 id 认，拿它现在的状态（之后 hub 的 print 事件按 data-job 刷新）
async function claimJob(el, jid) {
  el.dataset.job = jid;
  delete el.dataset.pending;
  if (state.jobs?.[jid]) { paintJob(el, state.jobs[jid]); return; }
  try {
    const { jobs } = await api(`api/print/jobs${state.conv?.kid ? "?kid=" + state.conv.kid : ""}`).then(json);
    const job = jobs.find((j) => j.id === jid);
    if (job) paintJob(el, job);
  } catch (_) { /* 拿不到就显示工具那句 */ }
}

// 设置 → 唤醒：待命时停多久算一句。存服务端（全家的 pad 一样），本机马上换（不用重开待命）
$("set-wake-vad").addEventListener("change", async () => {
  try {
    const e = await api("api/engines", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wake_vad_ms: Number($("set-wake-vad").value) }) }).then(json);
    voiceSettings = e;
    native?.setWakeVad?.(e.wake_vad_ms);
    $("set-wake-vad").value = e.wake_vad_ms;
    toast(`待命时停 ${e.wake_vad_ms} 毫秒算一句，马上生效`);
  } catch (err) { toast(err.message); loadEngines(); }
});


// ---------------------------------------------------------------- 视频通话（docs/architecture.md 8.3）
// 家长：群里点 📹 → POST api/call → 自己先进房间等（全屏通话界面）。
// 孩子 pad：hub 来 call.invite → 全屏「爸爸来电」，倒计时 5→1 到 0 自动接通（免打扰时段只响铃，要手动接）；
//   接 → POST answer → 进同一个房间（calltest?embed=1，WebRTC：先 P2P，打不通走 ECS 中继）；不接 → POST reject。
// 来电时停待命、让出麦克风（native.standby(false)），挂断后恢复。任一方挂断 → POST end，线程里记一条「视频通话 N 秒」。
const call = { cur: null, timer: null, left: 0, wasStandby: false, ring: null };

function callUrl(c) {
  const q = new URLSearchParams({ room: c.room, embed: "1", name: c.caller === state.me?.id ? (state.kids.find((k) => k.id === c.kid)?.name || "孩子") : c.caller_name,
                                  who: state.me?.id || "x" });
  return `calltest?${q}`;
}

function showCall(mode) {
  $("call").hidden = false;
  $("call-ring").hidden = mode !== "ring";
  $("call-frame").hidden = mode !== "talk";
}

function closeCall() {
  clearInterval(call.timer);
  call.ring?.pause?.();
  $("call-frame").src = "about:blank";
  $("call").hidden = true;
  call.cur = null;
  if (call.wasStandby) { call.wasStandby = false; setStandby(true); }  // 恢复待命
}

async function enterCallRoom(c) {
  if (standby.on) { call.wasStandby = true; await setStandby(false); }  // 家长那边（或没响铃直接接）：也让出麦克风
  if (talk.on) hangUp();
  stopSpeaking();
  $("call-frame").src = callUrl(c);
  showCall("talk");
}

function onCallEvent(ev) {
  const c = ev.call;
  if (ev.type === "call.invite") {
    if (state.me?.role === "parent" || c.kid !== (state.conv?.kid)) return;  // 只有被叫的孩子的 pad 响
    call.cur = c;
    const p = state.people?.[c.caller];
    $("call-avatar").textContent = p?.avatar || "🧑";
    $("call-title").textContent = `${c.caller_name}来电`;
    if (talk.on) hangUp();  // 正在和小新自由对话：先打断
    stopSpeaking();
    if (standby.on) { call.wasStandby = true; setStandby(false); }  // 一响就停待命：不然唤醒监听占着麦克风，铃声里说的话还会被当成唤醒
    showCall("ring");
    native?.wakeScreen?.();
    speak(`${c.caller_name}打视频来啦`, document.createElement("button"));
    const auto = c.auto_answer_s || 0;
    call.left = auto;
    $("call-count").hidden = !auto;
    $("call-sub").textContent = auto ? "秒后自动接通" : "现在是休息时间，要接就点「现在接」";
    const R = 2 * Math.PI * 54;
    $("call-prog").style.strokeDasharray = R;
    $("call-prog").style.strokeDashoffset = 0;
    if (auto) {
      $("call-count").textContent = auto;
      clearInterval(call.timer);
      call.timer = setInterval(() => {
        call.left--;
        $("call-count").textContent = Math.max(0, call.left);
        $("call-prog").style.strokeDashoffset = R * (1 - Math.max(0, call.left) / auto);
        if (typeof hzPlay === "function") hzPlay(["#tick"]);
        if (call.left <= 0) { clearInterval(call.timer); answerCall(); }
      }, 1000);
    }
    return;
  }
  if (!call.cur || c.id !== call.cur.id) return;
  if (ev.type === "call.rejected" && state.me?.role === "parent") { toast(`${state.kids.find((k) => k.id === c.kid)?.name || "孩子"}现在不方便`); closeCall(); }
  if (ev.type === "call.missed" || ev.type === "call.ended") { if (!$("call").hidden) closeCall(); }
}

async function answerCall() {
  const c = call.cur;
  if (!c) return;
  clearInterval(call.timer);
  try { await api(`api/call/${c.id}/answer`, { method: "POST" }); } catch (e) { toast(e.message); closeCall(); return; }
  enterCallRoom(c);
}

$("call-yes").addEventListener("click", answerCall);
$("call-no").addEventListener("click", async () => {
  const c = call.cur;
  clearInterval(call.timer);
  if (c) { try { await api(`api/call/${c.id}/reject`, { method: "POST" }); } catch (_) { /* 已经结束了 */ } }
  closeCall();
});

// 家长：打给这个群的孩子
$("call-kid").addEventListener("click", async () => {
  try {
    const d = await api("api/call", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kid: state.conv?.kid }) }).then(json);
    call.cur = d.call;
    enterCallRoom(d.call);
    toast(`正在打给${state.kids.find((k) => k.id === d.call.kid)?.name || "孩子"}…`);
  } catch (e) { toast(e.message); }
});

// 通话界面里挂断（calltest 发来的）：告诉服务端结束，关掉
window.addEventListener("message", async (e) => {
  if (e.origin !== location.origin || e.data?.xiaoxinCall !== "ended") return;
  const c = call.cur;
  if (c) { try { await api(`api/call/${c.id}/end`, { method: "POST" }); } catch (_) { /* 对方先挂了 */ } }
  closeCall();
});
