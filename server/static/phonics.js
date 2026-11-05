"use strict";
// 自然拼读拆解 + 音标（单词卡、英文书写卡共用）：
//   英 /keɪk/                      ← 英式音标（英音，女儿学的是英音）
//   [c] [a_e] [k]                  ← 一块一块的字母组合，元音红色、辅音蓝色、不发音的灰色
//   /k/  /eɪ/  /k/                 ← 每块发的音
//   ca·ke 这样的音节只在多音节词显示（el·e·phant）
// 拆法是服务端 Opus 拆、代码核对过的（server/phonics.py）；还没拆好就过两秒再要一次，拆不对的就只显示音标。

const PH_VOWEL = /[aeiouy]/i;

function phonicsBox(word) {
  const box = document.createElement("div");
  box.className = "ph-box";
  box.innerHTML = '<div class="ph-ipa">…</div>';
  if (!/^[A-Za-z][A-Za-z '\-]*$/.test(String(word || "").trim())) { box.hidden = true; return box; }
  const w = encodeURIComponent(String(word).trim());
  let tries = 0;
  const load = () => api(`api/english/phonics/${w}?wait=${tries ? 0 : 8}`).then(json).then((d) => {
    phonicsFill(box, d);
    if (d.pending && ++tries < 6) setTimeout(load, 2500);   // 后台在拆：过一会儿再要
  }).catch(() => { box.querySelector(".ph-ipa").textContent = ""; });
  load();
  return box;
}

function phonicsFill(box, d) {
  const uk = String(d.uk || "").split(/[;,]/)[0].trim();   // 词典偶尔给两种读法，显示第一种
  box.querySelector(".ph-ipa").innerHTML = uk ? `<span class="ph-tag">英</span>/${esc(uk)}/` : "";
  box.querySelector(".ph-parts")?.remove();
  box.querySelector(".ph-syl")?.remove();
  if (!d.parts?.length) return;
  const row = document.createElement("div");
  row.className = "ph-parts";
  row.setAttribute("aria-label", "自然拼读：" + d.parts.map((p) => `${p.g} 读 ${p.s || "不发音"}`).join("，"));
  row.innerHTML = d.parts.map((p) => {
    const silent = !p.s;
    const kind = silent ? "silent" : (PH_VOWEL.test(p.g.replace(/_e$/, "")) && !/^(y)$/i.test(p.g) ? "v" : "c");
    const g = /_e$/.test(p.g) ? `${esc(p.g[0])}<i>_</i>e` : esc(p.g);
    return `<span class="ph-chip ${kind}"><b>${g}</b><small>${silent ? "不读" : "/" + esc(p.s) + "/"}</small></span>`;
  }).join("");
  box.appendChild(row);
  if (d.syl && d.syl.includes("·")) {
    const s = document.createElement("div");
    s.className = "ph-syl";
    s.innerHTML = `<span class="ph-tag">音节</span>${esc(d.syl)}`;
    box.appendChild(s);
  }
}
