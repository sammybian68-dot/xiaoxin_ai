"use strict";
// 作业卡：小新调 homework_report 后，气泡里插一张卡——按天分组，每天按学科（语文红、数学蓝、英语紫）列出来，
// 正文照原样，附图点开能左右翻，群文件点了下载。一天一块，最近的在最上面。

const HW_SUBJECT = { chinese: ["语", "语文", "c"], math: ["数", "数学", "m"], english: ["英", "英语", "e"], other: ["其", "其他", "o"] };

async function homeworkCard(row, args) {
  let d;
  const q = new URLSearchParams();
  for (const k of ["date", "since", "until"]) if (args[k]) q.set(k, args[k]);
  try { d = await api(`api/homework${q.toString() ? "?" + q : ""}`).then(json); } catch (_) { return; }
  const days = d.days || [];
  if (!days.length) return;
  const card = document.createElement("div");
  card.className = "hw-card";
  card.innerHTML = days.map((r) => {
    const items = (r.items || []).map((it) => {
      const [tag, name, cls] = HW_SUBJECT[it.subject] || HW_SUBJECT.other;
      const imgs = (it.images || []).map((f) => `<a href="api/files/${f}" data-fid="${f}"><img src="api/files/${f}?v=thumb" alt="附图" loading="lazy"></a>`).join("");
      const files = (it.files || []).map((f) => `<a class="hw-file" href="api/files/${f.fid}" download>📄 ${esc(f.name || "文件")}</a>`).join("");
      const miss = (it.mentions || []).filter((m) => !(it.files || []).some((f) => f.name === m))
        .map((m) => `<span class="hw-miss">老师提到「${esc(m)}」，群里没找到</span>`).join("");
      return `<div class="hw-item"><div class="hw-head"><span class="hw-tag ${cls}">${tag}</span><b>${name}</b>`
        + `${it.teacher ? `<small>${esc(it.teacher)}</small>` : ""}`
        + `${it.revised ? '<span class="hw-rev">已修改</span>' : ""}`
        + `${it.submit ? `<span class="hw-submit${it.submit.includes("无需") ? " ok" : ""}">${esc(it.submit)}</span>` : ""}</div>`
        + `<div class="hw-text">${esc(it.text || "（没抓到正文）")}</div>`
        + (it.notes || []).map((n) => `<div class="hw-note">${esc(n)}</div>`).join("")
        + (imgs ? `<div class="hw-imgs">${imgs}</div>` : "") + (files ? `<div class="hw-files">${files}</div>` : "") + miss + `</div>`;
    }).join("") || '<div class="hw-text">这天没有作业</div>';
    return `<section class="hw-day"><h4>${r.day.slice(5).replace("-", " 月 ")} 日 · 星期${r.week}</h4>${items}</section>`;
  }).join("");
  // 好几天的时候：顶上一排日期，点了滚到那天（一周的作业一屏看不完）
  if (days.length > 1) {
    const nav = document.createElement("div");
    nav.className = "hw-nav";
    nav.innerHTML = days.map((r, i) => `<button type="button" data-hw-go="${i}">${r.day.slice(5).replace("-", "/")} 周${r.week}`
      + `<small>${(r.items || []).length} 科</small></button>`).join("");
    nav.addEventListener("click", (e) => {
      const b = e.target.closest("[data-hw-go]");
      if (b) card.querySelectorAll(".hw-day")[+b.dataset.hwGo]?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    card.prepend(nav);
  }
  // 附图要能在查看器里翻：登记到 extraFiles
  const extra = {};
  for (const r of days) for (const it of r.items || []) for (const f of it.images || []) extra[f] = { id: f, name: `${r.day} ${HW_SUBJECT[it.subject]?.[1] || ""}作业附图`, kind: "image" };
  state.extraFiles = { ...(state.extraFiles || {}), ...extra };
  row.querySelector(".bubble").insertBefore(card, row.querySelector(".bubble .text"));
  requestAnimationFrame(() => revealRow(row));
}
window.homeworkCard = homeworkCard;
