"use strict";
// 下围棋（docs/board-games.md）：聊天里的「开一盘」卡片 → 全屏对局 → 结束问要不要复盘；复盘、看板也在这个文件。
// 棋盘用 canvas 画（木纹、立体棋子、最后一手标记、预落子虚影、提示点、复盘里 AI 建议点和变化序号）。
// 服务端：/api/go/*（server/games/api.py）。孩子落子立刻生效，小新的应手用长轮询 ?wait=手数 等。

const GO_COLS = "ABCDEFGHJKLMNOPQRST";
const go = { g: null, pending: null, hints: [], cfg: null, gen: 0, openAt: 0 };

function goKidQ() { return state.conv?.kid ? `kid=${encodeURIComponent(state.conv.kid)}` : ""; }
async function goApi(path, body) {
  const q = goKidQ();
  const url = path + (body || !q ? "" : (path.includes("?") ? "&" : "?") + q);
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, kid: state.conv?.kid }) } : {};
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `出错了（${r.status}）`);
  return d;
}
const goXY = (s) => [GO_COLS.indexOf(s[0]), 19 - parseInt(s.slice(1), 10)];
const goName = (x, y) => GO_COLS[x] + (19 - y);

// ---------------------------------------------------------------- 画棋盘
// opts: {board: ["...", ...], last: "Q16", ghost: {x,y,c}, hints: ["Q16"], marks: [{p,"text"|"ring",color}], kid}
function goDraw(canvas, opts) {
  const dpr = window.devicePixelRatio || 1;
  const size = canvas.clientWidth;
  if (canvas.width !== Math.round(size * dpr)) { canvas.width = Math.round(size * dpr); canvas.height = Math.round(size * dpr); }
  const c = canvas.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pad = size * 0.055, step = (size - 2 * pad) / 18;
  canvas._geo = { pad, step, size };
  // 木纹底：两层渐变 + 细纹
  const g = c.createLinearGradient(0, 0, size, size);
  g.addColorStop(0, "#e7c27d"); g.addColorStop(0.5, "#dcae62"); g.addColorStop(1, "#d3a257");
  c.fillStyle = g; c.fillRect(0, 0, size, size);
  c.globalAlpha = 0.08; c.strokeStyle = "#7a4e1c";
  for (let i = 0; i < 40; i++) {
    const y = (i / 40) * size + Math.sin(i * 1.7) * 4;
    c.lineWidth = 1 + (i % 3);
    c.beginPath(); c.moveTo(0, y);
    c.bezierCurveTo(size * 0.3, y + 6 * Math.sin(i), size * 0.7, y - 6 * Math.cos(i), size, y + 3);
    c.stroke();
  }
  c.globalAlpha = 1;
  // 线、星位、坐标
  c.strokeStyle = "#3b2a14"; c.lineWidth = Math.max(1, size / 900);
  for (let i = 0; i < 19; i++) {
    const p = pad + i * step;
    c.beginPath(); c.moveTo(pad, p); c.lineTo(size - pad, p); c.stroke();
    c.beginPath(); c.moveTo(p, pad); c.lineTo(p, size - pad); c.stroke();
  }
  c.lineWidth = Math.max(1.5, size / 450);
  c.strokeRect(pad, pad, size - 2 * pad, size - 2 * pad);
  c.fillStyle = "#3b2a14";
  for (const x of [3, 9, 15]) for (const y of [3, 9, 15]) { c.beginPath(); c.arc(pad + x * step, pad + y * step, step * 0.12, 0, 7); c.fill(); }
  c.font = `${Math.round(step * 0.38)}px -apple-system, sans-serif`; c.textAlign = "center"; c.textBaseline = "middle";
  c.fillStyle = "rgba(59,42,20,.75)";
  for (let i = 0; i < 19; i++) {
    c.fillText(GO_COLS[i], pad + i * step, pad * 0.45); c.fillText(GO_COLS[i], pad + i * step, size - pad * 0.45);
    c.fillText(String(19 - i), pad * 0.45, pad + i * step); c.fillText(String(19 - i), size - pad * 0.45, pad + i * step);
  }
  const stone = (x, y, col, alpha = 1) => {
    const cx = pad + x * step, cy = pad + y * step, r = step * 0.48;
    c.globalAlpha = alpha;
    c.fillStyle = "rgba(0,0,0,.28)"; c.beginPath(); c.arc(cx + r * 0.12, cy + r * 0.16, r, 0, 7); c.fill();
    const gr = c.createRadialGradient(cx - r * 0.35, cy - r * 0.4, r * 0.1, cx, cy, r);
    if (col === "B") { gr.addColorStop(0, "#6b6b6b"); gr.addColorStop(0.35, "#202020"); gr.addColorStop(1, "#000"); }
    else { gr.addColorStop(0, "#ffffff"); gr.addColorStop(0.6, "#ecebe6"); gr.addColorStop(1, "#b8b3a8"); }
    c.fillStyle = gr; c.beginPath(); c.arc(cx, cy, r, 0, 7); c.fill();
    c.globalAlpha = 1;
  };
  const board = opts.board || [];
  for (let y = 0; y < 19; y++) for (let x = 0; x < 19; x++) {
    const v = board[y]?.[x];
    if (v === "B" || v === "W") stone(x, y, v);
  }
  if (opts.ghost) stone(opts.ghost.x, opts.ghost.y, opts.ghost.c, 0.55);
  if (opts.last && opts.last !== "pass") {   // 最后一手：棋子上一个小圈
    const [x, y] = goXY(opts.last);
    const v = board[y]?.[x];
    c.strokeStyle = v === "B" ? "#fff" : "#000"; c.lineWidth = Math.max(2, step * 0.08);
    c.beginPath(); c.arc(pad + x * step, pad + y * step, step * 0.22, 0, 7); c.stroke();
  }
  for (const [i, h] of (opts.hints || []).entries()) {   // 提示：和棋子一样大的绿圈 + 白边 + 序号（pad 上 0.36 格太小，看不清）
    const [x, y] = goXY(h);
    const cx = pad + x * step, cy = pad + y * step;
    c.fillStyle = i === 0 ? "rgba(34,170,80,.92)" : "rgba(34,170,80,.6)";
    c.beginPath(); c.arc(cx, cy, step * 0.46, 0, 7); c.fill();
    c.strokeStyle = "#fff"; c.lineWidth = Math.max(2, step * 0.07);
    c.beginPath(); c.arc(cx, cy, step * 0.46, 0, 7); c.stroke();
    c.fillStyle = "#fff"; c.font = `bold ${Math.round(step * 0.55)}px sans-serif`; c.fillText(String(i + 1), cx, cy + 1);
  }
  for (const m of opts.marks || []) {   // 复盘：AI 建议点（绿圈）、你下的点（红圈）、变化序号
    const [x, y] = goXY(m.p);
    const cx = pad + x * step, cy = pad + y * step;
    if (m.ring) {
      c.strokeStyle = m.ring; c.lineWidth = Math.max(3, step * 0.12);
      c.beginPath(); c.arc(cx, cy, step * 0.42, 0, 7); c.stroke();
    }
    if (m.text) {
      const v = board[y]?.[x];
      c.fillStyle = v === "B" ? "#fff" : v === "W" ? "#111" : (m.color || "#111");
      c.font = `bold ${Math.round(step * 0.46)}px sans-serif`; c.fillText(m.text, cx, cy + 1);
    }
  }
}

// 手指 / 鼠标位置 → 最近的交叉点（离线太远就算没点中）
function goPointAt(canvas, ev) {
  const r = canvas.getBoundingClientRect(), g = canvas._geo;
  if (!g) return null;
  const x = Math.round((ev.clientX - r.left - g.pad) / g.step), y = Math.round((ev.clientY - r.top - g.pad) / g.step);
  if (x < 0 || x > 18 || y < 0 || y > 18) return null;
  return { x, y };
}

// 棋盘尺寸：横屏占满高度（留边），竖屏占满宽度
function goFit(canvas, wrap) {
  // 横屏：棋盘边长 = 视口高度（减边距），右边留至少 300 给信息栏；竖屏：= 视口宽度，下面留 260 给按钮
  const vw = window.innerWidth, vh = window.innerHeight, land = vw > vh && vw > 900;
  const s = Math.max(240, Math.floor(land ? Math.min(vh - 28, vw - 330) : Math.min(vw - 28, vh - 280)));
  canvas.style.width = canvas.style.height = s + "px";
  wrap.style.width = land ? s + 28 + "px" : "";
}
// ---------------------------------------------------------------- 落子声
// 合成：一段很短的噪声（石头碰木头的「嗒」）+ 一个衰减很快的低频（棋盘的共鸣），提子时再加几下轻的「咔」。
// 不用音频文件：离线也能响、没有加载延迟。WebView 要先有一次触摸才让出声：开棋盘时 unlock 一下。
const goSnd = { ctx: null };
function goAudioUnlock() {
  try {
    goSnd.ctx = goSnd.ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (goSnd.ctx.state === "suspended") goSnd.ctx.resume();
  } catch (_) { /* 没有 WebAudio 就不响 */ }
}
function goClack(capture = 0, when = 0) {
  if (go.cfg && go.cfg.sound === false) return;
  const ac = goSnd.ctx;
  if (!ac) return;
  const t = ac.currentTime + when;
  const hit = (at, gain, freq, len) => {
    const n = Math.floor(ac.sampleRate * len), buf = ac.createBuffer(1, n, ac.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / n, 4);
    const src = ac.createBufferSource(); src.buffer = buf;
    const bp = ac.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = freq; bp.Q.value = 1.4;
    const g = ac.createGain(); g.gain.setValueAtTime(gain, at); g.gain.exponentialRampToValueAtTime(0.001, at + len);
    src.connect(bp).connect(g).connect(ac.destination); src.start(at);
  };
  hit(t, 0.9, 2600, 0.05);                           // 「嗒」
  const o = ac.createOscillator(), og = ac.createGain();   // 棋盘「咚」
  o.type = "sine"; o.frequency.setValueAtTime(190, t); o.frequency.exponentialRampToValueAtTime(110, t + 0.12);
  og.gain.setValueAtTime(0.35, t); og.gain.exponentialRampToValueAtTime(0.001, t + 0.16);
  o.connect(og).connect(ac.destination); o.start(t); o.stop(t + 0.18);
  for (let i = 0; i < Math.min(capture, 6); i++) hit(t + 0.12 + i * 0.06, 0.35, 4200, 0.03);  // 提子：几下「咔」
}
// 盘面变了：新落下的子响一声；提了子再多响几下
function goSoundDiff(prev, next) {
  if (!prev || !next || next.moves.length <= prev.moves.length) return;
  const added = next.moves.slice(prev.moves.length).filter((m) => m[1] !== "pass");
  const cap = (g) => (g.captures?.B || 0) + (g.captures?.W || 0);
  const took = Math.max(0, cap(next) - cap(prev));
  added.forEach((_, i) => goClack(i === added.length - 1 ? took : 0, i * 0.35));
}

// ---------------------------------------------------------------- 全屏对局
function goIsKid() { return state.me?.role === "kid" || (state.me?.kind === "admin" && !!window.native); }

async function goOpen(g) {
  go.g = g; go.pending = null; go.hints = []; go.openAt = Date.now(); go.gen++;
  if (typeof pracHushXiaoxin === "function") pracHushXiaoxin();   // 和练习一样：小新先闭嘴
  try { go.cfg = (await goApi("api/games")).go; } catch (_) { go.cfg = go.cfg || { undo: false, hint: false, winbar: false, warn: false, sound: true }; }
  goAudioUnlock();
  let el = $("go");
  if (!el) {
    el = document.createElement("div");
    el.id = "go"; el.className = "gogame"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "下围棋");
    el.innerHTML = `<div class="go-board-wrap"><canvas class="go-board" aria-label="19 路棋盘，点两下落子"></canvas></div>
      <div class="go-side">
        <button type="button" class="go-close" data-go="close">收起棋盘</button>
        <div class="go-players"></div>
        <div class="go-winbar"><div class="bar"><i></i></div><div class="lab"><span></span><span></span></div></div>
        <div class="go-status" aria-live="polite"></div>
        <div class="go-result" hidden></div>
        <div class="go-btns">
          <button type="button" data-go="undo">↶ 悔棋</button>
          <button type="button" data-go="hint">💡 提示</button>
          <button type="button" data-go="pass">停一手</button>
          <button type="button" data-go="count">数子结束</button>
          <button type="button" class="danger wide" data-go="resign">认输</button>
        </div>
      </div>`;
    document.body.appendChild(el);
    const cv = el.querySelector("canvas");
    cv.addEventListener("pointerdown", goTap);
    el.addEventListener("click", goButton);
    window.addEventListener("resize", () => { if (!el.hidden) goRender(); });
  }
  el.hidden = false;
  goRender();
  if (g.thinking) goWait();
}

function goClose() {
  go.gen++;
  const el = $("go");
  if (el) el.hidden = true;
}

function goRender() {
  const el = $("go"), g = go.g;
  if (!el || !g) return;
  const cv = el.querySelector("canvas");
  goFit(cv, el.querySelector(".go-board-wrap"));
  const last = g.moves.length ? g.moves[g.moves.length - 1][1] : null;
  goDraw(cv, { board: g.board, last, ghost: go.pending ? { ...go.pending, c: g.kid_color } : null, hints: go.hints });
  const me = g.kid_color, xin = me === "B" ? "W" : "B";
  const turn = g.result ? "" : g.to_move;
  const cap = (c) => g.captures?.[c] || 0;
  el.querySelector(".go-players").innerHTML = [
    [xin, `小新 · ${esc(g.level_name)}`, g.handicap ? `让 ${g.handicap} 子` : (xin === "W" ? `贴 ${g.komi} 目` : "")],
    [me, "你", ""],
  ].map(([c, name, sub]) => `<div class="go-player${turn === c ? " turn" : ""}"><span class="go-stone-ico ${c}"></span>`
    + `<div><b>${name}</b><small>${c === "B" ? "执黑" : "执白"}${sub ? " · " + sub : ""}</small></div><span class="cap">提 ${cap(c)}</span></div>`).join("");
  const wb = el.querySelector(".go-winbar");
  wb.hidden = !go.cfg?.winbar;   // 辅助：默认关
  const black = me === "B" ? g.winrate : 1 - g.winrate;
  wb.querySelector("i").style.width = `${Math.round(black * 100)}%`;
  const lead = g.score >= 0 ? `你领先 ${g.score.toFixed(1)} 目` : `你落后 ${(-g.score).toFixed(1)} 目`;
  wb.querySelector(".lab").innerHTML = `<span>你的胜率 ${Math.round(g.winrate * 100)}%</span><span>${lead}</span>`;
  const st = el.querySelector(".go-status");
  st.className = "go-status";
  if (g.result) st.textContent = "";
  else if (g.error) { st.textContent = g.error + "（点「再试一次」）"; st.classList.add("err"); }
  else if (g.thinking) { st.textContent = "小新在想"; st.classList.add("think"); }
  else if (go.pending) st.textContent = `再点一下 ${goName(go.pending.x, go.pending.y)} 就落子，点别处换位置`;
  else if (go.cfg?.warn && g.last_loss?.grade === "big") { st.textContent = `刚才那手亏了不少（胜率 ${Math.round(g.last_loss.before * 100)}% → ${Math.round(g.last_loss.after * 100)}%），要不要悔一步再想想？`; st.classList.add("warn"); }
  else st.textContent = `第 ${g.moves.length + 1} 手，轮到你`;
  const busy = !!g.thinking || !!g.result;
  const btn = (k) => el.querySelector(`[data-go="${k}"]`);
  btn("undo").hidden = !go.cfg?.undo;   // 辅助：默认关，设置里开
  btn("hint").hidden = !go.cfg?.hint;
  btn("undo").disabled = busy || !g.moves.some((m) => m[0] === me);
  btn("hint").disabled = busy;
  btn("pass").disabled = busy;
  btn("count").disabled = busy || g.moves.length < 40;
  btn("resign").disabled = busy;
  btn("resign").textContent = g.error ? "再试一次" : "认输";
  btn("resign").dataset.go = g.error ? "retry" : "resign";
  btn("resign").classList.toggle("danger", !g.error);
  const res = el.querySelector(".go-result");
  res.hidden = !g.result;
  if (g.result) {
    const won = g.result.winner === me;
    res.innerHTML = `<b>${won ? "你赢了 🎉" : "小新赢了"}</b><div>${esc(g.result.text)}</div>`
      + `<div class="go-btns" style="margin:0"><button type="button" class="primary" data-go="review">复盘这盘</button>`
      + `<button type="button" data-go="again">再来一盘</button></div>`;
  }
}

function goTap(ev) {
  goAudioUnlock();
  const g = go.g;
  if (!g || g.thinking || g.result || g.to_move !== g.kid_color) return;
  if (Date.now() - go.openAt < 600) return;   // 刚弹出来那一下不认（练习模式里踩过：抬手误点）
  const p = goPointAt(ev.currentTarget, ev);
  if (!p || g.board[p.y][p.x] !== ".") { go.pending = null; goRender(); return; }
  if (go.pending && go.pending.x === p.x && go.pending.y === p.y) { go.pending = null; goMove(goName(p.x, p.y)); return; }
  go.pending = p;
  goRender();
}

async function goMove(mv) {
  const g = go.g;
  go.hints = [];
  try {
    go.g = await goApi(`api/go/${g.id}/move`, { move: mv });
  } catch (e) { toast(e.message); goRender(); return; }
  goSoundDiff(g, go.g);
  goRender();
  goWait();
}

async function goWait() {
  const my = go.gen;
  while (go.g && go.g.thinking && my === go.gen) {
    const before = go.g;
    try {
      go.g = await goApi(`api/go/${go.g.id}?wait=${go.g.moves.length}`);
    } catch (e) { await new Promise((r) => setTimeout(r, 1500)); continue; }
    if (my === go.gen) goSoundDiff(before, go.g);
    if (my === go.gen) goRender();
  }
}

async function goButton(ev) {
  const b = ev.target.closest("[data-go]");
  if (!b || b.disabled) return;
  const g = go.g, k = b.dataset.go;
  try {
    if (k === "close") { goClose(); return; }
    if (k === "undo") { go.g = await goApi(`api/go/${g.id}/undo`, {}); go.hints = []; }
    if (k === "hint") { go.hints = (await goApi(`api/go/${g.id}/hint`, {})).hints.map((h) => h.move); }
    if (k === "pass") { go.pending = null; await goMove("pass"); return; }
    if (k === "retry") { go.g = await goApi(`api/go/${g.id}/retry`, {}); goRender(); goWait(); return; }
    if (k === "count") {
      if (!confirm("现在就数子结束这盘吗？")) return;
      go.g = await goApi(`api/go/${g.id}/count`, {});
    }
    if (k === "resign") {
      if (!confirm("确定认输吗？")) return;
      go.g = await goApi(`api/go/${g.id}/resign`, {});
    }
    if (k === "review") { goClose(); goReviewOpen(g.id); return; }
    if (k === "again") { go.g = await goApi("api/go", { level: g.level, color: g.kid_color, handicap: g.handicap }); go.openAt = Date.now(); goRender(); if (go.g.thinking) goWait(); return; }
  } catch (e) { toast(e.message); }
  goRender();
}
// ---------------------------------------------------------------- 聊天里的「开一盘」卡片
async function goCard(row, args) {
  let s;
  try { s = await goApi("api/games"); } catch (e) { return; }
  let cur = null;
  try { cur = await goApi("api/go/current"); } catch (_) { /* 没有就算了 */ }
  const cfg = s.go, card = document.createElement("div");
  card.className = "go-card";
  const lv = args.level || cfg.level, color = args.color || cfg.color, hc = args.handicap ?? cfg.handicap;
  card.innerHTML = `<h4>⚫⚪ 和小新下围棋</h4>
    <label>小新的棋力<select data-k="level">${s.levels.map((l) => `<option value="${l.id}"${l.id === lv ? " selected" : ""}>${esc(l.name)}</option>`).join("")}</select></label>
    <div class="row2">
      <label>你执（黑先下）<div class="seg" data-k="color"><button type="button" data-v="B"${color === "B" ? ' class="on"' : ""}>黑</button><button type="button" data-v="W"${color === "W" ? ' class="on"' : ""}>白</button></div></label>
      <label>让子<select data-k="handicap">${[0, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => `<option value="${n}"${n === +hc ? " selected" : ""}>${n ? n + " 子" : "不让"}</option>`).join("")}</select></label>
    </div>
    <button type="button" class="go-start">开始</button>
    ${cur?.id && cur.moves.length ? `<button type="button" class="go-resume">接着下没下完的（对 ${esc(cur.level_name)}，第 ${cur.moves.length} 手）</button>` : ""}
    ${s.engine && !s.engine.running ? `<small>小新的棋盘大脑正在启动，开始后第一步可能要等十几秒</small>` : ""}`;
  card.querySelector(".seg").addEventListener("click", (e) => {
    const b = e.target.closest("[data-v]"); if (!b) return;
    card.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
  });
  card.querySelector(".go-start").addEventListener("click", async (e) => {
    const btn = e.currentTarget;   // await 之后 currentTarget 就成 null 了，先存下
    btn.disabled = true;
    const body = { level: card.querySelector('[data-k="level"]').value, color: card.querySelector(".seg .on").dataset.v,
                   handicap: +card.querySelector('[data-k="handicap"]').value };
    try { goOpen(await goApi("api/go", body)); } catch (err) { toast(err.message); }
    btn.disabled = false;
  });
  card.querySelector(".go-resume")?.addEventListener("click", () => goOpen(cur));
  row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
  requestAnimationFrame(() => revealRow(row));
}

// ---------------------------------------------------------------- 看板
async function goStatsCard(row, args) {
  let s;
  try { s = await goApi(`api/go-stats?days=${+args.days || 30}`); } catch (e) { return; }
  const card = document.createElement("div");
  card.className = "gos";
  const rate = s.rate == null ? "-" : Math.round(s.rate * 100) + "%";
  const recent = s.games.slice(0, 10);
  const trend = s.games.filter((g) => g.avg_loss != null).slice(0, 15).reverse();
  card.innerHTML = `<div class="gos-top"><div><b>${s.total}</b><small>下完几盘</small></div>`
    + `<div><b>${s.won} : ${s.lost}</b><small>胜 : 负</small></div><div><b>${rate}</b><small>胜率</small></div></div>`
    + (s.by_level.length ? `<h5>对每个档位</h5><div class="gos-lv">${s.by_level.map((b) => {
      const n = b.won + b.lost, p = n ? b.won / n : 0;
      return `<div class="r"><span>${esc(b.name)}</span><span class="bar"><i style="width:${Math.round(p * 100)}%"></i></span><span>${b.won} 胜 ${b.lost} 负</span></div>`;
    }).join("")}</div>` : "")
    + (trend.length >= 2 ? `<h5>平均每手亏几目（越低越好）</h5><canvas class="gos-trend"></canvas>` : "")
    + `<h5>最近几盘（点一下复盘）</h5><div class="gos-list">${recent.map((g) => {
      const d = new Date(g.created * 1000);
      const when = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      const res = g.won == null ? `<span>没下完</span>` : `<span class="${g.won ? "w" : "l"}">${g.won ? "赢" : "输"}</span>`;
      return `<button type="button" data-gid="${g.id}"><small>${when}</small><span>对 ${esc(g.level_name)} · ${g.color === "B" ? "执黑" : "执白"} · ${g.moves} 手`
        + `${g.big ? ` · 大亏 ${g.big} 手` : ""}<br><small>${esc(g.result)}</small></span>${res}</button>`;
    }).join("")}</div>`;
  card.querySelector(".gos-list").addEventListener("click", (e) => { const b = e.target.closest("[data-gid]"); if (b) goReviewOpen(b.dataset.gid); });
  row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
  const cv = card.querySelector(".gos-trend");
  if (cv) requestAnimationFrame(() => goLine(cv, trend.map((g) => g.avg_loss), { color: "#e9b85c" }));
  requestAnimationFrame(() => revealRow(row));
}

// 复盘那天有好几盘：列出来点
function goPickCard(row, ids) {
  const card = document.createElement("div");
  card.className = "gos gos-pick";
  card.innerHTML = `<h5>点一盘复盘</h5><div class="gos-list"></div>`;
  row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
  Promise.all(ids.map((id) => goApi(`api/go/${id}`).catch(() => null))).then((gs) => {
    card.querySelector(".gos-list").innerHTML = gs.filter(Boolean).map((g) => {
      const d = new Date(g.created * 1000);
      const r = g.result ? `<span class="${g.result.winner === g.kid_color ? "w" : "l"}">${g.result.winner === g.kid_color ? "赢" : "输"}</span>` : "<span>没下完</span>";
      return `<button type="button" data-gid="${g.id}"><small>${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}</small>`
        + `<span>对 ${esc(g.level_name)} · ${g.moves.length} 手<br><small>${esc(g.result?.text || "")}</small></span>${r}</button>`;
    }).join("");
    requestAnimationFrame(() => revealRow(row));
  });
  card.addEventListener("click", (e) => { const b = e.target.closest("[data-gid]"); if (b) goReviewOpen(b.dataset.gid); });
}

// 折线：values 数组，可选 marks（红点下标）、cursor（竖线下标）、mid（画 50% 线）
function goLine(cv, values, o = {}) {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const pts = values.map((v, i) => [values.length > 1 ? 8 + (i / (values.length - 1)) * (w - 16) : w / 2, v]).filter((p) => p[1] != null);
  if (!pts.length) return;
  const lo = o.lo ?? Math.min(...pts.map((p) => p[1])), hi = o.hi ?? Math.max(...pts.map((p) => p[1]));
  const Y = (v) => h - 10 - ((v - lo) / ((hi - lo) || 1)) * (h - 20);
  cv._x = (i) => values.length > 1 ? 8 + (i / (values.length - 1)) * (w - 16) : w / 2;
  if (o.mid != null) { c.strokeStyle = "rgba(233,184,92,.35)"; c.setLineDash([4, 4]); c.beginPath(); c.moveTo(0, Y(o.mid)); c.lineTo(w, Y(o.mid)); c.stroke(); c.setLineDash([]); }
  if (o.fill) {
    c.fillStyle = o.fill; c.beginPath(); c.moveTo(pts[0][0], h - 10);
    for (const [x, v] of pts) c.lineTo(x, Y(v));
    c.lineTo(pts[pts.length - 1][0], h - 10); c.closePath(); c.fill();
  }
  c.strokeStyle = o.color || "#f3ece0"; c.lineWidth = 2; c.beginPath();
  pts.forEach(([x, v], i) => (i ? c.lineTo(x, Y(v)) : c.moveTo(x, Y(v)))); c.stroke();
  for (const m of o.marks || []) {
    if (values[m.i] == null) continue;
    c.fillStyle = m.color || "#ff5a45"; c.beginPath(); c.arc(cv._x(m.i), Y(values[m.i]), 5, 0, 7); c.fill();
  }
  if (o.cursor != null) { c.strokeStyle = "#fff"; c.lineWidth = 1.5; c.beginPath(); c.moveTo(cv._x(o.cursor), 4); c.lineTo(cv._x(o.cursor), h - 4); c.stroke(); }
}
// ---------------------------------------------------------------- 复盘
// 盘面在前端按棋谱重摆（简单提子规则，和服务端 go_rules 一致；复盘只用来看，不判合法）
function goReplay(stones, moves, upto) {
  const b = Array.from({ length: 19 }, () => Array(19).fill("."));
  for (const s of stones || []) { const [x, y] = goXY(s); b[y][x] = "B"; }
  const libs = (x, y) => {
    const col = b[y][x], seen = new Set([x + "," + y]), todo = [[x, y]], grp = [[x, y]];
    let free = false;
    while (todo.length) {
      const [cx, cy] = todo.pop();
      for (const [nx, ny] of [[cx + 1, cy], [cx - 1, cy], [cx, cy + 1], [cx, cy - 1]]) {
        if (nx < 0 || ny < 0 || nx > 18 || ny > 18 || seen.has(nx + "," + ny)) continue;
        if (b[ny][nx] === ".") free = true;
        else if (b[ny][nx] === col) { seen.add(nx + "," + ny); todo.push([nx, ny]); grp.push([nx, ny]); }
      }
    }
    return { free, grp };
  };
  for (const [c, mv] of moves.slice(0, upto)) {
    if (mv === "pass") continue;
    const [x, y] = goXY(mv);
    b[y][x] = c;
    for (const [nx, ny] of [[x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]]) {
      if (nx < 0 || ny < 0 || nx > 18 || ny > 18 || b[ny][nx] === "." || b[ny][nx] === c) continue;
      const r = libs(nx, ny);
      if (!r.free) for (const [px, py] of r.grp) b[py][px] = ".";
    }
  }
  return b.map((r) => r.join(""));
}

const gor = { r: null, n: 0, pv: 0, key: null, saying: null, sayseq: 0, audio: null };

async function goReviewOpen(gid) {
  goClose();
  let el = $("gor");
  if (!el) {
    el = document.createElement("div");
    el.id = "gor"; el.className = "gor"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "复盘");
    el.innerHTML = `<div class="go-board-wrap"><canvas class="go-board" aria-label="复盘棋盘"></canvas></div>
      <div class="gor-side">
        <div class="gor-head"><h3></h3><button type="button" class="go-close" data-r="close">关掉</button></div>
        <div class="gor-sum"></div>
        <canvas class="gor-chart" aria-label="你的胜率曲线，红点是下坏的手，点一下跳到那一手"></canvas>
        <div class="gor-legend"><span><i style="background:#ff5a45"></i>大亏</span><span><i style="background:#e9b85c"></i>小亏</span><span>竖线 = 现在看的那手</span></div>
        <div class="gor-keys"></div>
        <div class="gor-nav"><button type="button" data-r="first" aria-label="开头">⏮</button><button type="button" data-r="prev" aria-label="上一手">◀</button>
          <button type="button" data-r="next" aria-label="下一手">▶</button><button type="button" data-r="last" aria-label="最后">⏭</button><span></span></div>
        <div class="gor-move"></div>
      </div>`;
    document.body.appendChild(el);
    el.addEventListener("click", gorButton);
    el.querySelector(".gor-chart").addEventListener("pointerdown", (e) => {
      const cv = e.currentTarget, r = cv.getBoundingClientRect(), n = gor.r.curve.length - 1;
      gorGo(Math.max(0, Math.min(n, Math.round(((e.clientX - r.left - 8) / (r.width - 16)) * n))));
    });
    window.addEventListener("resize", () => { if (!el.hidden && gor.r) gorRender(); });
    document.addEventListener("keydown", (e) => {
      if (el.hidden || !gor.r) return;
      if (e.key === "ArrowLeft") gorGo(gor.n - 1);
      if (e.key === "ArrowRight") gorGo(gor.n + 1);
    });
  }
  el.hidden = false;
  el.querySelector(".gor-move").innerHTML = '<div class="gor-text loading">小新在复盘这盘棋，第一次要十几秒…</div>';
  el.querySelector(".gor-head h3").textContent = "复盘";
  try { gor.r = await goApi(`api/go/${gid}/review`); } catch (e) { toast(e.message); el.hidden = true; return; }
  const r = gor.r, d = new Date(r.created * 1000);
  el.querySelector(".gor-head h3").textContent = `${d.getMonth() + 1}月${d.getDate()}日 对 ${r.level_name} · ${r.kid_color === "B" ? "执黑" : "执白"}`;
  el.querySelector(".gor-sum").textContent = (r.result ? r.result.text + "。" : "") + r.summary;
  el.querySelector(".gor-keys").innerHTML = r.keys.map((k) => `<button type="button" class="${k.grade}" data-key="${k.n}">第 ${k.n} 手 ${k.decided ? `-${k.loss_score} 目` : `-${Math.round(k.loss_wr * 100)}%`}</button>`).join("")
    || "<small>这盘没有明显下坏的手 👍</small>";
  gorGo(r.keys.length ? r.keys[0].n : r.moves.length);
  if (gor.key) gorSay();   // 一打开就念第一个关键步（孩子不认字）。浏览器拦了自动播放就等他点「听小新讲」
}

// 念讲解（儿子还不认字）：用小新平时的声音（/api/tts），念完按钮变回来。再点一下停
async function gorSay() {
  const k = gor.key;
  if (!k?.text) return;
  if (gor.saying === gor.n) { gorHush(); gorRender(); return; }
  gorHush();
  if (typeof pracHushXiaoxin === "function") pracHushXiaoxin();
  const n = gor.n, seq = ++gor.sayseq;
  gor.saying = n; gorRender();
  try {
    const r = await api("api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: k.text }) });
    const blob = await r.blob();
    if (seq !== gor.sayseq) return;
    const a = new Audio(URL.createObjectURL(blob));
    gor.audio = a;
    a.onended = a.onerror = () => { if (seq === gor.sayseq) { gor.saying = null; gor.audio = null; gorRender(); } };
    await a.play();
  } catch (e) { if (seq === gor.sayseq) { gor.saying = null; gorRender(); toast(e.message); } }
}
function gorHush() { gor.sayseq = (gor.sayseq || 0) + 1; gor.audio?.pause(); gor.audio = null; gor.saying = null; }

function gorGo(n) {
  const r = gor.r;
  const wasSaying = gor.saying != null;
  gorHush();
  gor.n = Math.max(0, Math.min(r.moves.length, n));
  gor.pv = 0;
  gor.key = r.keys.find((k) => k.n === gor.n) || null;
  gorRender();
  if (wasSaying && gor.key) gorSay();   // 在听的时候跳到下一个关键步：接着念新的
}

function gorRender() {
  const el = $("gor"), r = gor.r, n = gor.n;
  const cv = el.querySelector(".go-board");
  goFit(cv, el.querySelector(".go-board-wrap"));
  const k = gor.key;
  // 看 AI 的下法：从这手之前的局面，按 AI 建议的变化摆 gor.pv 手
  let board, marks = [], last = n ? r.moves[n - 1][1] : null;
  if (k && gor.pv > 0) {
    const pv = r.pv[n - 1] || [], seq = [];
    let c = r.moves[n - 1][0];
    for (const mv of pv.slice(0, gor.pv)) { seq.push([c, mv]); c = c === "B" ? "W" : "B"; }
    board = goReplay(r.stones, [...r.moves.slice(0, n - 1), ...seq], n - 1 + seq.length);
    marks = seq.map(([, mv], i) => ({ p: mv, text: String(i + 1) })).filter((m) => m.p !== "pass");
    last = null;
    marks.push({ p: r.moves[n - 1][1], ring: "rgba(255,90,69,.9)" });
  } else {
    board = goReplay(r.stones, r.moves, n);
    if (k) {
      if (r.best[n - 1] && r.best[n - 1] !== "pass") marks.push({ p: r.best[n - 1], ring: "#3fd27a" });
      if (r.moves[n - 1][1] !== "pass") marks.push({ p: r.moves[n - 1][1], ring: "rgba(255,90,69,.95)" });
    }
  }
  goDraw(cv, { board, last, marks });
  const keyMarks = r.keys.map((x) => ({ i: x.n, color: x.grade === "big" ? "#ff5a45" : "#e9b85c" }));
  goLine(el.querySelector(".gor-chart"), r.curve, { lo: 0, hi: 1, mid: 0.5, marks: keyMarks, cursor: n, color: "#f3ece0", fill: "rgba(243,236,224,.10)" });
  el.querySelector(".gor-nav span").textContent = `${n} / ${r.moves.length} 手`;
  el.querySelectorAll(".gor-keys button").forEach((b) => b.classList.toggle("on", +b.dataset.key === n));
  const box = el.querySelector(".gor-move");
  const pct = (v) => (v == null ? "-" : Math.round(v * 100) + "%");
  const lead = (s) => (s == null ? "-" : s >= 0 ? `领先 ${s.toFixed(1)} 目` : `落后 ${(-s).toFixed(1)} 目`);
  if (!n) { box.innerHTML = `<div class="line">开局。点 ▶ 一手手往下看，或点上面红色的那几手。</div>`; return; }
  const [c, mv] = r.moves[n - 1];
  const mine = c === r.kid_color;
  const before = r.curve[n - 1], after = r.curve[n];
  const head = `<div class="line">第 ${n} 手 · ${mine ? "你" : "小新"}下在 <b>${esc(mv)}</b>${k && r.best[n - 1] && r.best[n - 1] !== mv ? `，AI 建议 <b style="color:#7be3a0">${esc(r.best[n - 1])}</b>` : ""}</div>`
    + `<div class="nums"><span>你的胜率 <b>${pct(before)}</b> → <b class="${k ? "drop" : ""}">${pct(after)}</b></span>`
    + `<span>目差 <b>${lead(r.score[n - 1])}</b> → <b>${lead(r.score[n])}</b></span></div>`;
  if (!k) { box.innerHTML = head; return; }
  const pvLen = (r.pv[n - 1] || []).length;
  box.innerHTML = head
    + `<div class="gor-text">${esc(k.text || "")}</div>`
    + `<div class="gor-pv"><button type="button" class="gor-say" data-r="say">${gor.saying === n ? "⏹ 停" : "🔊 听小新讲"}</button>`
    + `<button type="button" data-r="pv"${gor.pv ? ' class="on"' : ""}>${gor.pv ? `AI 的下法 ${gor.pv} / ${pvLen}（再点一手）` : "看 AI 的下法"}</button>`
    + (gor.pv ? `<button type="button" data-r="pv0">回到实战</button>` : "") + `</div>`
    + (k.source === "template" ? `<div class="src">（讲解没通过核对，换成了固定说法）</div>` : "");
}

function gorButton(ev) {
  const b = ev.target.closest("[data-r],[data-key]");
  if (!b) return;
  const r = gor.r;
  if (b.dataset.key) { gorGo(+b.dataset.key); if (gor.key && gor.saying == null) gorSay(); return; }
  const k = b.dataset.r;
  if (k === "close") { gorHush(); $("gor").hidden = true; return; }
  if (k === "say") { gorSay(); return; }
  if (k === "first") gorGo(0);
  if (k === "prev") gorGo(gor.n - 1);
  if (k === "next") gorGo(gor.n + 1);
  if (k === "last") gorGo(r.moves.length);
  if (k === "pv") { gor.pv = Math.min((r.pv[gor.n - 1] || []).length, gor.pv + 1); gorRender(); }
  if (k === "pv0") { gor.pv = 0; gorRender(); }
}

// ---------------------------------------------------------------- 聊天里调了工具 → 出卡片（app.js 的 showAction 调这个）
function goAction(row, a) {
  if (/^失败/.test(a.result || "")) return false;
  let args = {};
  try { args = JSON.parse(a.arguments || "{}") || {}; } catch (_) { /* 用默认 */ }
  if (a.name === "play_go") { goCard(row, args); return true; }
  if (a.name === "go_stats") { goStatsCard(row, args); return true; }
  if (a.name === "go_review") {
    const ids = [...(a.result || "").matchAll(/\[go:([0-9a-f]{16})\]/g)].map((m) => m[1]);
    if (!ids.length) return false;
    if (ids.length === 1) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "action-chip"; b.textContent = "⚫⚪ 打开复盘";
      b.addEventListener("click", () => goReviewOpen(ids[0]));
      row.querySelector(".bubble").insertBefore(b, row.querySelector(".bubble .text"));
      if (a.live) goReviewOpen(ids[0]);   // 刚说完「复盘一下」：直接打开；翻历史时只出按钮
    } else goPickCard(row, ids);
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- 设置 → 下棋（一级页）；二级是棋种，现在只有围棋能用
async function loadGamesSettings() {
  let d;
  try { d = await goApi("api/games"); } catch (e) { toast(e.message); return; }
  const g = d.go, admin = state.me?.kind === "admin";
  $("games-tabs").innerHTML = d.games.map((x) => `<button type="button" class="hw-chip${x.id === "go" ? " on" : ""}"${x.ready ? "" : " disabled"}>${esc(x.name)}${x.ready ? "" : "（以后）"}</button>`).join("");
  $("go-level").innerHTML = d.levels.map((l) => `<option value="${l.id}"${l.id === g.level ? " selected" : ""}>${esc(l.name)}</option>`).join("");
  $("go-color").value = g.color;
  $("go-handicap").value = String(g.handicap);
  $("go-komi").value = g.komi;
  for (const k of ["undo", "hint", "winbar", "warn", "sound"]) $("go-" + k).checked = !!g[k];
  $("go-clear").hidden = !admin;
  const e = d.engine;
  $("go-engine").textContent = !e.installed ? "这台服务器上没装 KataGo"
    : e.running ? `在线（${e.human_model ? "带人类模型，段级档可用" : "没有人类模型，只有冠军档"}）` : `没在跑${e.error ? "：" + e.error : "，第一盘开始时会自动拉起（约 10 秒）"}`;
  document.querySelectorAll('.pane[data-pane="games"] input, .pane[data-pane="games"] select').forEach((x) => { x.disabled = !admin; });
}

async function saveGamesSettings(patch) {
  try {
    await api("api/games/go", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).then(json);
    toast("改好了");
  } catch (e) { toast(e.message); loadGamesSettings(); }
}

function wireGamesSettings() {
  $("go-level").addEventListener("change", (e) => saveGamesSettings({ level: e.target.value }));
  $("go-color").addEventListener("change", (e) => saveGamesSettings({ color: e.target.value }));
  $("go-handicap").addEventListener("change", (e) => saveGamesSettings({ handicap: +e.target.value }));
  $("go-komi").addEventListener("change", (e) => saveGamesSettings({ komi: +e.target.value }));
  for (const k of ["undo", "hint", "winbar", "warn", "sound"]) $("go-" + k).addEventListener("change", (e) => saveGamesSettings({ [k]: e.target.checked }));
  // 清空所有下棋记录：先确认一次，再输密码（服务端核对，错了不删）
  $("go-clear").addEventListener("click", async () => {
    if (!confirm("要删掉所有下棋记录吗？看板和复盘都会没有，删了不能恢复。")) return;
    const pw = prompt("请输入密码确认删除：");
    if (pw == null) return;
    try {
      const r = await api("api/games/go/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }) }).then(json);
      toast(`删掉了 ${r.deleted} 盘`);
    } catch (e) { toast(e.message); }
  });
}
if ($("go-level")) wireGamesSettings();
