"use strict";
// 查找聊天记录（顶栏 🗂）：像微信「查找聊天记录」——关键字、按说话人（群成员 + 小新）、只看图片 / 文件。
// 结果新的在前（GET api/thread/search）；点一条跳到聊天里那条（GET api/thread/around，拿它前后一段换掉当前页面），
// 跳过去后关掉搜索栏、往上照常懒加载；底下出「回到最新」，点了重新打开群（最新一页）。

const hist = { who: "", kind: "", timer: 0, seq: 0 };

function histKidQ() { return state.conv?.kid ? `&kid=${encodeURIComponent(state.conv.kid)}` : ""; }

function histOpen() {
  let el = $("hist");
  if (!el) {
    el = document.createElement("aside");
    el.id = "hist"; el.className = "hist"; el.setAttribute("aria-label", "查找聊天记录");
    el.innerHTML = `<div class="hist-head"><h3>查找聊天记录</h3><button type="button" data-h="close">关闭</button></div>
      <div class="hist-q"><input type="search" placeholder="搜关键字，比如「作业」「围棋」" aria-label="搜关键字" enterkeyhint="search"></div>
      <div class="hist-chips" role="group" aria-label="筛选"></div>
      <div class="hist-count" aria-live="polite"></div>
      <div class="hist-list"></div>`;
    document.body.appendChild(el);
    el.querySelector("input").addEventListener("input", () => { clearTimeout(hist.timer); hist.timer = setTimeout(histSearch, 250); });
    el.addEventListener("click", histClick);
  }
  el.hidden = false;
  histChips();
  histSearch();
  setTimeout(() => el.querySelector("input").focus(), 50);
}

function histClose() { const el = $("hist"); if (el) el.hidden = true; }

// 筛选：全部 / 每个群成员 / 小新 ｜ 图片 / 文件
function histChips() {
  const people = Object.entries(state.people || {})
    .filter(([id, p]) => p.role === "kid" ? id === state.conv?.kid : true);   // 别的孩子不在这个群里
  const who = [["", "全部"], ...people.map(([id, p]) => [id, `${p.avatar || ""} ${p.name}`]), ["xiaoxin", "小新"]];
  $("hist").querySelector(".hist-chips").innerHTML =
    who.map(([id, n]) => `<button type="button" data-who="${esc(id)}"${hist.who === id ? ' class="on"' : ""}>${esc(n.trim())}</button>`).join("")
    + `<span class="sep"></span>`
    + [["image", "🖼 图片"], ["file", "📄 文件"]].map(([k, n]) => `<button type="button" data-kind="${k}"${hist.kind === k ? ' class="on"' : ""}>${n}</button>`).join("");
}

async function histSearch() {
  const el = $("hist");
  if (!el || el.hidden || !state.conv) return;
  const q = el.querySelector("input").value.trim();
  const my = ++hist.seq;
  const list = el.querySelector(".hist-list"), count = el.querySelector(".hist-count");
  if (!q && !hist.who && !hist.kind) {
    count.textContent = "";
    list.innerHTML = '<div class="hist-empty">输入关键字，或者点上面按人、按图片文件筛</div>';
    return;
  }
  count.textContent = "搜索中…";
  let d;
  try {
    d = await api(`api/thread/search?q=${encodeURIComponent(q)}&who=${encodeURIComponent(hist.who)}&kind=${hist.kind}${histKidQ()}`).then(json);
  } catch (e) { count.textContent = e.message; return; }
  if (my !== hist.seq) return;   // 打字快：只画最后一次的
  count.textContent = d.results.length ? `找到 ${d.results.length >= 100 ? "100+" : d.results.length} 条` : "";
  const words = q.split(/\s+/).filter(Boolean);
  list.innerHTML = d.results.length ? d.results.map((m) => histItem(m, words)).join("") : '<div class="hist-empty">没找到</div>';
}

function histMark(text, words) {
  let out = esc(text);
  for (const w of words) {
    const re = new RegExp(esc(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    out = out.replace(re, (x) => `<mark>${x}</mark>`);
  }
  return out;
}

function histItem(m, words) {
  const p = m.from === "xiaoxin" ? { name: "小新", avatar: "" } : person(m.from);
  const av = m.from === "xiaoxin" ? '<img src="static/icon.png" alt="">' : esc(p.avatar);
  const d = new Date(m.created * 1000), now = new Date();
  const when = d.toDateString() === now.toDateString()
    ? `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
    : `${d.getFullYear() !== now.getFullYear() ? d.getFullYear() + "/" : ""}${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const imgs = m.files.filter((f) => f.kind === "image"), docs = m.files.filter((f) => f.kind !== "image");
  const thumbs = imgs.length || docs.length
    ? `<div class="thumbs">${imgs.map((f) => `<img src="api/files/${f.id}?v=thumb" alt="${esc(f.name)}" loading="lazy">`).join("")}`
      + `${docs.map((f) => `<span>📄 ${histMark(f.name, words)}</span>`).join("")}</div>` : "";
  return `<button type="button" class="hist-item" data-mid="${esc(m.id)}"><span class="av">${av}</span><span>`
    + `<span class="who"><b>${esc(p.name)}</b><span>${when}</span></span>`
    + (m.text ? `<span class="txt">${histMark(m.text, words)}</span>` : "") + thumbs + `</span></button>`;
}

function histClick(e) {
  const b = e.target.closest("[data-h],[data-who],[data-kind],.hist-item");
  if (!b) return;
  if (b.dataset.h === "close") { histClose(); return; }
  if (b.dataset.who !== undefined) { hist.who = hist.who === b.dataset.who ? "" : b.dataset.who; histChips(); histSearch(); return; }
  if (b.dataset.kind) { hist.kind = hist.kind === b.dataset.kind ? "" : b.dataset.kind; histChips(); histSearch(); return; }
  if (b.dataset.mid) histJump(b.dataset.mid);
}

// 跳到某条：把聊天区换成它前后那一段，滚到它、闪一下。窄屏（手机）顺手关掉搜索栏
async function histJump(mid) {
  let d;
  try { d = await api(`api/thread/around?mid=${encodeURIComponent(mid)}${histKidQ()}`).then(json); } catch (e) { toast(e.message); return; }
  state.conv = { ...state.conv, ...d };
  state.openingUntil = 0;
  renderThread({ noBottom: true });   // 不按住底部：要停在跳到的那条
  const row = $("messages").querySelector(`[data-mid="${CSS.escape(mid)}"]`);
  chatStick.on = false;
  if (row) {
    const go = () => row.scrollIntoView({ block: "center" });
    go(); requestAnimationFrame(go); setTimeout(go, 400);   // 图片、卡片撑高后再对一次
    row.classList.add("hist-focus");
    setTimeout(() => row.classList.remove("hist-focus"), 2500);
  }
  $("hist-newer")?.remove();
  if (d.has_newer) {
    const btn = document.createElement("button");
    btn.id = "hist-newer"; btn.type = "button"; btn.className = "hist-newer"; btn.textContent = "↓ 回到最新";
    btn.addEventListener("click", () => { btn.remove(); openThread(state.conv.kid); });
    $("messages").appendChild(btn);
  }
  histClose();   // 搜索栏会盖住聊天右半边（pad 横屏实测），跳过去就关，像微信
}

$("open-history")?.addEventListener("click", () => ($("hist") && !$("hist").hidden ? histClose() : histOpen()));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("hist") && !$("hist").hidden) histClose(); });
