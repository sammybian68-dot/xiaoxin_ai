// 报时卡：小新调了 query_time，回答上方插一个会走的钟。给正在学 12 / 24 小时制的小朋友：
//   - 表盘：时针、分针、秒针一直在走；外圈小字 13–24 对着 1–12（作业本上的那种双圈钟面）
//   - 右边大字：「下午 1:30」和「13:30」上下对照，中间一个「=」；点一下切换哪个在上
//   - 底下一条 24 小时时间轴：0 点到 24 点，白天亮、夜里暗，红点是现在
// 时间用服务端给的（工具结果里的「现在 2026-10-30 13:30:07」）加上本机走过的秒数，和服务端对齐。
"use strict";

const TC_PERIODS = [[0, 5, "凌晨"], [5, 9, "早上"], [9, 12, "上午"], [12, 13, "中午"], [13, 18, "下午"], [18, 24, "晚上"]];
const tcPeriod = (h) => TC_PERIODS.find(([a, b]) => h >= a && h < b)[2];
const tcPad = (n) => String(n).padStart(2, "0");
const tcEl = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };

function timeFace() {
  // 表盘 SVG：刻度、内圈 1–12、外圈 13–24（12 对着 24，写成 0/24 容易混，就写 24）
  let ticks = "", inner = "", outer = "";
  for (let i = 0; i < 60; i++) {
    const big = i % 5 === 0, a = (i * 6 - 90) * Math.PI / 180, r1 = big ? 78 : 82, r2 = 86;
    ticks += `<line x1="${100 + r1 * Math.cos(a)}" y1="${100 + r1 * Math.sin(a)}" x2="${100 + r2 * Math.cos(a)}" y2="${100 + r2 * Math.sin(a)}" class="${big ? "big" : ""}"/>`;
  }
  for (let h = 1; h <= 12; h++) {
    const a = (h * 30 - 90) * Math.PI / 180;
    inner += `<text x="${100 + 64 * Math.cos(a)}" y="${100 + 64 * Math.sin(a)}">${h}</text>`;
    outer += `<text x="${100 + 96 * Math.cos(a)}" y="${100 + 96 * Math.sin(a)}">${h + 12}</text>`;
  }
  return `<svg class="tc-face" viewBox="-6 -6 212 212" aria-hidden="true">
    <circle cx="100" cy="100" r="88" class="dial"/>
    <g class="ticks">${ticks}</g><g class="inner">${inner}</g><g class="outer">${outer}</g>
    <line class="hand h" x1="100" y1="100" x2="100" y2="56"/>
    <line class="hand m" x1="100" y1="100" x2="100" y2="36"/>
    <line class="hand s" x1="100" y1="112" x2="100" y2="30"/>
    <circle cx="100" cy="100" r="5" class="hub"/>
  </svg>`;
}

function timeCard(serverNow) {
  const card = tcEl(`<section class="tc" aria-label="现在几点">
    <div class="tc-clock">${timeFace()}</div>
    <div class="tc-side">
      <div class="tc-date"></div>
      <button type="button" class="tc-pair" title="点一下换个顺序">
        <span class="tc-a"><small class="tc-a-lab"></small><b class="tc-a-val"></b></span>
        <span class="tc-eq">=</span>
        <span class="tc-b"><small class="tc-b-lab"></small><b class="tc-b-val"></b></span>
      </button>
      <div class="tc-hint"></div>
    </div>
    <div class="tc-day" aria-hidden="true"><div class="tc-day-bar"><span class="tc-day-now"></span></div>
      <div class="tc-day-lab"><span>0</span><span>6</span><span>12</span><span>18</span><span>24</span></div></div>
  </section>`);
  const offset = serverNow ? serverNow.getTime() - Date.now() : 0;
  let twelveFirst = true;
  const q = (s) => card.querySelector(s);

  function tick() {
    if (!card.isConnected && card.dataset.started) return;  // 卡被删了就停
    card.dataset.started = "1";
    const t = new Date(Date.now() + offset);
    const h = t.getHours(), m = t.getMinutes(), s = t.getSeconds();
    q(".hand.h").setAttribute("transform", `rotate(${(h % 12) * 30 + m * 0.5} 100 100)`);
    q(".hand.m").setAttribute("transform", `rotate(${m * 6 + s * 0.1} 100 100)`);
    q(".hand.s").setAttribute("transform", `rotate(${s * 6} 100 100)`);
    const h12 = h % 12 || 12, per = tcPeriod(h);
    const twelve = [`12 小时制`, `${per} ${h12}:${tcPad(m)}`], twenty4 = [`24 小时制`, `${h}:${tcPad(m)}`];
    const [a, b] = twelveFirst ? [twelve, twenty4] : [twenty4, twelve];
    q(".tc-a-lab").textContent = a[0]; q(".tc-a-val").textContent = a[1];
    q(".tc-b-lab").textContent = b[0]; q(".tc-b-val").textContent = b[1];
    q(".tc-a-val").className = `tc-a-val ${twelveFirst ? "is12" : "is24"}`;  // 12 小时红、24 小时蓝，和表盘内外圈一样
    q(".tc-b-val").className = `tc-b-val ${twelveFirst ? "is24" : "is12"}`;
    q(".tc-date").textContent = `${t.getMonth() + 1} 月 ${t.getDate()} 日 星期${"日一二三四五六"[t.getDay()]}`;
    q(".tc-hint").textContent = h >= 13 ? `${h} − 12 = ${h12}，所以是${per} ${h12} 点` : h === 12 ? "中午 12 点就是 12:00" :
      h === 0 ? "半夜 12 点在 24 小时制里写 0:00" : `中午 12 点以前，两种写法的钟点一样，都是 ${h} 点`;
    q(".tc-day-now").style.left = `${((h * 60 + m) / 1440) * 100}%`;
    setTimeout(tick, 1000 - (Date.now() % 1000) + 5);
  }
  q(".tc-pair").addEventListener("click", () => { twelveFirst = !twelveFirst; tick(); });
  requestAnimationFrame(tick);
  return card;
}

// 工具结果「现在 2026-10-30 13:30:07 星期五（北京时间）」→ Date（按北京时间）
function timeCardFor(action) {
  const m = /现在 (\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/.exec(action.result || "");
  const at = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5], +m[6])) : null;
  // 工具调用的那一刻是这个时间；之后本机的时钟往前走。pad 和服务端都在北京时间，不换算时区
  return timeCard(at && Math.abs(at - Date.now()) < 6 * 3600e3 ? at : null);
}
