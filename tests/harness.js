/*
 * Shared test harness: runs a generated report's JavaScript outside a browser.
 *
 * Two things matter here.
 *
 * The script is compiled with vm.runInThisContext, never eval(). Code from a direct
 * eval() is not optimised the way script code is, which made an early version of this
 * harness report a 423 ms render for what actually takes 37 ms - and sent an
 * "optimisation" down the wrong path. If you change how this file loads the report,
 * check a known timing before trusting any number that comes out of it.
 *
 * rgbOf() is replaced with a deterministic stub, because the real one converts a CSS
 * colour through a 1x1 canvas that does not exist here. Colours in these tests are
 * therefore fake but self-consistent, which is all the geometry and masking checks
 * need.
 */
const fs = require('fs');
const vm = require('vm');

const RGB_STUB = 'function rgbOf(css) {' +
    ' var c = rgbOf.c || (rgbOf.c = {});' +
    ' if (c[css]) return c[css];' +
    ' var h = 0;' +
    ' for (var i = 0; i < css.length; i++) h = (h * 7 + css.charCodeAt(i)) % 255;' +
    ' return (c[css] = [h, (h * 3) % 255, (h * 5) % 255]);' +
    ' }';

const PROBE = ';globalThis.__P = {' +
    ' D: D, nodeAt: nodeAt, pathOf: pathOf, idxOf: idxOf, hitTest: hitTest,' +
    ' layout: layout, paintCushion: paintCushion, render: render, zoomTo: zoomTo,' +
    ' dimmedImage: dimmedImage, typeIdOf: typeIdOf, tok: tok, rgbOf: rgbOf,' +
    ' fmt: fmt, setLang: setLang, buildExtList: buildExtList,' +
    ' rects: function () { return rects; },' +
    ' labels: function () { return labels; },' +
    ' base: function () { return base; },' +
    ' root: function () { return root; },' +
    ' lang: function () { return lang; },' +
    ' pin: function () { return pinnedType; },' +
    ' setPin: function (v) { pinnedType = v; },' +
    ' reset: function () { rects = []; labels = []; rectOf = new Map(); }' +
    '};';

function element(id) {
    const el = {
        id,
        _html: '',
        attrs: {},
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        clientWidth: 1200,
        clientHeight: 800,
        offsetTop: 0,
        offsetHeight: 18,
        offsetWidth: 100,
        scrollTop: 0,
        width: 1200,
        height: 800,
        value: '',
        title: '',
        textContent: '',
        hidden: false,
        children: [],
        set innerHTML(v) { this._html = v; },
        get innerHTML() { return this._html; },
        get firstChild() { return element('row'); },
        get parentNode() { return element('parent'); },
        get nextSibling() { return null; },
        appendChild() {},
        insertBefore() {},
        removeChild() {},
        addEventListener() {},
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
        removeAttribute(k) { delete this.attrs[k]; },
        querySelector(sel) { return sel === '.caret' ? element('caret') : null; },
        querySelectorAll() { return []; },
        getBoundingClientRect() { return { left: 0, top: 0 }; },
        getContext() { return CONTEXT; },
    };
    return el;
}

const CONTEXT = {
    fillStyle: '', strokeStyle: '', lineWidth: 1, font: '', textBaseline: '',
    fillRect() {}, strokeRect() {}, fillText() {},
    measureText(text) { return { width: text.length * 6 }; },
    createImageData(w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }; },
    getImageData(x, y, w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }; },
    putImageData() {},
};

/**
 * Loads a generated report and returns its internals.
 * @param {string} htmlPath path to a report produced by superdirstat.py
 * @returns {object} probe exposing the report's functions and state
 */
function load(htmlPath) {
    const html = fs.readFileSync(htmlPath, 'utf8');
    // The main script is the last one: the theme bootstrap sits earlier in <head>.
    const blocks = html.split('<script>');
    const js = blocks[blocks.length - 1].split('</script>')[0];

    const patched = js.replace(/function rgbOf\(css\) \{[\s\S]*?\n\}/, RGB_STUB);
    if (patched === js) throw new Error('could not stub rgbOf - has it been renamed?');

    const canvas = element('map');
    globalThis.document = {
        getElementById: (id) => (id === 'map' ? canvas : element(id)),
        createElement: () => element('tmp'),
        documentElement: element('html'),
        addEventListener() {},
        querySelector() { return null; },
    };
    globalThis.navigator = { language: 'en-GB' };
    globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#808080' });
    globalThis.localStorage = { getItem: () => null, setItem() {} };
    globalThis.innerWidth = 1600;
    globalThis.innerHeight = 900;
    globalThis.addEventListener = () => {};

    vm.runInThisContext(patched + PROBE, { filename: 'report.js' });
    return globalThis.__P;
}

/** Canvas size the stubbed layout runs at. */
const SIZE = { W: 1200, H: 800 };

function reporter() {
    let failures = 0;
    return {
        check(label, ok) {
            console.log((ok ? '  ok   ' : '  FAIL ') + label);
            if (!ok) failures++;
        },
        note(text) { console.log('  --   ' + text); },
        done() {
            console.log(failures ? failures + ' failure(s)' : 'all checks passed');
            process.exitCode = failures ? 1 : 0;
            return failures;
        },
    };
}

module.exports = { load, SIZE, reporter };
