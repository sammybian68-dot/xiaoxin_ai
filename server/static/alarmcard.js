// 闹钟清单卡：小新调了 alarm_list，回答上方插一张卡。数据和闹钟面板同一份（clock.js 的 clk.snap，长轮询同步），
// 在卡上改了、语音改了、面板里改了，三处一起变。序号和服务端 alarm_list 一致：按建的先后（改时间不换号）。
"use strict";

const AC_WEEK = ["一", "二", "三", "四", "五", "六", "日"];
const AC_REPS = [
  { id: "once", name: "只响一次" }, { id: "daily", name: "每天" },
  { id: "workday", name: "上学的日子", sub: "法定工作日" }, { id: "offday", name: "放假的日子", sub: "节假日和周末" },
  { id: "custom", name: "自选星期" },
];
const acCards = new Set();
const acEl = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const acPad = (n) => String(n).padStart(2, "0");
const acPer = (h) => (h < 5 ? "凌晨" : h < 9 ? "早上" : h < 12 ? "上午" : h < 13 ? "中午" : h < 18 ? "下午" : "晚上");

function acRepKind(rep) {
  if (rep === "workday" || rep === "offday") return rep;
  if (!rep || !rep.length) return "once";
  if (rep.length === 7) return "daily";
  return "custom";
}

function acOrdered() {
  const list = (clk.snap?.alarms || []).slice();
  return list.sort((a, b) => (a.created || 0) - (b.created || 0) || a.id.localeCompare(b.id));
}

function acNextText(a) {
  if (!a.next) return "";
  const s = a.next - nowS();
  const d = new Date(a.next * 1000);
  const day = dayWord(a.next);
  const rel = s < 3600 ? `${Math.max(1, Math.round(s / 60))} 分钟后` : s < 86400 ? `${Math.floor(s / 3600)} 小时后` : "";
  return `${day} ${acPad(d.getHours())}:${acPad(d.getMinutes())}${rel ? ` · ${rel}` : ""}`;
}

function alarmCard() {
  const card = acEl(`<section class="ac" aria-label="闹钟">
    <div class="ac-head"><b>⏰ 闹钟</b><span class="ac-count"></span></div>
    <div class="ac-list"></div>
    <button type="button" class="ac-add">＋ 新闹钟</button>
  </section>`);
  card._editing = null;
  card.querySelector(".ac-add").addEventListener("click", () => { card._editing = "new"; renderAlarmCard(card); });
  acCards.add(card);
  renderAlarmCard(card);
  return card;
}

function renderAlarmCard(card) {
  if (!card.isConnected && card._shown) { acCards.delete(card); return; }
  card._shown = true;
  const list = acOrdered();
  const on = list.filter((a) => a.next).length;
  card.querySelector(".ac-count").textContent = list.length ? `${list.length} 个，${on} 个开着` : "";
  const box = card.querySelector(".ac-list");
  // 正在编辑就不重画那一格（长轮询每次都来，别把用户正在拨的时间冲掉）
  if (card._editing && box.querySelector(".ac-edit")) {
    updateRowsOnly(card, list);
    return;
  }
  box.replaceChildren();
  if (card._editing === "new") box.appendChild(acEditor(card, null));
  if (!list.length && card._editing !== "new") box.appendChild(acEl('<div class="ac-empty">还没有闹钟。点下面加一个，或者跟小新说「明天早上 7 点叫我」。</div>'));
  list.forEach((a, i) => box.appendChild(card._editing === a.id ? acEditor(card, a) : acRow(card, a, i + 1)));
  card.querySelector(".ac-add").hidden = card._editing === "new";
}

function updateRowsOnly(card, list) {
  for (const row of card.querySelectorAll(".ac-row")) {
    const a = list.find((x) => x.id === row.dataset.id);
    if (a) row.querySelector(".ac-next").textContent = acNextText(a);
  }
}

function acRow(card, a, no) {
  const h = +a.time.slice(0, 2);
  const legal = a.repeat === "workday" || a.repeat === "offday";
  const row = acEl(`<div class="ac-row${a.enabled || a.next ? "" : " off"}" role="button" tabindex="0" data-id="${a.id}">
    <span class="ac-no">${no}</span>
    <span class="ac-main"><span class="ac-time">${a.time}</span><span class="ac-per">${acPer(h)}</span>
      ${a.label ? `<span class="ac-name">${esc(a.label)}</span>` : ""}<span class="ac-rep${legal ? " legal" : ""}">${esc(a.repeat_label)}</span></span>
    <input type="checkbox" class="ac-sw" aria-label="开关第 ${no} 个闹钟" ${a.enabled ? "checked" : ""}>
    <span class="ac-sub"><span class="ac-say">🔊 ${esc(a.say_text)}</span><span class="ac-next">${acNextText(a)}</span></span>
  </div>`);
  const sw = row.querySelector(".ac-sw");
  sw.addEventListener("click", (e) => e.stopPropagation());
  sw.addEventListener("change", () => clockApi(`/alarms/${a.id}`, "PATCH", { enabled: sw.checked }));
  const open = () => { card._editing = a.id; renderAlarmCard(card); };
  row.addEventListener("click", open);
  row.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  return row;
}

function acEditor(card, a) {
  const now = new Date(nowS() * 1000);
  const st = {
    h: a ? +a.time.slice(0, 2) : (now.getHours() >= 20 || now.getHours() < 6 ? 7 : now.getHours() + 1), m: a ? +a.time.slice(3) : 0,
    kind: a ? acRepKind(a.repeat) : "once", days: new Set(Array.isArray(a?.repeat) ? a.repeat : []),
  };
  const box = acEl(`<div class="ac-edit">
    <div class="ac-tp">
      <div class="ac-wheel"><button type="button" data-d="h+1" aria-label="加一小时">︿</button><input class="ac-h" inputmode="numeric" maxlength="2" aria-label="小时"><button type="button" data-d="h-1" aria-label="减一小时">﹀</button></div>
      <span class="ac-colon">:</span>
      <div class="ac-wheel"><button type="button" data-d="m+5" aria-label="加五分钟">︿</button><input class="ac-m" inputmode="numeric" maxlength="2" aria-label="分钟"><button type="button" data-d="m-5" aria-label="减五分钟">﹀</button></div>
      <div class="ac-both"><span>12 小时制 <b class="t12"></b></span><span>24 小时制 <b class="t24"></b></span></div>
    </div>
    <div><h4>什么时候响</h4></div>
    <div class="ac-reps" role="group" aria-label="重复">${AC_REPS.map((r) => `<button type="button" data-k="${r.id}" aria-pressed="false">${r.name}${r.sub ? `<small>${r.sub}</small>` : ""}</button>`).join("")}</div>
    <div class="ac-days" role="group" aria-label="星期" hidden>${AC_WEEK.map((d, i) => `<button type="button" data-day="${i + 1}" aria-pressed="false">${d}</button>`).join("")}</div>
    <div class="ac-legal-note" hidden></div>
    <div><h4>响的时候小新说</h4></div>
    <div class="ac-say-row"><input class="ac-say-in" maxlength="40" aria-label="响铃时小新说的话"><button type="button" class="ac-try" aria-label="试听">▶</button></div>
    <input class="ac-label" maxlength="10" placeholder="名字（可不填），比如 起床、练琴" aria-label="闹钟名字">
    <p class="ac-err" aria-live="polite"></p>
    <div class="ac-acts">${a ? '<button type="button" class="del">删除</button>' : ""}<button type="button" class="cancel">取消</button><button type="button" class="save">${a ? "保存" : "添加"}</button></div>
  </div>`);
  const q = (s) => box.querySelector(s);
  const sayIn = q(".ac-say-in");
  sayIn.value = a?.say || "";
  sayIn.placeholder = `不填就说「${clk.snap?.ring_text || ""}」`;
  q(".ac-label").value = a?.label || "";
  const sync = () => {
    q(".ac-h").value = acPad(st.h); q(".ac-m").value = acPad(st.m);
    const h12 = st.h % 12 || 12;
    q(".t12").textContent = `${acPer(st.h)} ${st.h === 0 ? 0 : h12}:${acPad(st.m)}`;
    q(".t24").textContent = `${st.h}:${acPad(st.m)}`;
    for (const b of box.querySelectorAll("[data-k]")) b.setAttribute("aria-pressed", String(b.dataset.k === st.kind));
    q(".ac-days").hidden = st.kind !== "custom";
    for (const b of box.querySelectorAll("[data-day]")) b.setAttribute("aria-pressed", String(st.days.has(+b.dataset.day)));
    const note = { workday: "按国家法定安排：节假日不响，调休上班的周末照响。", offday: "按国家法定安排：周末和节假日响，调休上班的周末不响。" }[st.kind];
    q(".ac-legal-note").hidden = !note;
    q(".ac-legal-note").textContent = note || "";
  };
  for (const b of box.querySelectorAll("[data-d]")) b.addEventListener("click", () => {
    const [k, n] = [b.dataset.d[0], +b.dataset.d.slice(1)];
    if (k === "h") st.h = (st.h + n + 24) % 24; else st.m = (Math.round(st.m / 5) * 5 + n + 60) % 60;
    sync();
  });
  for (const [sel, max, key] of [[".ac-h", 23, "h"], [".ac-m", 59, "m"]]) q(sel).addEventListener("change", () => {
    const v = parseInt(q(sel).value, 10);
    if (v >= 0 && v <= max) st[key] = v;
    sync();
  });
  for (const b of box.querySelectorAll("[data-k]")) b.addEventListener("click", () => {
    st.kind = b.dataset.k;
    if (st.kind === "custom" && !st.days.size) st.days = new Set([1, 2, 3, 4, 5]);
    sync();
  });
  for (const b of box.querySelectorAll("[data-day]")) b.addEventListener("click", () => {
    const d = +b.dataset.day;
    st.days.has(d) ? st.days.delete(d) : st.days.add(d);
    sync();
  });
  q(".ac-try").addEventListener("click", () => sayPreview(sayIn.value.trim() || clk.snap?.ring_text || ""));
  const close = () => { card._editing = null; renderAlarmCard(card); };
  q(".cancel").addEventListener("click", close);
  const del = q(".del");
  del?.addEventListener("click", async () => {
    if (!del.classList.contains("sure")) { del.classList.add("sure"); del.textContent = "确定删除"; return; }  // 点两下才删，小朋友误碰不会删
    if (await clockApi(`/alarms/${a.id}`, "DELETE")) close();
  });
  q(".save").addEventListener("click", async () => {
    if (st.kind === "custom" && !st.days.size) { q(".ac-err").textContent = "自选星期至少选一天"; return; }
    const repeat = st.kind === "custom" ? [...st.days].sort() : st.kind === "once" ? [] : st.kind;
    const body = { time: `${acPad(st.h)}:${acPad(st.m)}`, repeat, say: sayIn.value.trim(), label: q(".ac-label").value.trim() };
    if (a) body.enabled = true;
    const ok = await clockApi(a ? `/alarms/${a.id}` : "/alarms", a ? "PATCH" : "POST", body);
    if (ok) close();
  });
  sync();
  return box;
}

// clock.js 每次拿到新状态都调这个：页面上所有清单卡跟着刷新
function refreshAlarmCards() { for (const c of [...acCards]) renderAlarmCard(c); }
