/*
 * Checks the commands the context menu puts on the clipboard.
 *
 * The report never runs anything, so the only thing that can go wrong is the text
 * itself. A path is arbitrary bytes: quotes, spaces, newlines and backslashes are all
 * ordinary on a filesystem. Every command is therefore handed to a real shell, which
 * must resolve it to exactly one argument equal to the intended target. Nothing is
 * executed beyond `set --` and `printf`, both builtins, and the whole report goes
 * through one bash invocation — a process per path costs minutes on a large tree.
 *
 * Usage: node tests/context.js report.html
 */
const { execFileSync } = require('child_process');
const { load, reporter } = require('./harness');

const path = process.argv[2];
if (!path) {
    console.error('usage: node tests/context.js <report.html>');
    process.exit(2);
}

const P = load(path);
const r = reporter();

/**
 * Resolves quoted shell words in one batch.
 * @param {string[]} words quoted words, one per check
 * @returns {Array<[number, string]>} argument count and first argument for each
 */
function shellResolve(words) {
    if (!words.length) return [];
    const script = words.map((w) => 'set -- ' + w + '; printf "%s\\0%s\\0" "$#" "$1"').join('\n');
    const out = execFileSync('bash', ['-s'], { input: script, maxBuffer: 1 << 28 }).toString('utf8');
    const parts = out.split('\0');
    const pairs = [];
    for (let i = 0; i + 1 < parts.length; i += 2) pairs.push([Number(parts[i]), parts[i + 1]]);
    return pairs;
}

// Walk every node, keeping the index so paths can be rebuilt.
const files = [], dirs = [], aggs = [];
(function walk(node, idx) {
    if (node.length === 4) {
        if (idx.length) dirs.push(idx.slice());
        node[3].forEach((child, i) => walk(child, idx.concat(i)));
    } else if (node.length === 3) {
        aggs.push(idx.slice());
    } else {
        files.push(idx.slice());
    }
})(P.D.root, []);

console.log(files.length + ' files, ' + dirs.length + ' folders, ' + aggs.length + ' aggregates');

// --- what each entry should say -------------------------------------------
const PREFIX = { ctxCd: 'cd ', ctxLs: 'ls -la ', ctxRm: 'rm -i -- ' };
const structure = [];
const checks = [];

for (const idx of files.concat(dirs)) {
    const node = P.nodeAt(idx);
    const isDir = node.length === 4;
    const full = P.pathOf(idx);
    const cut = full.lastIndexOf('/');
    const holder = isDir ? full : (cut > 0 ? full.slice(0, cut) : '/');
    const items = P.ctxCommands(node, idx);

    const want = isDir ? ['ctxPath', 'ctxCd', 'ctxLs', 'ctxScan', 'ctxBig']
        : ['ctxPath', 'ctxCd', 'ctxLs', 'ctxRm'];
    const keys = items.map((i) => i.key);
    if (keys.join() !== want.join()) structure.push('entries for ' + full + ': ' + keys.join());
    if (items[0].text !== full) structure.push('path entry for ' + full);

    // The path alone must survive quoting, and so must each command's argument.
    checks.push({ word: P.shQuote(full), want: full, what: 'path of ' + full });
    for (const item of items) {
        const prefix = PREFIX[item.key];
        if (!prefix) continue;
        if (item.text.indexOf(prefix) !== 0) {
            structure.push(item.key + ' prefix on ' + full);
            continue;
        }
        checks.push({
            word: item.text.slice(prefix.length),
            want: item.key === 'ctxRm' ? full : holder,
            what: item.key + ' on ' + full,
        });
    }
}

const started = process.hrtime.bigint();
const resolved = shellResolve(checks.map((c) => c.word));
const seconds = Number(process.hrtime.bigint() - started) / 1e9;

const wrong = [];
for (let i = 0; i < checks.length; i++) {
    const [count, value] = resolved[i] || [0, ''];
    if (count !== 1 || value !== checks[i].want) {
        wrong.push(checks[i].what + ' -> ' + count + ' arg(s), ' + JSON.stringify(value));
    }
}

r.check(checks.length + ' commands resolve to exactly one intended target', !wrong.length);
wrong.slice(0, 4).forEach((w) => console.log('       ' + w));
r.note('one bash invocation, ' + seconds.toFixed(2) + ' s');

r.check('every menu offers the entries its node kind calls for', !structure.length);
structure.slice(0, 4).forEach((s) => console.log('       ' + s));

const dirOffersRm = dirs.some((idx) => P.ctxCommands(P.nodeAt(idx), idx).some((i) => i.key === 'ctxRm'));
r.check('no folder is offered a delete command', !dirOffersRm);

const aggOpens = aggs.filter((idx) => P.ctxTargetOf(idx) !== null);
r.check('aggregate nodes open no menu (' + aggs.length + ' checked)', !aggOpens.length);

const scanOk = dirs.every((idx) => {
    const item = P.ctxCommands(P.nodeAt(idx), idx).find((i) => i.key === 'ctxScan');
    return item.text === 'superdirstat ' + P.shQuote(P.pathOf(idx)) + ' report.html';
});
r.check('the rescan command names the folder it was opened on', scanOk);

r.done();
