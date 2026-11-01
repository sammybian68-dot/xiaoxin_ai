"use strict";
// 英文书写卡（像汉字笔顺卡）：四线格里演示一个单词每个字母怎么写（一笔一笔，红色是正在写的），然后「我来写」：
// 孩子用手指 / 笔在四线格里一笔一笔描，每笔和标准笔画比，对了变黑、错了变红并提示「从第二线起笔」「方向反了」。
// 字母笔画数据在 letters.js。任何单词都能写（只用到 a–z、A–Z，别的字符跳过）。
//
// 判一笔对不对（比汉字宽松，手写英文差异大）：
//   起点离标准起点 ≤ 0.28 格高、终点 ≤ 0.32 格高；重采样成 16 个点后平均距离 ≤ 0.22 格高；
//   画反了（起终点对调后才对得上）单独提示「方向反了」。点（i、j 的点）只要位置对就算。

function englishCard(word, meaning = "") {
  const letters = [...String(word || "")].filter((c) => window.LETTERS[c]);
  const wrap = document.createElement("div");
  wrap.className = "en-card";
  if (!letters.length) { wrap.textContent = "没有可以写的英文字母"; return wrap; }
  const H = 100, PAD = 6;
  // 每个字母的横向位置
  const xs = [];
  let x = PAD;
  for (const c of letters) { xs.push(x); x += window.LETTER_WIDTH[c]; }
  const W = x + PAD;
  const strokes = [];  // [{li, si, pts（绝对坐标）}]
  letters.forEach((c, li) => window.LETTERS[c].forEach((s, si) => strokes.push({ li, si, ch: c, pts: s.map(([px, py]) => [px + xs[li], py]) })));

  const phrase = /^[A-Za-z][A-Za-z '\-]*$/.test(word.trim()) && letters.length > 1;  // 单个字母不查音标
  wrap.innerHTML = `<div class="en-top"><b class="en-word">${esc(word)}</b>${meaning ? `<span class="en-mean">${esc(meaning)}</span>` : ""}</div>`
    + `<div class="en-ph">${phrase
      ? ["uk", "us"].map((a) => `<button type="button" class="en-acc" data-acc="${a}"><span class="en-tag">${a === "uk" ? "英" : "美"}</span>`
        + `<span class="en-ipa">…</span><span class="en-ico">🔊</span></button>`).join("")
      : '<button type="button" class="en-acc" data-acc="tts"><span class="en-ico">🔊</span> 读一遍</button>'}</div>`
    + `<div class="en-board"><svg class="en-svg" viewBox="${-2} ${-6} ${W + 4} ${H + 12}" preserveAspectRatio="xMidYMid meet"></svg></div>`
    + `<div class="en-now" aria-live="polite">共 ${letters.length} 个字母、${strokes.length} 笔</div>`
    + `<div class="en-btns"><button type="button" class="primary" data-a="play">▶ 看怎么写</button>`
    + `<button type="button" data-a="quiz">✍ 我来写</button></div>`;
  const svg = wrap.querySelector("svg");
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  // 四线格：第三线（基线）红色，其余浅蓝，和练习本一样
  window.LETTER_LINES.forEach((y, i) => svg.appendChild(mk("line", { x1: 0, x2: W, y1: y, y2: y, class: i === 2 ? "en-line base" : "en-line" })));
  const path = (pts) => "M" + pts.map(([a, b]) => `${a} ${b}`).join(" L");
  const ghost = mk("g", { class: "en-ghost" });
  const ink = mk("g", { class: "en-ink" });
  svg.append(ghost, ink);
  for (const s of strokes) ghost.appendChild(mk("path", { d: path(s.pts) }));

  const st = { playing: 0, quiz: false, k: 0, wrong: 0, mistakes: {} };
  const now = (h) => { wrap.querySelector(".en-now").innerHTML = h; };

  // ---------------------------------------------------------------- 演示：一笔一笔画，正在写的红色
  async function play() {
    const gen = ++st.playing;
    endQuiz();
    ink.innerHTML = "";
    for (let i = 0; i < strokes.length; i++) {
      if (gen !== st.playing) return;
      const s = strokes[i];
      now(`<b>${esc(s.ch)}</b> · ${esc(window.letterStrokeHint(s.ch, s.si, window.LETTERS[s.ch][s.si]))}`);
      const p = mk("path", { d: path(s.pts), class: "en-stroke live" });
      ink.appendChild(p);
      const len = p.getTotalLength();
      p.style.strokeDasharray = len;
      p.style.strokeDashoffset = len;
      const dur = Math.max(260, len * 9);
      await new Promise((r) => { p.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }], { duration: dur, fill: "forwards" }).onfinish = r; });
      p.style.strokeDashoffset = 0;
      p.classList.remove("live");
      await new Promise((r) => setTimeout(r, 120));
    }
    if (gen === st.playing) now(`写完了。点「✍ 我来写」自己写一遍`);
  }

  // ---------------------------------------------------------------- 我来写：一笔一笔比
  const toSvg = (e) => {
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    return [p.x, p.y];
  };
  let drawing = null;

  function startQuiz() {
    if (st.quiz) { endQuiz(); return; }
    st.playing++;
    st.quiz = true; st.k = 0; st.wrong = 0; st.mistakes = {};
    ink.innerHTML = "";
    wrap.classList.add("quiz");
    wrap.querySelector('[data-a="quiz"]').textContent = "不写了";
    hint();
  }
  function endQuiz() {
    if (!st.quiz) return;
    st.quiz = false;
    wrap.classList.remove("quiz");
    wrap.querySelector('[data-a="quiz"]').textContent = "✍ 我来写";
  }
  function hint(extra = "") {
    const s = strokes[st.k];
    now(`${extra}写 <b>${esc(s.ch)}</b> 的第 ${s.si + 1} 笔` + (st.mistakes[st.k] >= 2 ? "（看红点，从这里起笔）" : ""));
    svg.querySelector(".en-start")?.remove();
    if (st.mistakes[st.k] >= 2) svg.appendChild(mk("circle", { cx: s.pts[0][0], cy: s.pts[0][1], r: 3.5, class: "en-start" }));
  }

  svg.addEventListener("pointerdown", (e) => {
    if (!st.quiz) return;
    e.preventDefault();
    svg.setPointerCapture(e.pointerId);
    drawing = { pts: [toSvg(e)], el: mk("path", { class: "en-stroke mine" }) };
    ink.appendChild(drawing.el);
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    for (const ev of e.getCoalescedEvents?.() || [e]) drawing.pts.push(toSvg(ev));  // 一帧里合并的点也要，轨迹才完整
    drawing.el.setAttribute("d", path(drawing.pts));
  });
  const up = (e) => {
    if (!drawing) return;
    const d = drawing;
    drawing = null;
    // 抬笔那一下也算一个点：最后一段手指动得快、没来得及发 move，终点会丢（2026-11-01 真机：竖只到一半就判「形状不像」）
    if (e?.clientX != null) { const p = toSvg(e); const last = d.pts[d.pts.length - 1]; if (Math.hypot(p[0] - last[0], p[1] - last[1]) > 0.5) d.pts.push(p); }
    judge(d);
  };
  svg.addEventListener("pointerup", up);
  svg.addEventListener("pointercancel", up);

  function judge(d) {
    const want = strokes[st.k];
    const res = compare(d.pts, want.pts, H);
    if (res.ok) {
      d.el.remove();
      ink.appendChild(mk("path", { d: path(want.pts), class: "en-stroke done" }));
      st.k++;
      if (st.k >= strokes.length) {
        endQuiz();
        const bad = Object.keys(st.mistakes).length;
        now(bad ? `写完了！有 ${bad} 笔写错过，再写一遍会更好` : "全对！真棒 🎉");
        if (typeof hzPlay === "function") hzPlay(["#done"]);
        return;
      }
      hint(want.li !== strokes[st.k].li ? `<b>${esc(want.ch)}</b> 写好了。` : "");
      return;
    }
    st.mistakes[st.k] = (st.mistakes[st.k] || 0) + 1;
    st.wrong++;
    d.el.classList.add("wrong");
    setTimeout(() => d.el.remove(), 600);
    if (typeof hzPlay === "function") hzPlay(["#wrong"]);
    hint(`<span class="en-bad">${esc(res.why)}</span> `);
  }

  wrap.querySelector('[data-a="play"]').addEventListener("click", play);
  wrap.querySelector('[data-a="quiz"]').addEventListener("click", startQuiz);
  // 音标、英音美音：服务端从词典取（有缓存）；取不到就只剩「🔊」，点了用小新的声音读
  const w = encodeURIComponent(word.trim());
  if (phrase) {
    api(`api/dict/${w}`).then(json).then((d) => {
      for (const a of ["uk", "us"]) wrap.querySelector(`[data-acc="${a}"] .en-ipa`).textContent = d[a] ? `/${d[a]}/` : "";
    }).catch(() => wrap.querySelectorAll(".en-ipa").forEach((e) => { e.textContent = ""; }));
  }
  for (const b of wrap.querySelectorAll(".en-acc")) {
    b.addEventListener("click", () => {
      if (b.dataset.acc === "tts") { speak(word, b.querySelector(".en-ico")); return; }
      stopSpeaking();
      const audio = new Audio(`api/dict/${w}/${b.dataset.acc}.mp3`);
      player.audio = audio; player.btn = b.querySelector(".en-ico");
      b.classList.add("on");
      const done = () => { b.classList.remove("on"); if (player.audio === audio) stopSpeaking(); };
      audio.onended = done;
      audio.play().catch(() => { done(); speak(word, b.querySelector(".en-ico")); });  // 词典没这个读音：小新读
    });
  }
  setTimeout(play, 300);
  wrap._strokes = strokes;  // 调试 / 端到端测试用：每笔的绝对坐标（SVG 单位）
  wrap._svg = svg;
  return wrap;
}

// 一笔对不对：返回 {ok, why}。坐标都在四线格单位里（格高 100）
function compare(mine, want, H) {
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const len = (pts) => pts.slice(1).reduce((s, p, i) => s + dist(p, pts[i]), 0);
  const isDot = len(want) < 6;
  if (isDot) return dist(mine[0], want[0]) < 0.18 * H ? { ok: true } : { ok: false, why: "点的位置不对" };
  if (len(mine) < 0.06 * H) return { ok: false, why: "这一笔太短了" };
  const a = resample(mine, 16), b = resample(want, 16);
  const avg = (p, q) => p.reduce((s, x, i) => s + dist(x, q[i]), 0) / p.length;
  const fwd = avg(a, b), rev = avg([...a].reverse(), b);
  // 起终点的容差：长笔 0.28 格高，短笔（A 的横、t 的横）按笔长缩，不然短横反着画起终点都在容差里
  const tol = Math.min(0.28 * H, Math.max(0.12 * H, len(want) * 0.45));
  const s0 = dist(mine[0], want[0]), e0 = dist(mine[mine.length - 1], want[want.length - 1]);
  const closed = dist(want[0], want[want.length - 1]) < 8;  // O、o 闭合：只看形状和起点
  const startOk = s0 <= tol, endOk = closed || e0 <= tol * 1.15;
  // 方向：正着比反着更像，并且起点离标准起点比离标准终点近（v、u 这种左右对称的形状光看形状分不出正反）
  const dirOk = closed || (fwd <= rev && s0 < dist(mine[0], want[want.length - 1]));
  if (fwd <= 0.22 * H && startOk && endOk && dirOk) return { ok: true };
  if (!dirOk && Math.min(fwd, rev) <= 0.22 * H) return { ok: false, why: "方向反了，从另一头起笔。" };
  if (!startOk) return { ok: false, why: "起笔的位置不对。" };
  return { ok: false, why: "形状不太像，再试一次。" };
}

function resample(pts, n) {
  const d = [0];
  for (let i = 1; i < pts.length; i++) d.push(d[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const total = d[d.length - 1] || 1, out = [];
  for (let k = 0; k < n; k++) {
    const t = total * k / (n - 1);
    let i = d.findIndex((x) => x >= t);
    if (i <= 0) { out.push(pts[0]); continue; }
    const f = (t - d[i - 1]) / ((d[i] - d[i - 1]) || 1);
    out.push([pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f]);
  }
  return out;
}

window.englishCard = englishCard;
window.enCompare = compare;
