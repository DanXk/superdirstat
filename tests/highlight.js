/*
 * Checks the file-type highlight, pixel by pixel.
 *
 * Clicking a row in the type panel must leave the matching rectangles untouched and
 * fade everything else towards the map background. Fading towards the background,
 * rather than multiplying towards black, is what makes it work on a light theme.
 *
 * Usage: node tests/highlight.js report.html
 */
const { load, SIZE, reporter } = require('./harness');

const path = process.argv[2];
if (!path) {
    console.error('usage: node tests/highlight.js <report.html>');
    process.exit(2);
}

const P = load(path);
const { W } = SIZE;
const r = reporter();

P.render();
let rects = P.rects();
let base = P.base();

// Pick the type with the most rectangles, plus one witness on each side.
const counts = new Map();
for (const rect of rects) counts.set(rect.e, (counts.get(rect.e) || 0) + 1);
const target = [...counts.entries()].filter(e => e[0] >= 0).sort((a, b) => b[1] - a[1])[0][0];
console.log('type id ' + target + ', ' + counts.get(target) + ' of ' + rects.length + ' rectangles');

function pixel(data, rect) {
    const x = Math.round(rect.x) + 1, y = Math.round(rect.y) + 1;
    const o = (y * W + x) * 4;
    return [data[o], data[o + 1], data[o + 2]];
}
function findWitnesses(list) {
    return {
        match: list.find(x => x.e === target && x.w > 3 && x.h > 3),
        other: list.find(x => x.e !== target && x.w > 3 && x.h > 3),
    };
}

// The fade the report is expected to apply to everything outside the selected type.
const mapBg = P.rgbOf(P.tok('--map-bg'));
function faded(channel, value) {
    return mapBg[channel] * 0.8 + value * 0.2;
}

const image = P.dimmedImage(target);
let { match, other } = findWitnesses(rects);

const keptBefore = pixel(base.data, match);
const keptAfter = pixel(image.data, match);
r.check('selected type keeps its pixels byte for byte',
    keptBefore.every((v, i) => v === keptAfter[i]));

const otherBefore = pixel(base.data, other);
const otherAfter = pixel(image.data, other);
r.check('other types fade towards the map background',
    otherAfter.every((v, i) => Math.abs(v - faded(i, otherBefore[i])) <= 1));
r.check('the fade does not flatten to a single colour',
    otherAfter.some((v, i) => v !== mapBg[i]) || otherBefore.every((v, i) => v === mapBg[i]));

let opaque = true;
for (let i = 3; i < image.data.length; i += 4) {
    if (image.data[i] !== 255) { opaque = false; break; }
}
r.check('every pixel stays opaque', opaque);
r.check('the same type reuses the cached image', P.dimmedImage(target) === image);

// --- the highlight must survive a zoom, recomputed against the new layout ---
P.setPin(target);
const biggest = rects.slice().sort((a, b) => b.node[1] - a.node[1])[0];
const parent = P.idxOf(biggest).slice(0, -1);
if (parent.length) {
    P.zoomTo(parent);
    rects = P.rects();
    base = P.base();
    r.check('zoom keeps the filter pinned', P.pin() === target);

    const zoomed = P.dimmedImage(target);
    const witnesses = findWitnesses(rects);
    if (witnesses.match) {
        const before = pixel(base.data, witnesses.match);
        const after = pixel(zoomed.data, witnesses.match);
        r.check('zoom keeps the selected type at full brightness',
            before.every((v, i) => v === after[i]));
    } else {
        r.note('no rectangle of this type in the zoomed view');
    }
    if (witnesses.other) {
        const before = pixel(base.data, witnesses.other);
        const after = pixel(zoomed.data, witnesses.other);
        r.check('zoom fades the other types on the new layout',
            after.every((v, i) => Math.abs(v - faded(i, before[i])) <= 1));
    }
}

// --- switching language must not disturb the map ---
P.setLang('fr');
r.check('language switch keeps the filter pinned', P.pin() === target);
r.check('language switch reaches the report', P.lang() === 'fr');

r.done();
