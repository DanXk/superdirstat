/*
 * Checks the treemap layout itself: the rectangles must tile the canvas exactly, and
 * every rectangle's area must be proportional to the size it represents.
 *
 * These two properties are what make a treemap readable, and both have been broken in
 * the past by changes that looked harmless - once by a directory's own inode blocks,
 * which are part of a folder's size but carried by no child, so children shared an
 * area larger than their real sum (16.8 % error on deep, mostly-empty trees).
 *
 * Usage: node tests/geometry.js report.html
 */
const { load, SIZE, reporter } = require('./harness');

const path = process.argv[2];
if (!path) {
    console.error('usage: node tests/geometry.js <report.html>');
    process.exit(2);
}

const P = load(path);
const { W, H } = SIZE;
const r = reporter();

P.render();
const rects = P.rects();
console.log(rects.length + ' rectangles, root ' + P.fmt(P.nodeAt(P.root())[1]));

// --- exact tiling: every pixel centre belongs to exactly one rectangle ---
const cover = new Uint8Array(W * H);
let outOfBounds = 0;
let area = 0;
for (const rect of rects) {
    area += rect.w * rect.h;
    if (rect.x < -0.01 || rect.y < -0.01 ||
        rect.x + rect.w > W + 0.01 || rect.y + rect.h > H + 0.01) outOfBounds++;
    for (let y = Math.max(0, Math.floor(rect.y)); y < Math.min(H, Math.ceil(rect.y + rect.h)); y++) {
        for (let x = Math.max(0, Math.floor(rect.x)); x < Math.min(W, Math.ceil(rect.x + rect.w)); x++) {
            const cx = x + 0.5, cy = y + 0.5;
            if (cx >= rect.x && cx < rect.x + rect.w && cy >= rect.y && cy < rect.y + rect.h) {
                cover[y * W + x]++;
            }
        }
    }
}
let bare = 0, overlapping = 0;
for (let i = 0; i < cover.length; i++) {
    if (cover[i] === 0) bare++;
    else if (cover[i] > 1) overlapping++;
}

r.check('no rectangle leaves the canvas', outOfBounds === 0);
r.check('no two rectangles overlap', overlapping === 0);
// Sub-pixel slivers are dropped on purpose, so a few bare pixels are expected.
r.check('bare pixels stay marginal (' + bare + ')', bare < W * H * 0.01);
r.note('coverage ' + (area / (W * H) * 100).toFixed(1) + ' %');

// --- areas proportional to sizes ---
const total = P.nodeAt(P.root())[1];
let worst = 0, worstPath = '';
for (const rect of rects) {
    if (rect.w * rect.h < 400) continue;
    const expected = (rect.node[1] / total) * W * H;
    const err = Math.abs(rect.w * rect.h - expected) / expected;
    if (err > worst) { worst = err; worstPath = P.pathOf(P.idxOf(rect)); }
}
r.check('area error under 1 % (' + (worst * 100).toFixed(2) + ' %)', worst < 0.01);
if (worst > 0.001) r.note('worst: ' + worstPath.slice(-70));

// --- squarification quality: rectangles should stay near square ---
const ratios = rects.filter(x => x.w * x.h > 200)
    .map(x => Math.max(x.w / x.h, x.h / x.w))
    .sort((a, b) => a - b);
if (ratios.length) {
    const median = ratios[ratios.length >> 1];
    r.check('median aspect ratio under 2 (' + median.toFixed(2) + ')', median < 2);
    r.note('p90 ' + ratios[Math.floor(ratios.length * 0.9)].toFixed(2) +
        ', max ' + ratios[ratios.length - 1].toFixed(1));
}

// --- hit-testing agrees with the layout ---
let misses = 0;
for (const rect of rects) {
    if (P.hitTest(rect.x + rect.w / 2, rect.y + rect.h / 2) !== rect) misses++;
}
r.check('hit-test resolves every rectangle centre', misses === 0);

// --- zoom rebuilds a valid layout ---
const biggest = rects.slice().sort((a, b) => b.node[1] - a.node[1])[0];
const parent = biggest ? P.idxOf(biggest).slice(0, -1) : [];
if (parent.length) {
    P.zoomTo(parent);
    r.check('zoom produces a non-empty layout', P.rects().length > 0);
}

r.done();
