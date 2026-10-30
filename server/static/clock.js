// 闹钟 / 倒计时 / 正计时面板。数据在服务端（/api/clock），这里长轮询拿变化、每秒本地走表。
// pad 上响铃由原生服务负责（熄屏也响，见 ClockService）；浏览器里只在页面开着时响。
"use strict";

const clk = { snap: null, tab: "alarms", editing: null, ringing: null, offset: 0, since: -1, polling: false };
const WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"];
const pad2 = (n) => String(n).padStart(2, "0");
const nowS = () => Date.now() / 1000 + clk.offset;   // 跟服务端对齐的「现在」

function fmtLeft(s) {
  s = Math.max(0, Math.ceil(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${pad2(m)}:${pad2(r)}` : `${pad2(m)}:${pad2(r)}`;
}
function fmtElapsed(s) {
  const cs = Math.floor((s % 1) * 100), t = Math.floor(s);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), r = t % 60;
  return { main: (h ? `${h}:${pad2(m)}` : pad2(m)) + `:${pad2(r)}`, cs: pad2(cs) };
}
function inWords(sec) {
  const m = Math.round(sec / 60);
  if (m < 60) return `${Math.max(1, m)} 分钟后`;
  const h = Math.floor(m / 60), r = m % 60;
  return `${h} 小时${r ? ` ${r} 分` : ""}后`;
}
function dayWord(ts) {
  const d = new Date(ts * 1000), n = new Date(nowS() * 1000);
  const days = Math.round((new Date(d.toDateString()) - new Date(n.toDateString())) / 86400000);
  return ["今天", "明天", "后天"][days] ?? `周${WEEKDAY[(d.getDay() + 6) % 7]}`;
}

// ---------------------------------------------------------------- 数据

function applySnap(s) {
  clk.snap = s;
  clk.offset = s.now - Date.now() / 1000;
  clk.since = s.version;
  renderClock();
  checkRinging();
  updateTopBadge();
}

async function clockApi(path, method = "POST", body) {
  try {
    const r = await api("api/clock" + path, {
      method, headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    applySnap(await r.json());
    return true;
  } catch (e) {
    toast(e.message);
    return false;
  }
}

async function pollClock() {
  if (clk.polling) return;
  clk.polling = true;
  for (;;) {
    try {
      const r = await api(`api/clock?since=${clk.since}&wait=${clk.since < 0 ? 0 : 50}`);
      applySnap(await r.json());
    } catch (_) {
      await new Promise((ok) => setTimeout(ok, 5000));
    }
  }
}

// ---------------------------------------------------------------- 打开 / 关闭

function openClock(tab) {
  if (tab) clk.tab = tab;
  $("clock").hidden = false;
  renderClock();
}
function closeClock() { $("clock").hidden = true; clk.editing = null; }

// ---------------------------------------------------------------- 渲染

function renderClock() {
  if ($("clock").hidden || !clk.snap) return;
  for (const b of document.querySelectorAll(".clk-tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === clk.tab));
  const live = clk.snap.timers.filter((t) => t.state !== "done").length;
  $("clk-timer-count").textContent = live ? live : "";
  const body = $("clk-body");
  if (clk.tab === "alarms") body.replaceChildren(...alarmsView());
  else if (clk.tab === "timers") body.replaceChildren(...timersView());
  else body.replaceChildren(...stopwatchView());
  tickClock();
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function alarmsView() {
  const out = [];
  const list = clk.snap.alarms;
  out.push(el('<p class="clk-hint">也可以直接跟小新说：「明天早上 7 点叫我」「工作日 6 点 40 的闹钟改成 7 点」「把闹钟都删了」</p>'));
  if (clk.editing === "new") out.push(alarmEditor(null));
  const box = el('<div class="alarms"></div>');
  for (const a of list) {
    if (clk.editing === a.id) { box.appendChild(alarmEditor(a)); continue; }
    const on = a.enabled || (a.snooze_at && a.snooze_at > nowS());
    const soon = a.next && a.next - nowS() < 12 * 3600;
    const when = a.next ? `<span class="${soon ? "soon" : ""}">${dayWord(a.next)}${soon ? " · " + inWords(a.next - nowS()) : ""}</span>` : "不会响";
    const row = el(`<div class="alarm${on ? "" : " off"}" role="button" tabindex="0">
      <span class="clk-num">${a.time}</span>
      <input type="checkbox" class="switch" aria-label="开关 ${a.time} 的闹钟" ${a.enabled ? "checked" : ""}>
      <span class="alarm-sub">${a.label ? `<span class="label">${esc(a.label)}</span> · ` : ""}${esc(a.repeat_label)} · ${when}</span>
      <span class="alarm-say">「${esc(a.say_text)}」${a.say ? "" : "<small>默认</small>"}</span>
    </div>`);
    row.querySelector(".switch").addEventListener("click", (e) => e.stopPropagation());
    row.querySelector(".switch").addEventListener("change", (e) => clockApi(`/alarms/${a.id}`, "PATCH", { enabled: e.target.checked }));
    const edit = () => { clk.editing = a.id; renderClock(); };
    row.addEventListener("click", edit);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") edit(); });
    box.appendChild(row);
  }
  if (!list.length && clk.editing !== "new") {
    out.push(el('<div class="clk-empty"><b>还没有闹钟</b>点下面加一个，或者跟小新说「明天 7 点叫我起床」</div>'));
  }
  out.push(box);
  if (clk.editing !== "new") {
    const add = el('<button type="button" class="clk-add">＋ 新闹钟</button>');
    add.addEventListener("click", () => { clk.editing = "new"; renderClock(); });
    out.push(add);
  }
  out.push(ringTextEditor());
  return out;
}

// 默认响铃提示：闹钟 / 倒计时没写自己的话时，响铃时小新说这句
function ringTextEditor() {
  const box = el(`<div class="ring-default">
    <label for="ring-default-in"><b>默认响铃提示</b><small>闹钟没写自己的话时，响铃时小新用当前音色说这句</small></label>
    <span class="rd-row"><input id="ring-default-in" maxlength="40"><button type="button" class="rd-play" aria-label="试听">▶</button><button type="button" class="rd-save" hidden>保存</button></span>
  </div>`);
  const inp = box.querySelector("input"), save = box.querySelector(".rd-save");
  inp.value = clk.snap.ring_text;
  inp.addEventListener("input", () => { save.hidden = inp.value.trim() === clk.snap.ring_text || !inp.value.trim(); });
  save.addEventListener("click", async () => { if (await clockApi("/ring_text", "PUT", { text: inp.value.trim() })) toast("默认响铃提示改好了"); });
  box.querySelector(".rd-play").addEventListener("click", () => sayPreview(inp.value.trim()));
  return box;
}

async function sayPreview(text) {
  if (!text) return;
  try {
    const buf = await api(`api/cue/ring?text=${encodeURIComponent(text)}`).then((r) => r.arrayBuffer());
    const s = new PcmStream(24000);
    s.pushBytes(buf);
    s.close();
  } catch (e) { toast(e.message); }
}

function alarmEditor(a) {
  const now = new Date(nowS() * 1000);
  const st = {
    h: a ? +a.time.slice(0, 2) : 7, m: a ? +a.time.slice(3) : 0,
    days: new Set(a ? a.repeat : []), label: a?.label || "", say: a?.say || "",
  };
  if (!a && now.getHours() >= 7 && now.getHours() < 20) { st.h = now.getHours() + 1; st.m = 0; }
  const box = el(`<div class="alarm-edit">
    <div class="ae-time">
      <div class="ae-col"><button type="button" data-d="h+1" aria-label="加一小时">︿</button>
        <input id="ae-h" class="clk-num" inputmode="numeric" maxlength="2" aria-label="小时"><button type="button" data-d="h-1" aria-label="减一小时">﹀</button></div>
      <span class="ae-colon clk-num">:</span>
      <div class="ae-col"><button type="button" data-d="m+5" aria-label="加五分钟">︿</button>
        <input id="ae-m" class="clk-num" inputmode="numeric" maxlength="2" aria-label="分钟"><button type="button" data-d="m-5" aria-label="减五分钟">﹀</button></div>
    </div>
    <div class="ae-days" role="group" aria-label="重复">${WEEKDAY.map((d, i) => `<button type="button" data-day="${i + 1}" aria-pressed="false">${d}</button>`).join("")}</div>
    <div class="ae-presets"><button type="button" data-p="">只响一次</button><button type="button" data-p="1,2,3,4,5">工作日</button><button type="button" data-p="6,7">周末</button><button type="button" data-p="1,2,3,4,5,6,7">每天</button></div>
    <input id="ae-label" class="ae-label" maxlength="10" placeholder="名字（可不填），比如 起床、练琴">
    <input id="ae-say" class="ae-label" maxlength="40" aria-label="响铃时小新说的话">
    <p class="ae-err" aria-live="polite"></p>
    <div class="ae-actions">${a ? '<button type="button" class="del">删除</button>' : ""}<button type="button" class="cancel">取消</button><button type="button" class="primary">${a ? "保存" : "添加"}</button></div>
  </div>`);
  const hIn = box.querySelector("#ae-h"), mIn = box.querySelector("#ae-m");
  const sync = () => {
    hIn.value = pad2(st.h); mIn.value = pad2(st.m);
    for (const b of box.querySelectorAll("[data-day]")) b.setAttribute("aria-pressed", String(st.days.has(+b.dataset.day)));
  };
  box.querySelector("#ae-label").value = st.label;
  const sayIn = box.querySelector("#ae-say");
  sayIn.value = st.say;
  sayIn.placeholder = `响铃时小新说（不填就说「${clk.snap.ring_text}」）`;
  for (const b of box.querySelectorAll("[data-d]")) b.addEventListener("click", () => {
    const [k, n] = [b.dataset.d[0], +b.dataset.d.slice(1)];
    if (k === "h") st.h = (st.h + n + 24) % 24; else st.m = (Math.round(st.m / 5) * 5 + n + 60) % 60;
    sync();
  });
  const readNum = (inp, max, key) => inp.addEventListener("change", () => {
    const v = parseInt(inp.value, 10);
    if (v >= 0 && v <= max) st[key] = v;
    sync();
  });
  readNum(hIn, 23, "h"); readNum(mIn, 59, "m");
  for (const b of box.querySelectorAll("[data-day]")) b.addEventListener("click", () => {
    const d = +b.dataset.day;
    st.days.has(d) ? st.days.delete(d) : st.days.add(d);
    sync();
  });
  for (const b of box.querySelectorAll("[data-p]")) b.addEventListener("click", () => {
    st.days = new Set(b.dataset.p ? b.dataset.p.split(",").map(Number) : []);
    sync();
  });
  box.querySelector(".cancel").addEventListener("click", () => { clk.editing = null; renderClock(); });
  box.querySelector(".del")?.addEventListener("click", async () => { if (await clockApi(`/alarms/${a.id}`, "DELETE")) { clk.editing = null; renderClock(); } });
  box.querySelector(".primary").addEventListener("click", async () => {
    const body = { time: `${pad2(st.h)}:${pad2(st.m)}`, repeat: [...st.days].sort(), label: box.querySelector("#ae-label").value.trim(),
      say: sayIn.value.trim() };
    if (a) body.enabled = true;
    const ok = await clockApi(a ? `/alarms/${a.id}` : "/alarms", a ? "PATCH" : "POST", body);
    if (ok) { clk.editing = null; renderClock(); }
  });
  sync();
  return box;
}

const R = 66, C = 2 * Math.PI * R;
function timersView() {
  const out = [];
  const grid = el('<div class="timers"></div>');
  for (const t of clk.snap.timers) {
    const card = el(`<div class="timer ${t.state}" data-id="${t.id}">
      <svg viewBox="0 0 150 150" aria-hidden="true"><circle class="ring-bg" cx="75" cy="75" r="${R}" fill="none" stroke-width="6"></circle>
        <circle class="ring" cx="75" cy="75" r="${R}" fill="none" stroke-width="6" stroke-linecap="round" stroke-dasharray="${C}" transform="rotate(-90 75 75)"></circle></svg>
      <div class="timer-face"><span class="clk-num" data-left></span><small>${esc(t.label || fmtLeft(t.duration))}</small></div>
      <div class="timer-btns"></div></div>`);
    const btns = card.querySelector(".timer-btns");
    const b = (text, fn, cls = "") => { const x = el(`<button type="button" class="${cls}">${text}</button>`); x.addEventListener("click", fn); btns.appendChild(x); };
    if (t.state === "done") b("关掉", () => clockApi(`/timers/${t.id}/cancel`), "go");
    else {
      b(t.state === "running" ? "暂停" : "继续", () => clockApi(`/timers/${t.id}/${t.state === "running" ? "pause" : "resume"}`), t.state === "paused" ? "go" : "");
      b("+1 分", () => clockApi(`/timers/${t.id}/add`, "POST", { seconds: 60 }));
      b("取消", () => clockApi(`/timers/${t.id}/cancel`));
    }
    grid.appendChild(card);
  }
  if (clk.snap.timers.length) out.push(grid);
  else out.push(el('<div class="clk-empty"><b>没有在走的倒计时</b>点一个时长马上开始，或者跟小新说「10 分钟后提醒我」</div>'));
  const quick = el('<div class="quick"></div>');
  for (const [label, s] of [["1 分钟", 60], ["3 分钟", 180], ["5 分钟", 300], ["10 分钟", 600], ["15 分钟", 900], ["30 分钟", 1800], ["1 小时", 3600]]) {
    const x = el(`<button type="button">${label}</button>`);
    x.addEventListener("click", () => clockApi("/timers", "POST", { seconds: s }));
    quick.appendChild(x);
  }
  const custom = el('<span class="quick-custom"><input id="tm-min" inputmode="numeric" placeholder="分钟" aria-label="自定义分钟"><button type="button">开始</button></span>');
  custom.querySelector("button").addEventListener("click", () => {
    const m = parseFloat(custom.querySelector("input").value);
    if (m > 0) clockApi("/timers", "POST", { seconds: Math.round(m * 60) });
  });
  quick.appendChild(custom);
  out.push(quick);
  return out;
}

function stopwatchView() {
  const sw = clk.snap.stopwatch;
  const box = el(`<div class="sw"><span class="clk-num" data-sw></span><div class="sw-btns"></div><ol class="laps"></ol></div>`);
  const btns = box.querySelector(".sw-btns");
  const b = (text, action, cls = "") => { const x = el(`<button type="button" class="${cls}">${text}</button>`); x.addEventListener("click", () => clockApi(`/stopwatch/${action}`)); btns.appendChild(x); };
  if (sw.state === "idle") b("开始", "start", "go");
  else if (sw.state === "running") { b("记圈", "lap"); b("暂停", "pause", "go"); }
  else { b("清零", "reset"); b("继续", "resume", "go"); }
  const laps = box.querySelector(".laps");
  sw.laps.slice().reverse().forEach((t, i, arr) => {
    const prev = arr[i + 1] ?? 0, f = fmtElapsed(t), d = fmtElapsed(t - prev);
    laps.appendChild(el(`<li><span>第 ${arr.length - i} 圈 +${d.main}.${d.cs}</span><b>${f.main}.${f.cs}</b></li>`));
  });
  return [box];
}

// ---------------------------------------------------------------- 每秒走表（不重画结构，只改数字）

function tickClock() {
  const n = nowS(), d = new Date(n * 1000);
  if (!$("clock").hidden) {
    $("clk-time").textContent = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    $("clk-date").textContent = `${d.getMonth() + 1} 月 ${d.getDate()} 日 星期${WEEKDAY[(d.getDay() + 6) % 7]}`;
  }
  if (!$("night").hidden) $("night-time").textContent = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (!clk.snap) return;
  const next = clk.snap.alarms.filter((a) => a.next).sort((x, y) => x.next - y.next)[0];
  $("clk-next").hidden = !next;
  if (next) {
    $("clk-next-what").textContent = `${dayWord(next.next)} ${next.time}${next.label ? " " + next.label : ""}`;
    $("clk-next-when").textContent = inWords(next.next - n);
  }
  for (const card of document.querySelectorAll(".timer[data-id]")) {
    const t = clk.snap.timers.find((x) => x.id === card.dataset.id);
    if (!t) continue;
    const left = t.state === "running" ? Math.max(0, t.ends_at - n) : t.state === "paused" ? t.remaining : 0;
    card.querySelector("[data-left]").textContent = t.state === "done" ? "时间到" : fmtLeft(left);
    card.querySelector(".ring").style.strokeDashoffset = String(C * (1 - left / t.duration));
  }
  const swEl = document.querySelector("[data-sw]");
  if (swEl) {
    const sw = clk.snap.stopwatch;
    const e = sw.state === "running" ? sw.elapsed + n - sw.started_at : sw.elapsed;
    const f = fmtElapsed(e);
    swEl.innerHTML = `${f.main}<small>.${f.cs}</small>`;
  }
}

// ---------------------------------------------------------------- 响铃（浏览器里；pad 由原生响，网页只显示）

function checkRinging() {
  if (!clk.snap) return;
  const n = nowS();
  const timer = clk.snap.timers.find((t) => t.state === "done" || (t.state === "running" && t.ends_at <= n));
  const alarm = clk.snap.alarms.find((a) => a.ringing);
  showRing(timer ? { kind: "timer", id: timer.id, title: "时间到", sub: timer.say_text }
    : alarm ? { kind: "alarm", id: alarm.id, title: alarm.time, sub: alarm.say_text } : null);
}

// 闹钟到点：服务端快照里的 next 是将来的时刻，过了就拉一次新快照，里面 ringing 会标出来
function alarmDue() {
  if (!clk.snap || clk.ringing) return;
  const n = nowS();
  if (clk.snap.alarms.some((a) => a.next && a.next <= n)) api("api/clock").then(json).then(applySnap).catch(() => {});
}

function showRing(r) {
  clk.ringing = r;
  $("ring").hidden = !r;
  if (!r) { stopBeep(); return; }
  $("night").hidden = true;
  $("ring-time").textContent = r.kind === "alarm" ? r.title : "时间到";
  $("ring-what").textContent = r.sub;
  $("ring-snooze").hidden = r.kind !== "alarm";
  if (!native) startBeep();  // pad 上原生在响
}

let beep = null;
function startBeep() {
  if (beep) return;
  try {
    const ctx = new AudioContext();
    beep = { ctx, timer: setInterval(() => {
      for (const at of [0, 0.25]) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
        g.gain.setValueAtTime(0.0001, ctx.currentTime + at);
        g.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + at + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.18);
        o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + 0.2);
      }
    }, 1000) };
  } catch (_) { beep = null; }
}
function stopBeep() { if (beep) { clearInterval(beep.timer); beep.ctx.close(); beep = null; } }

async function ringStop() {
  const r = clk.ringing;
  showRing(null);
  if (!r) return;
  if (r.kind === "timer") await clockApi(`/timers/${r.id}/cancel`);
  else await clockApi(`/alarms/${r.id}/dismiss`);
  native?.clockStop?.();
}
async function ringSnooze() {
  const r = clk.ringing;
  showRing(null);
  if (r?.kind === "alarm") await clockApi(`/alarms/${r.id}/snooze`, "POST", { minutes: 5 });
  native?.clockStop?.();
}

// ---------------------------------------------------------------- 熄屏（pad：关屏幕，原生服务接着管闹钟和待命）

function goNight() {
  if (native?.screenOff) { native.screenOff(); return; }
  $("night").hidden = false;  // 浏览器里关不了屏，盖一层全黑
  tickClock();
}

function updateTopBadge() {
  const b = $("open-clock");
  if (!b || !clk.snap) return;
  const live = clk.snap.timers.find((t) => t.state === "running");
  b.classList.toggle("live", !!live);
  b.title = live ? `倒计时还剩 ${fmtLeft(live.ends_at - nowS())}` : "闹钟和计时";
}

// pad 原生响铃 / 停止时通知网页
window.xiaoxinClockEvent = (ev) => {
  if (ev.type === "ring") showRing(ev.kind === "alarm"
    ? { kind: "alarm", id: ev.id, title: ev.title, sub: ev.say || ev.sub || "闹钟" }
    : { kind: "timer", id: ev.id, title: "时间到", sub: ev.say || ev.sub || "倒计时" });
  else if (ev.type === "stop") showRing(null);
};

// ---------------------------------------------------------------- 绑定

$("open-clock").addEventListener("click", () => openClock());
$("clk-close").addEventListener("click", closeClock);
$("clk-night").addEventListener("click", goNight);
$("night").addEventListener("click", () => { $("night").hidden = true; });
$("ring-stop").addEventListener("click", ringStop);
$("ring-snooze").addEventListener("click", ringSnooze);
for (const b of document.querySelectorAll(".clk-tabs button")) b.addEventListener("click", () => { clk.tab = b.dataset.tab; clk.editing = null; renderClock(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("clock").hidden && !clk.ringing) closeClock(); });
setInterval(() => { tickClock(); alarmDue(); updateTopBadge(); }, 1000);

// 设置 → 音色 里也能改默认响铃提示（和闹钟面板底下那个是同一个）
(() => {
  const inp = $("set-ring-text"), save = $("set-ring-save");
  if (!inp) return;
  const fill = () => { if (clk.snap && document.activeElement !== inp) inp.value = clk.snap.ring_text; };
  setInterval(fill, 1000);
  inp.addEventListener("input", () => { save.hidden = !inp.value.trim() || inp.value.trim() === clk.snap?.ring_text; });
  save.addEventListener("click", async () => { if (await clockApi("/ring_text", "PUT", { text: inp.value.trim() })) { save.hidden = true; toast("默认响铃提示改好了"); } });
  $("set-ring-play").addEventListener("click", () => sayPreview(inp.value.trim()));
})();
