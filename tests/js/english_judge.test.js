// 英文书写判错：每个字母每一笔，标准写法（加一点手抖）要判对；反着写判「方向反了」；挪到别处判错。
// node tests/js/english_judge.test.js（pytest 里 test_english.py 会调它）
global.window = {};
global.esc = (s) => String(s);
global.document = { createElement: () => ({}) };
require("../../server/static/letters.js");
require("../../server/static/english.js");
const L = window.LETTERS, cmp = window.enCompare;
let rnd = 1;
const rand = () => { rnd = (rnd * 16807) % 2147483647; return rnd / 2147483647 - 0.5; };
const jitter = (pts, a) => pts.map(([x, y]) => [x + rand() * a, y + rand() * a]);
const dense = (pts) => { const out = []; for (let i = 1; i < pts.length; i++) for (let t = 0; t < 4; t++) { const f = t / 4; out.push([pts[i-1][0] + (pts[i][0]-pts[i-1][0])*f, pts[i-1][1] + (pts[i][1]-pts[i-1][1])*f]); } out.push(pts[pts.length-1]); return out; };
let fail = 0, n = 0;
for (const [ch, strokes] of Object.entries(L)) {
  strokes.forEach((s, i) => {
    const isDot = s.length <= 2 && Math.hypot(s[0][0]-s[s.length-1][0], s[0][1]-s[s.length-1][1]) < 6;
    const good = cmp(jitter(dense(s), 5), s, 100);
    n++;
    if (!good.ok) { fail++; console.log(`✗ ${ch} 第${i+1}笔 标准写法被判错：${good.why}`); }
    if (isDot) return;
    const rev = cmp(dense([...s].reverse()), s, 100);
    const closed = Math.hypot(s[0][0]-s[s.length-1][0], s[0][1]-s[s.length-1][1]) < 8;  // O、o 这种闭合的反着画也对得上
    if (!closed && rev.ok) { fail++; console.log(`✗ ${ch} 第${i+1}笔 反着写被判对`); }
    const moved = cmp(dense(s).map(([x, y]) => [x + 45, y + 40]), s, 100);
    if (moved.ok) { fail++; console.log(`✗ ${ch} 第${i+1}笔 挪远了被判对`); }
  });
}
console.log(`${n} 笔，${fail} 个问题`);
process.exit(fail ? 1 : 0);
