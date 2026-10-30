// 笔顺卡：小新调了 show_stroke_order，回答上方插一张卡，一笔一划演示。用 Hanzi Writer 3.7.3（MIT，static/vendor）。
// 数据来自 /api/hanzi/<字>（服务端 server/hanzi.py），不走外网。
"use strict";

const HZ_SIZE = 260, HZ_CELL = 76;
const HZ_INK = "#2b2420", HZ_RED = "#e0452b", HZ_GHOST = "#ece5db";
let hzSeq = 0;
const hzEl = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const hzCache = {};

function hzData(ch) {
  if (!hzCache[ch]) {
    hzCache[ch] = api(`api/hanzi/${encodeURIComponent(ch)}`).then(json).catch((e) => { delete hzCache[ch]; throw e; });
  }
  return hzCache[ch];
}

function hzGrid() {
  // 田字格 + 米字格（虚线），和作业本一样
  return `<svg class="grid" viewBox="0 0 100 100" aria-hidden="true"><line x1="0" y1="50" x2="100" y2="50"/><line x1="50" y1="0" x2="50" y2="100"/>`
    + `<line x1="0" y1="0" x2="100" y2="100"/><line x1="100" y1="0" x2="0" y2="100"/></svg>`;
}

function strokeCard(chars) {
  const card = hzEl(`<section class="hz" aria-label="笔顺">
    <div class="hz-tabs" role="tablist"></div>
    <div class="hz-main">
      <div class="hz-board">${hzGrid()}<div class="writer"></div><span class="hz-step"></span></div>
      <div class="hz-info">
        <div class="hz-py"></div>
        <div class="hz-facts"></div>
        <div class="hz-now" aria-live="polite"></div>
        <div class="hz-btns">
          <button type="button" class="go" data-a="play">▶ 再看一遍</button>
          <button type="button" data-a="step">一笔一笔</button>
          <button type="button" data-a="quiz">✍ 我来写</button>
        </div>
        <div class="hz-cheer" aria-live="polite"></div>
      </div>
    </div>
    <div class="hz-strip"></div>
  </section>`);
  const st = { ch: chars[0], data: null, writer: null, step: -1, quiz: false, cells: [], ink: [], len: [], gen: 0 };
  const $c = (s) => card.querySelector(s);

  const tabs = $c(".hz-tabs");
  if (chars.length > 1) {
    for (const ch of chars) {
      const b = hzEl(`<button type="button" role="tab">${esc(ch)}</button>`);
      b.addEventListener("click", () => load(ch));
      tabs.appendChild(b);
    }
  } else tabs.remove();

  function opts(extra = {}) {
    return { width: HZ_SIZE, height: HZ_SIZE, padding: 18, showOutline: true, showCharacter: false,
      strokeColor: HZ_INK, outlineColor: HZ_GHOST, highlightColor: HZ_RED, drawingColor: HZ_INK, drawingWidth: 26,
      charDataLoader: (c, done) => hzData(c).then((d) => done({ strokes: d.strokes, medians: d.medians, radStrokes: d.radStrokes })),
      ...extra };
  }

  function name(i) { return st.data?.names?.[i] || ""; }
  function showNow(i) {
    const n = st.data.count;
    $c(".hz-now").innerHTML = i < 0 ? `共 <b>${n}</b> 画` : `第 <b>${i + 1}</b> 笔${name(i) ? `：<b>${esc(name(i))}</b>` : ""}`;
    $c(".hz-step").textContent = i < 0 ? "" : `${i + 1} / ${n}`;
    st.cells.forEach((c, k) => c.setAttribute("aria-current", String(k === i)));
    st.cells[i]?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
  }

  // 演示：自己画（不用 Hanzi Writer 的动画），这样每一笔的颜色能单独控制——正在写的红色，写过的墨色，没写的淡灰。
  // 每笔 = 轮廓当 clipPath + 沿中线的一条粗线，stroke-dashoffset 从全长走到 0，就是「笔尖顺着笔画走」
  function board() {
    const d = st.data, id = `hz${++hzSeq}`;
    const defs = d.strokes.map((p, k) => `<clipPath id="${id}c${k}"><path d="${p}"/></clipPath>`).join("");
    const ghost = d.strokes.map((p) => `<path d="${p}" fill="${HZ_GHOST}"/>`).join("");
    const ink = d.medians.map((m, k) => `<path data-k="${k}" d="M${m.map((pt) => pt.join(" ")).join(" L")}" fill="none" `
      + `stroke="${HZ_INK}" stroke-width="128" stroke-linecap="round" stroke-linejoin="round" clip-path="url(#${id}c${k})"/>`).join("");
    const holder = $c(".writer");
    holder.innerHTML = `<svg viewBox="0 0 1024 1024" width="100%" height="100%" aria-label="${esc(st.ch)} 的笔顺">`
      + `<defs>${defs}</defs><g transform="translate(64, 960) scale(0.875, -0.875)">${ghost}${ink}</g></svg>`;
    st.ink = [...holder.querySelectorAll("path[data-k]")];
    st.len = st.ink.map((el) => el.getTotalLength() + 128);
    reset();
  }

  function reset() {
    st.ink.forEach((el, k) => { el.style.transition = "none"; el.style.strokeDasharray = st.len[k]; el.style.strokeDashoffset = st.len[k]; el.setAttribute("stroke", HZ_INK); });
  }
  function fill(k, color) {
    const el = st.ink[k];
    el.style.transition = "none";
    el.style.strokeDashoffset = 0;
    el.setAttribute("stroke", color);
  }
  // 第 k 笔走一遍：时长按笔画长短，0.45–1.2 秒
  function draw(k, gen) {
    return new Promise((ok) => {
      const el = st.ink[k];
      const secs = Math.min(1.2, Math.max(0.45, st.len[k] / 1500));
      el.setAttribute("stroke", HZ_RED);
      el.style.transition = "none";
      el.style.strokeDashoffset = st.len[k];
      el.getBoundingClientRect();  // 让浏览器先落下起点，transition 才会生效
      el.style.transition = `stroke-dashoffset ${secs}s linear`;
      el.style.strokeDashoffset = 0;
      setTimeout(() => ok(gen === st.gen), secs * 1000 + 40);
    });
  }

  // 「再看一遍」：从头一笔一笔写，正在写的红色，写完变墨色，笔与笔之间停一下
  function ensureBoard() {
    if (st.needBoard) { st.needBoard = false; st.writer = null; board(); }
  }

  async function play() {
    endQuiz();
    ensureBoard();
    const gen = ++st.gen;
    reset();
    st.step = -1;
    for (let k = 0; k < st.data.count; k++) {
      showNow(k);
      if (!(await draw(k, gen))) return;
      fill(k, HZ_INK);
      await new Promise((ok) => setTimeout(ok, 260));
      if (gen !== st.gen) return;
    }
    showNow(-1);
  }

  // 只亮到第 i 笔：前面的墨色一下子出现，第 i 笔红色写一遍（停在红色，看清是哪一笔）
  async function showUpTo(i) {
    endQuiz();
    ensureBoard();
    const gen = ++st.gen;
    reset();
    for (let k = 0; k < i; k++) fill(k, HZ_INK);
    showNow(i);
    await draw(i, gen);
  }

  // 「一笔一笔」：点一下走一笔，孩子跟着在本子上写
  function nextStep() {
    st.step = st.step >= st.data.count - 1 ? 0 : st.step + 1;
    showUpTo(st.step);
  }

  function startQuiz() {
    if (st.quiz) { endQuiz(); return; }
    if (typeof HanziWriter === "undefined") { $c(".hz-now").textContent = "描红要的组件没加载上，刷新一下再试"; return; }
    st.gen++;
    st.needBoard = false;
    st.quiz = true;
    const holder = $c(".writer");
    holder.innerHTML = "";
    st.writer = HanziWriter.create(holder, st.ch, opts());
    st.wrong = {};  // 第几笔错了几次，写完念出来
    $c(".hz-board").classList.add("quiz");
    $c('[data-a="quiz"]').classList.add("quiz-on");
    $c('[data-a="quiz"]').textContent = "不写了";
    $c(".hz-cheer").textContent = "";
    showNow(0);
    $c(".hz-now").innerHTML = `照着淡灰色描，从第 <b>1</b> 笔开始`;
    st.writer.quiz({
      showHintAfterMisses: 2, markStrokeCorrectAfterMisses: 4, leniency: 1.3,
      onCorrectStroke: (d) => {
        const i = d.strokeNum + 1;
        // 最后一笔不在这念：和写完的成绩合成一句（不然两句互相掐）
        if (i < st.data.count && name(d.strokeNum)) hzPlay([name(d.strokeNum)]);
        if (i < st.data.count) { showNow(i); $c(".hz-now").innerHTML = `写对了！下一笔：<b>${esc(name(i) || `第 ${i + 1} 笔`)}</b>`; }
      },
      onMistake: (d) => { st.wrong[d.strokeNum] = (st.wrong[d.strokeNum] || 0) + 1; hzPlay(["#wrong"]); $c(".hz-now").innerHTML = d.mistakesOnStroke >= 2 ? `看红色提示，第 <b>${d.strokeNum + 1}</b> 笔从这里起笔` : `再试一次，第 <b>${d.strokeNum + 1}</b> 笔是<b>${esc(name(d.strokeNum) || "")}</b>`; },
      onComplete: (d) => {
        st.quiz = false;
        st.needBoard = true;  // 格子里现在是孩子写的字，下次演示前换回演示用的画板
        $c(".hz-board").classList.remove("quiz");
        $c('[data-a="quiz"]').classList.remove("quiz-on");
        $c('[data-a="quiz"]').textContent = "✍ 再写一次";
        showNow(-1);
        const parts = quizResultParts(st.data, st.wrong, d.totalMistakes);
        const last = name(st.data.count - 1);
        hzPlay(last ? [last, ...parts] : parts);
        // 屏幕上写阿拉伯数字（孩子好认），念的是中文整句
        const worst = d.totalMistakes ? +Object.entries(st.wrong).sort((x, y) => y[1] - x[1])[0][0] : -1;
        $c(".hz-cheer").textContent = d.totalMistakes === 0 ? "🎉 全对，真棒！"
          : `写完啦，错了 ${d.totalMistakes} 次。第 ${worst + 1} 笔${name(worst)}要注意，再写一遍吧。`;
      },
    });
  }
  function endQuiz() {
    if (!st.quiz) return;
    st.quiz = false;
    st.writer?.cancelQuiz();
    st.writer = null;
    board();
    for (let k = 0; k < st.data.count; k++) fill(k, HZ_INK);
    $c(".hz-board").classList.remove("quiz");
    $c('[data-a="quiz"]').classList.remove("quiz-on");
    $c('[data-a="quiz"]').textContent = "✍ 我来写";
  }

  // 下面一排小格：第 i 格只画前 i+1 笔，第 i 笔红色（和百度一样）；点哪格就在大格里单看那一笔
  function strip() {
    const box = $c(".hz-strip");
    box.innerHTML = "";
    st.cells = [];
    const d = st.data;
    for (let i = 0; i < d.count; i++) {
      const c = hzEl(`<button type="button" class="hz-cell" aria-label="第 ${i + 1} 笔 ${esc(name(i))}">
        <span class="box">${cellSvg(d, i)}</span><span class="nm">${esc(name(i) || "　")}</span><span class="no">${i + 1}</span></button>`);
      c.addEventListener("click", () => { st.step = i; showUpTo(i); });
      box.appendChild(c);
      st.cells.push(c);
    }
  }

  async function load(ch) {
    st.ch = ch;
    for (const b of tabs.querySelectorAll("button")) b.setAttribute("aria-selected", String(b.textContent === ch));
    $c(".hz-now").textContent = "正在拿笔顺…";
    $c(".hz-cheer").textContent = "";
    try {
      st.data = await hzData(ch);
    } catch (e) {
      $c(".hz-now").textContent = e.message;
      return;
    }
    const d = st.data;
    $c(".hz-py").innerHTML = `${esc(d.pinyin[0] || "")}${d.pinyin.length > 1 ? `<small>也读 ${esc(d.pinyin.slice(1).join("、"))}</small>` : ""}`;
    $c(".hz-facts").innerHTML = [["部首", d.radical], ["结构", d.structure], ["笔画", `${d.count} 画`]]
      .filter(([, v]) => v).map(([k, v]) => `<span>${k}<b>${esc(v)}</b></span>`).join("");
    if (st.quiz) { st.quiz = false; st.writer?.cancelQuiz(); st.writer = null; }
    $c(".hz-board").classList.remove("quiz");
    board();
    st.step = -1;
    strip();
    showNow(-1);
    hzPrefetch(d);  // 描红播报要用的短语先下好，写的时候本机直接放
    play();
  }

  $c('[data-a="play"]').addEventListener("click", play);
  $c('[data-a="step"]').addEventListener("click", nextStep);
  $c('[data-a="quiz"]').addEventListener("click", startQuiz);
  load(chars[0]);
  return card;
}

// 小格子用静态 SVG 画（不开 76 个 Hanzi Writer），坐标系和 Hanzi Writer 一样：1024×1024，y 轴朝上
function cellSvg(d, i) {
  const paths = d.strokes.map((p, k) => k > i ? "" : `<path d="${p}" fill="${k === i ? "#e0452b" : "#2b2420"}"/>`).join("");
  const ghost = d.strokes.map((p) => `<path d="${p}" fill="#efe8de"/>`).join("");
  return `<svg viewBox="0 0 1024 1024" width="${HZ_CELL - 4}" height="${HZ_CELL - 4}" aria-hidden="true">`
    + `<g transform="translate(64, 960) scale(0.875, -0.875)">${ghost}${paths}</g></svg>`;
}

// 从回答里的工具结果建卡：action = {name:"show_stroke_order", arguments:'{"chars":"心"}', result:"已…"}
function strokeCardFor(action) {
  let chars = [];
  try { chars = [...(JSON.parse(action.arguments || "{}").chars || "")].filter((c) => /[㐀-鿿]/.test(c)); } catch (_) { /* 坏参数 */ }
  chars = [...new Set(chars)].slice(0, 4);
  if (!chars.length || /^失败/.test(action.result || "")) return null;
  return strokeCard(chars);
}

// ---------------------------------------------------------------- 描红播报
// 不现场 TTS：用到的每一句都是完整的一句话，提前用当前音色合成好存在本机（pad 原生 Phrases；浏览器存在内存）。
// 拼接只发生在句子之间（句号处本来就停顿），不在词中间拼，听着连贯。
//   写对一笔：这一笔的名字（「卧钩」）
//   写错一笔：「嘟嘟」一声（本机合成）
//   写完：全对「全对，真棒！」；有错「写完啦，错了两次。」+「第二笔卧钩要注意，再写一遍吧。」（只说错得最多的那一笔）
// 每次播都会掐掉之前在放的（包括小新正在说的话）。
const HZ_DIGITS = ["一", "两", "三", "四", "五", "六", "七", "八", "九", "十"];
const HZ_MAX_SAID = 12;  // 错到这么多次以上就说「好多次」

function hzNum(n) { return n <= 10 ? HZ_DIGITS[n - 1] : n < 20 ? "十" + HZ_DIGITS[n - 11] : "二十"; }
function hzNumOrd(n) { return n === 2 ? "二" : hzNum(n); }  // 「第二笔」，不说「第两笔」
const hzTimes = (n) => `写完啦，错了${n > HZ_MAX_SAID ? "好多" : hzNum(n)}次。`;
const hzWatch = (k, name) => `第${hzNumOrd(k + 1)}笔${name || ""}要注意，再写一遍吧。`;

// 这个字描红可能用到的所有句子
function hzPhrases(data) {
  const set = new Set(["全对，真棒！", ...(data.names || []).filter(Boolean)]);
  for (let n = 1; n <= HZ_MAX_SAID + 1; n++) set.add(hzTimes(n));
  for (let k = 0; k < data.count; k++) set.add(hzWatch(k, data.names?.[k]));
  return [...set];
}

function hzPrefetch(data) {
  const list = hzPhrases(data);
  if (native?.phrasePrefetch) { native.phrasePrefetch(JSON.stringify(list)); return; }
  for (const t of list) hzWebPcm(t).catch(() => {});
}

function quizResultParts(data, wrong, total) {
  if (!total) return ["全对，真棒！"];
  const worst = +Object.entries(wrong).sort((a, b) => b[1] - a[1])[0][0];
  return [hzTimes(total), hzWatch(worst, data.names?.[worst])];
}

function hzPlay(parts) {
  if (native?.phrasePlay) { native.phrasePlay(JSON.stringify(parts)); return; }
  hzWebPlay(parts);
}

// ---- 浏览器里：同样按短语缓存（内存），Web Audio 拼着放
const hzWebCache = {};
function hzWebPcm(text) {
  if (!hzWebCache[text]) {
    hzWebCache[text] = api(`api/cue/say?text=${encodeURIComponent(text)}`).then((r) => r.arrayBuffer())
      .catch((e) => { delete hzWebCache[text]; throw e; });
  }
  return hzWebCache[text];
}
let hzWebStream = null;
async function hzWebPlay(parts) {
  hzWebStream?.stop();
  stopSpeaking?.();
  talk?.stream?.stop?.();
  if (talk?.on) talkSend({ type: "hush" });
  const s = (hzWebStream = new PcmStream(24000));
  for (const p of parts) {
    if (s.stopped) return;
    if (p === "#wrong") { s.pushBytes(hzWrongTone()); continue; }
    try { s.pushBytes((await hzWebPcm(p)).slice(0)); } catch (_) { /* 缺这段就跳过 */ }
  }
  s.close();
}
function hzWrongTone() {
  const r = 24000, n = Math.round(r * 0.09), gap = Math.round(r * 0.05);
  const pcm = new Int16Array(n * 2 + gap);
  let pos = 0;
  for (const f of [330, 262]) {
    for (let i = 0; i < n; i++) {
      const env = Math.min(1, i / (r * 0.006), (n - i) / (r * 0.03));
      pcm[pos++] = Math.round(32767 * 0.32 * env * (0.75 * Math.sin(2 * Math.PI * f * i / r) + 0.25 * Math.sin(4 * Math.PI * f * i / r)));
    }
    if (f === 330) pos += gap;
  }
  return pcm.buffer;
}
