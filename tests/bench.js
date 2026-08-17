/*
 * Times the three stages of a report render, outside a browser.
 *
 * The numbers are indicative - no canvas, no compositing - but they track the parts
 * that actually cost something: the squarified layout, the per-pixel cushion loop, and
 * building a highlight image.
 *
 * Usage: node tests/bench.js report.html
 */
const { load, SIZE, reporter } = require('./harness');

const path = process.argv[2];
if (!path) {
    console.error('usage: node tests/bench.js <report.html>');
    process.exit(2);
}

const P = load(path);
const { W, H } = SIZE;

function ms(fn, runs) {
    runs = runs || 5;
    const start = process.hrtime.bigint();
    for (let i = 0; i < runs; i++) fn();
    return (Number(process.hrtime.bigint() - start) / 1e6 / runs).toFixed(1);
}

P.render();
const surface = [0, 0, 0, 0];

console.log('layout      : ' + ms(() => {
    P.reset();
    P.layout(P.D.root, 0, 0, W, H, 0, surface, null);
}) + ' ms');

const rects = P.rects();
let pixels = 0;
for (const rect of rects) pixels += Math.round(rect.w) * Math.round(rect.h);
console.log('              ' + rects.length + ' rectangles, ' + pixels + ' px');

const image = { width: W, height: H, data: new Uint8ClampedArray(W * H * 4) };
console.log('paint       : ' + ms(() => {
    for (const rect of rects) P.paintCushion(image, rect);
}) + ' ms');

console.log('render      : ' + ms(() => P.render()) + ' ms');

const types = new Set(rects.map(rect => rect.e));
const target = [...types].find(id => id >= 0);
if (target !== undefined) {
    console.log('highlight   : ' + ms(() => {
        globalThis.dimmedId = null;
        P.dimmedImage(target);
    }, 10) + ' ms (cold)');
}

const hits = 200;
console.log('hit-test    : ' + ms(() => {
    for (let i = 0; i < hits; i++) P.hitTest((i * 6) % W, (i * 4) % H);
}, 10) + ' ms for ' + hits + ' lookups');
