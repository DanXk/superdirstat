#!/usr/bin/env python3
"""
superdirstat - WinDirStat for Linux.

Scans a directory tree and writes a single self-contained HTML report: a squarified
treemap with van Wijk cushion shading, next to a synchronised, clickable file tree.

    ./superdirstat.py /path/to/scan report.html
    ./superdirstat.py / root.html        # ~13 s for 1.25 M files on a busy server

No dependencies: the standard library does the scan, the browser does the drawing.

Implementation notes:
- Size is st_blocks * 512 (real disk usage, like WinDirStat); --apparent uses st_size.
- Virtual filesystems (/proc, /sys, tmpfs...) are skipped: they occupy no disk. Their
  mount points are read from /proc/self/mountinfo rather than hard-coded, because the
  real list depends on the machine. --include-pseudo brings them back.
- Symlinks are never followed; hardlinks are counted once.
- The tree is pruned before it is emitted: any node below a relative threshold is
  folded into a single "+ N items" node. Without this, a scan of several million
  files produces a JSON payload no browser will open. The threshold is raised
  automatically until the node count fits under --max-nodes.
- The report holds no text of its own: every label is built in the browser from an
  i18n table, so the language selector can switch the interface without re-scanning.
  That is why aggregate nodes carry a count instead of a rendered label, and why the
  metadata line is emitted as structured values rather than as HTML.
"""

import argparse
import fnmatch
import json
import os
import sys
import time
from datetime import datetime
from html import escape

VERSION = '1.0.0'

# Nodes are compact lists/tuples, to keep the JSON payload small:
#   file      -> (name, size)                       len 2
#   aggregate -> [item_count, size, 1]              len 3, 0 items = folder overhead
#   folder    -> [name, size, file_count, [...]]    len 4
# An aggregate holds a count rather than a label, because a label is interface: it is
# built in the browser so the language selector can change it.
KIND_DIR = 4

# Extensions listed individually in the side panel; the rest is grouped under
# "other extensions" so every file stays reachable with a click.
EXT_LIST_SIZE = 40

# Filesystems that consume no disk space. Walking them during a scan of `/` costs
# time and floods the treemap with thousands of zero-byte entries. tmpfs is in the
# list on purpose (/run, /dev/shm): that is RAM, not disk.
PSEUDO_FS = frozenset((
    'proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2', 'securityfs',
    'debugfs', 'tracefs', 'fusectl', 'configfs', 'mqueue', 'hugetlbfs', 'pstore', 'bpf',
    'binfmt_misc', 'autofs', 'nsfs', 'rpc_pipefs', 'selinuxfs', 'efivarfs', 'ramfs',
))

# mountinfo escapes awkward characters in octal.
MOUNT_ESCAPES = (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\'))

# Deepest directory level the walk descends into. The kernel already caps real depth:
# paths here are absolute, a level costs at least two bytes, so past PATH_MAX (4096)
# scandir fails with ENAMETOOLONG and the branch is counted as an access error. This
# bound exists so recursion depth is not what fails first, and so a pathological tree
# — a runaway script, a mangled backup — costs one reported number instead of the
# whole scan.
MAX_DEPTH = 2048


def pseudo_mount_points():
    """Mount points of virtual filesystems, read once at start-up.

    Read from /proc/self/mountinfo instead of hard-coding ['/proc', '/sys', ...]:
    the real list depends on the machine (containers, cgroup v1 vs v2, bind mounts).
    """
    points = set()
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                # The ' - ' separator is unambiguous: the kernel escapes spaces
                # inside mount paths as \040.
                head, _, tail = line.partition(' - ')
                if not tail:
                    continue
                fields = head.split(' ')
                if len(fields) < 5:
                    continue
                if tail.split(' ')[0] in PSEUDO_FS:
                    point = fields[4]
                    for escape_seq, char in MOUNT_ESCAPES:
                        point = point.replace(escape_seq, char)
                    points.add(point)
    except OSError:
        pass
    return points


def human(nbytes):
    """Human-readable size for terminal output (the report formats its own)."""
    units = ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB')
    value = float(nbytes)
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return '%d B' % nbytes
    text = ('%.2f' if value < 10 else '%.1f' if value < 100 else '%.0f') % value
    return text + ' ' + units[idx]


def group_int(number):
    return format(number, ',')


def plural(count, singular, plural_form=None):
    """'1 error' / '12 errors'. plural_form for words a trailing 's' does not fix."""
    word = singular if abs(count) < 2 else (plural_form or singular + 's')
    return '%s %s' % (group_int(count), word)


class Scanner:
    def __init__(self, opts):
        self.opts = opts
        self.total_size = 0
        self.files = 0
        self.dirs = 0
        self.errors = 0
        self.symlinks = 0
        self.too_deep = 0
        self.dedup_hardlinks = 0
        self.root_dev = None
        self.skip_points = set() if opts.include_pseudo else pseudo_mount_points()
        self.skipped_mounts = []
        self.seen_inodes = set()
        self.ext_stats = {}
        self.last_tick = 0.0

    def excluded(self, name, path):
        for pattern in self.opts.exclude:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path, pattern):
                return True
        return False

    def size_of(self, st):
        if self.opts.apparent:
            return st.st_size
        return st.st_blocks * 512

    def tick(self):
        if not self.opts.progress:
            return
        now = time.time()
        if now - self.last_tick < 0.25:
            return
        self.last_tick = now
        sys.stderr.write('\r  %s, %s, %s ' % (
            plural(self.files, 'file'), plural(self.dirs, 'folder'), human(self.total_size)))
        sys.stderr.flush()

    def account_ext(self, name, size):
        dot = name.rfind('.')
        if dot > 0 and dot < len(name) - 1 and len(name) - dot <= 12:
            ext = name[dot:].lower()
        else:
            # An empty key, not a label: the report translates it for display.
            ext = ''
        slot = self.ext_stats.get(ext)
        if slot is None:
            self.ext_stats[ext] = [size, 1]
        else:
            slot[0] += size
            slot[1] += 1

    def scan(self, path, label, depth=0):
        """Recursively scan one directory; returns a folder node."""
        self.dirs += 1
        size = 0
        nb_files = 0
        children = []

        try:
            st = os.lstat(path)
            size += self.size_of(st)
        except OSError:
            self.errors += 1

        try:
            iterator = os.scandir(path)
        except OSError:
            self.errors += 1
            return [label, size, 0, []]

        with iterator:
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError:
                    # Failure mid-iteration: give up on this directory but keep
                    # whatever was already collected.
                    self.errors += 1
                    break

                if self.excluded(entry.name, entry.path):
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    self.errors += 1
                    continue

                if is_dir:
                    if entry.path in self.skip_points:
                        self.skipped_mounts.append(entry.path)
                        continue
                    if self.opts.one_file_system and st.st_dev != self.root_dev:
                        continue
                    if depth >= MAX_DEPTH:
                        self.too_deep += 1
                        continue
                    child = self.scan(entry.path, entry.name, depth + 1)
                    size += child[1]
                    nb_files += child[2]
                    children.append(child)
                    continue

                if entry.is_symlink():
                    # Counted, never followed: the link's own blocks belong to this
                    # directory, as with du, but the target is not walked from here.
                    # Reported so the number is explainable, not as an omission.
                    self.symlinks += 1

                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in self.seen_inodes:
                        self.dedup_hardlinks += 1
                        continue
                    self.seen_inodes.add(key)

                fsize = self.size_of(st)
                if self.opts.min_file and fsize < self.opts.min_file:
                    # Aggregating during the scan: only useful for huge trees, where
                    # the memory held by the process becomes the limiting factor.
                    size += fsize
                    nb_files += 1
                    self.files += 1
                    self.total_size += fsize
                    self.account_ext(entry.name, fsize)
                    continue

                size += fsize
                nb_files += 1
                self.files += 1
                self.total_size += fsize
                self.account_ext(entry.name, fsize)
                children.append((entry.name, fsize))
                self.tick()

        return [label, size, nb_files, children]


def items_of(node):
    return node[2] if len(node) == KIND_DIR else 1


def prune(node, threshold):
    """Pruned copy of a subtree. Returns (node, node_count)."""
    label, size, nb_files, children = node
    kept = []
    count = 1
    agg_size = 0
    agg_items = 0

    # A directory's own blocks are part of `size` but are carried by no child. Without
    # materialising them, the children share an area larger than their real sum and
    # the treemap over-represents small files in deep trees.
    own = size - sum(child[1] for child in children)
    # Same reasoning for the item count: with --min-file, some files were counted in
    # nb_files without being added as children.
    own_items = nb_files - sum(items_of(child) for child in children)

    for child in children:
        if child[1] < threshold:
            agg_size += child[1]
            agg_items += items_of(child)
            continue
        if len(child) == KIND_DIR:
            sub, sub_count = prune(child, threshold)
            kept.append(sub)
            count += sub_count
        else:
            kept.append(child)
            count += 1

    agg_items += max(0, own_items)

    # An aggregate carries its item count and the browser turns it into a label. A
    # count of 0 marks "this folder's own blocks only", which reads differently.
    if agg_items:
        kept.append([agg_items, agg_size + max(0, own), 1])
        count += 1
    elif own > 0 and children and (own >= threshold or own > size * 0.05):
        kept.append([0, own, 1])
        count += 1

    kept.sort(key=lambda item: -item[1])
    return [label, size, nb_files, kept], count


def prune_adaptive(root, total, min_share, max_nodes):
    """Raise the threshold until the tree fits. Returns (node, count, threshold)."""
    threshold = max(1, int(total * min_share))
    while True:
        pruned, count = prune(root, threshold)
        if count <= max_nodes or threshold >= total:
            return pruned, count, threshold
        threshold = int(threshold * 1.6) + 1


# The report carries no comments of its own: it is code shipped to a browser, and it
# is rewritten on every scan. The non-obvious points of the template are noted here.
#
# - The canvas gets `width: 100%; height: 100%` on top of `inset: 0`. A canvas is a
#   replaced element, so its `auto` CSS height resolves to the intrinsic bitmap size
#   and `inset: 0` does not stretch it. Without both rules the CSS box stays frozen at
#   the size of the last render and a blank band appears when the window is resized.
# - The JS reads its colours from the CSS custom properties (`tok()`) instead of
#   holding its own: the `:root` blocks are the single source of truth, which is what
#   lets canvas-painted pixels follow the light/dark theme.
# - Rectangles are keyed by node identity (`rectOf`, a Map), not by a path string.
#   Building 40,000 text keys per render cost more than the render itself.
# - Subdivided directories keep their box in `rectOf` but stay out of `rects`: hovering
#   a tree row needs the box, while hit-testing must resolve to the leaf.
# - `paintCushion` hoists the per-row invariants and steps the x normal instead of
#   recomputing it. It is the only per-pixel loop in the report.
# - Highlighting walks the rectangles rather than a per-pixel type mask: a mask means
#   carrying an Int16Array the size of the canvas plus one more store in the render
#   loop, and saves nothing measurable.
# - Dimming fades towards the map background rather than multiplying towards black,
#   so it works on a light theme too.
# - Escape clears the type filter before leaving the zoom. The filter is the more
#   recent state, and un-zooming while the map stays dimmed reads as a bug.
# - In the type panel a null size marks a row with no weight of its own (compacted
#   folders, aggregates), whose volume depends on the zoom level: no size, no bar.
# - The panel hint and its reset link share one slot right of the title: at 250 px,
#   showing both breaks the header onto three lines.
# - The scan data arrives as a string for `JSON.parse`, not as an array literal. See
#   build_report() for the ceiling that motivates it.
# - Of the counters on the metadata line, only `too_deep` marks data missing from the
#   map, which is why it alone is painted with the `warn` class.
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>superdirstat &mdash; /*@@TITLE@@*/</title>
<script>
(function () {
    try {
        if (localStorage.getItem('superdirstat-theme') === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
        }
    } catch (e) {}
})();
</script>
<style>
:root {
    --bg: #f4f6f9;
    --panel: #ffffff;
    --panel-2: #eceff4;
    --line: #d5dbe4;
    --text: #1b2027;
    --muted: #5e6875;
    --accent: #1668c7;
    --warn: #9a5b0c;
    --hover: #e6eaf1;
    --sel: #cfe0f6;
    --size: #414b58;
    --bar: #d5dbe4;
    --tip-bg: #ffffff;
    --tip-shadow: rgba(20, 30, 45, .18);
    --map-bg: #f0f2f6;
    --outline-sel: #10161f;
    --outline-hover: rgba(20, 25, 35, .8);
    --outline-hover-box: rgba(10, 70, 140, .95);
    --outline-inner: rgba(255, 255, 255, .75);
}
:root[data-theme="dark"] {
    --bg: #14171c;
    --panel: #1b1f26;
    --panel-2: #21262f;
    --line: #2c323d;
    --text: #dfe4ec;
    --muted: #8d97a8;
    --accent: #4da3ff;
    --warn: #e0a352;
    --hover: #262c36;
    --sel: #2f4a6b;
    --size: #b9c2d0;
    --bar: #2c323d;
    --tip-bg: #0c0e12;
    --tip-shadow: rgba(0, 0, 0, .5);
    --map-bg: #1f2328;
    --outline-sel: #ffffff;
    --outline-hover: rgba(255, 255, 255, .8);
    --outline-hover-box: rgba(120, 190, 255, .95);
    --outline-inner: rgba(0, 0, 0, .75);
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
    background: var(--bg);
    color: var(--text);
    font: 13px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
header {
    flex: 0 0 auto;
    padding: 10px 16px;
    background: var(--panel);
    border-bottom: 1px solid var(--line);
    display: flex;
    align-items: center;
    gap: 18px;
    flex-wrap: nowrap;
}
header h1 {
    flex: 0 1 auto;
    min-width: 9ch;
    font-size: 14px;
    font-weight: 600;
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
header h1 span { color: var(--accent); font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
header .stat { flex: 0 0 auto; color: var(--muted); font-size: 12px; }
#s-meta {
    flex: 0 999 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
header .stat b { color: var(--text); font-weight: 600; }
header .warn { color: var(--warn); }
main { flex: 1 1 auto; display: flex; min-height: 0; }
#tree-pane {
    flex: 0 0 380px;
    min-width: 220px;
    max-width: 70vw;
    background: var(--panel);
    border-right: 1px solid var(--line);
    overflow: auto;
    padding: 6px 0 40px;
}
#splitter { flex: 0 0 5px; cursor: col-resize; background: var(--line); }
#splitter:hover { background: var(--accent); }
#map-pane { flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; }
#crumbs {
    flex: 0 0 auto;
    padding: 7px 12px;
    background: var(--panel-2);
    border-bottom: 1px solid var(--line);
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
    overflow-x: auto;
    white-space: nowrap;
}
#crumbs a { color: var(--accent); cursor: pointer; text-decoration: none; }
#crumbs a:hover { text-decoration: underline; }
#crumbs .sep { color: var(--muted); }
#crumbs .hint { color: var(--muted); margin-left: auto; padding-left: 16px; }
#canvas-wrap { flex: 1 1 auto; position: relative; min-height: 0; }
canvas { display: block; position: absolute; inset: 0; width: 100%; height: 100%; cursor: pointer; }
#ext-pane {
    flex: 0 0 250px;
    min-width: 0;
    background: var(--panel);
    border-left: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    min-height: 0;
}
#ext-head {
    flex: 0 0 auto;
    padding: 7px 12px;
    background: var(--panel-2);
    border-bottom: 1px solid var(--line);
    font-size: 12px;
    display: flex;
    align-items: baseline;
    gap: 8px;
}
#ext-head { white-space: nowrap; }
#ext-head b { font-weight: 600; }
#ext-head .sub { color: var(--muted); font-size: 11px; margin-left: auto; }
#ext-head a { color: var(--accent); cursor: pointer; margin-left: auto; }
#ext-list { flex: 1 1 auto; overflow: auto; padding: 2px 0 30px; }
.ext {
    position: relative;
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 3px 12px 5px;
    cursor: pointer;
    white-space: nowrap;
}
.ext:hover { background: var(--hover); }
.ext.on { background: var(--sel); }
.ext i { flex: 0 0 9px; height: 9px; }
.ext .xn { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; }
.ext .xs { flex: 0 0 auto; color: var(--size); font-variant-numeric: tabular-nums; }
.ext .xc { flex: 0 0 52px; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.ext em {
    position: absolute;
    left: 12px;
    bottom: 1px;
    height: 2px;
    max-width: calc(100% - 24px);
    background: var(--accent);
    opacity: .55;
}
.row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 1px 10px 1px 0;
    cursor: default;
    white-space: nowrap;
}
.row:hover { background: var(--hover); }
.row.sel { background: var(--sel); }
.row .caret {
    flex: 0 0 12px;
    color: var(--muted);
    cursor: pointer;
    text-align: center;
    user-select: none;
}
.row .caret.leaf { visibility: hidden; }
.row i { flex: 0 0 9px; height: 9px; }
.row .nm { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; }
.row.dir .nm { font-weight: 600; }
.row.agg .nm { color: var(--muted); font-style: italic; }
.row .sz { flex: 0 0 auto; color: var(--size); font-variant-numeric: tabular-nums; }
.row .pc { flex: 0 0 46px; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.row .bar { flex: 0 0 40px; height: 6px; background: var(--bar); position: relative; }
.row .bar em { position: absolute; inset: 0 auto 0 0; background: var(--accent); }
#tip {
    position: fixed;
    z-index: 20;
    display: none;
    max-width: 460px;
    padding: 7px 10px;
    background: var(--tip-bg);
    border: 1px solid var(--line);
    box-shadow: 0 6px 22px var(--tip-shadow);
    pointer-events: none;
    font-size: 12px;
}
#tip .p { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; word-break: break-all; }
#tip .m { color: var(--muted); margin-top: 3px; }
.tools { flex: 0 0 auto; margin-left: auto; display: flex; align-items: center; gap: 8px; }
#lang, #theme {
    flex: 0 0 auto;
    white-space: nowrap;
    padding: 3px 9px;
    background: var(--panel-2);
    color: var(--muted);
    border: 1px solid var(--line);
    border-radius: 3px;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
}
#lang:hover, #lang:focus, #theme:hover { color: var(--text); border-color: var(--accent); }
</style>
</head>
<body>
<header>
    <h1>superdirstat <span>/*@@ROOTPATH@@*/</span></h1>
    <div class="stat"><b id="s-size"></b> &middot; <b id="s-files"></b> &middot; <b id="s-dirs"></b></div>
    <div class="stat" id="s-meta"></div>
    <div class="tools">
        <select id="lang" title="Language">
            <option value="en">EN</option>
            <option value="fr">FR</option>
        </select>
        <button id="theme" type="button"></button>
    </div>
</header>
<main>
    <div id="tree-pane"></div>
    <div id="splitter"></div>
    <div id="map-pane">
        <div id="crumbs"></div>
        <div id="canvas-wrap"><canvas id="map"></canvas></div>
    </div>
    <div id="ext-pane">
        <div id="ext-head">
            <b id="ext-title"></b>
            <span class="sub" id="ext-hint"></span>
            <a id="ext-clear" hidden></a>
        </div>
        <div id="ext-list"></div>
    </div>
</main>
<div id="tip"></div>
<script>
var D = JSON.parse(/*@@DATA@@*/);

var PALETTE = ['#e05252','#e0a352','#d4d152','#8fd152','#52d17a','#52d1c4',
               '#52a8e0','#5f66e0','#9152e0','#d152c4','#e0527f','#a37552',
               '#c4d152','#52c4a8','#9c6fb8','#5f8fa8'];
var DIR_COLOR = '#6b7787';
var AGG_COLOR = '#4a5260';

var EXT_OTHER = -1, EXT_DIR = -2, EXT_AGG = -3;
var OTHER_COLOR = '#7a8494';

var extColor = {};
var extId = {};
D.exts.forEach(function (row, i) {
    extColor[row[0]] = i < PALETTE.length ? PALETTE[i] : hashColor(row[0]);
    extId[row[0]] = i;
});

function hashColor(key) {
    var h = 0;
    for (var i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) % 360;
    return 'hsl(' + h + ',42%,55%)';
}

function isDir(n) { return n.length === 4; }
function isAgg(n) { return n.length === 3; }

function extOf(name) {
    var dot = name.lastIndexOf('.');
    if (dot > 0 && dot < name.length - 1 && name.length - dot <= 12) return name.slice(dot).toLowerCase();
    return '';
}

function extLabel(ext) {
    return ext === '' ? t('noExt') : ext;
}

function nameOf(node) {
    if (!isAgg(node)) return node[0];
    return node[0] ? t('aggregate')(node[0]) : t('folderOverhead');
}

function colorOf(n) {
    if (isDir(n)) return DIR_COLOR;
    if (isAgg(n)) return AGG_COLOR;
    var ext = extOf(n[0]);
    return extColor[ext] || hashColor(ext);
}

function typeIdOf(n) {
    if (isDir(n)) return EXT_DIR;
    if (isAgg(n)) return EXT_AGG;
    var id = extId[extOf(n[0])];
    return id === undefined ? EXT_OTHER : id;
}

function typeColorOf(id) {
    if (id === EXT_DIR) return DIR_COLOR;
    if (id === EXT_AGG) return AGG_COLOR;
    if (id === EXT_OTHER) return OTHER_COLOR;
    return extColor[D.exts[id][0]];
}

function rgbOf(css) {
    var c = rgbOf.cache || (rgbOf.cache = {});
    if (c[css]) return c[css];
    var cv = rgbOf.cv || (rgbOf.cv = document.createElement('canvas'));
    cv.width = cv.height = 1;
    var cx = cv.getContext('2d');
    cx.fillStyle = css;
    cx.fillRect(0, 0, 1, 1);
    var d = cx.getImageData(0, 0, 1, 1).data;
    return (c[css] = [d[0], d[1], d[2]]);
}

function tok(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

var lang = 'en';

var I18N = {
    en: {
        locale: 'en-GB',
        units: ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'],
        files: function (n) { return plural(n, 'file'); },
        folders: function (n) { return plural(n, 'folder'); },
        aggregate: function (n) { return '+ ' + plural(n, 'item'); },
        folderOverhead: 'folder overhead',
        ofTotal: 'of total',
        types: 'File types',
        clickToHighlight: 'click to highlight',
        reset: 'reset',
        otherExt: 'other extensions',
        noExt: 'no extension',
        compactFolders: 'compacted folders',
        aggregated: 'aggregated items',
        hint: 'double-click to zoom, Esc to go up',
        generated: 'generated',
        scan: function (s) { return 'scan ' + s + ' s'; },
        sizeMode: function (apparent) { return apparent ? 'apparent size' : 'disk size'; },
        nodes: function (n, t) { return plural(n, 'node') + ' (threshold ' + t + ')'; },
        errors: function (n) { return plural(n, 'access error'); },
        mounts: function (n) { return plural(n, 'virtual mount skipped', 'virtual mounts skipped'); },
        links: function (n) { return plural(n, 'symlink not followed', 'symlinks not followed'); },
        hardlinks: function (n) { return plural(n, 'hardlink deduplicated', 'hardlinks deduplicated'); },
        deep: function (n) { return plural(n, 'folder too deep', 'folders too deep'); },
        themeDark: 'Dark theme',
        themeLight: 'Light theme'
    },
    fr: {
        locale: 'fr-FR',
        units: ['o', 'Kio', 'Mio', 'Gio', 'Tio', 'Pio'],
        files: function (n) { return plural(n, 'fichier'); },
        folders: function (n) { return plural(n, 'dossier'); },
        aggregate: function (n) { return '+ ' + plural(n, 'élément'); },
        folderOverhead: 'occupation du dossier',
        ofTotal: 'du total',
        types: 'Types de fichiers',
        clickToHighlight: 'clic pour surligner',
        reset: 'réinitialiser',
        otherExt: 'autres extensions',
        noExt: 'sans extension',
        compactFolders: 'dossiers compactés',
        aggregated: 'éléments agrégés',
        hint: 'double-clic pour zoomer, Échap pour remonter',
        generated: 'généré le',
        scan: function (s) { return 'scan ' + s + ' s'; },
        sizeMode: function (apparent) { return apparent ? 'taille apparente' : 'taille disque'; },
        nodes: function (n, t) { return plural(n, 'nœud') + ' (seuil ' + t + ')'; },
        errors: function (n) { return plural(n, 'erreur d\'accès', 'erreurs d\'accès'); },
        mounts: function (n) { return plural(n, 'montage virtuel ignoré', 'montages virtuels ignorés'); },
        links: function (n) { return plural(n, 'lien symbolique non suivi', 'liens symboliques non suivis'); },
        hardlinks: function (n) { return plural(n, 'hardlink dédupliqué', 'hardlinks dédupliqués'); },
        deep: function (n) { return plural(n, 'dossier trop profond', 'dossiers trop profonds'); },
        themeDark: 'Thème sombre',
        themeLight: 'Thème clair'
    }
};

function t(key) {
    return I18N[lang][key];
}

function num(value, decimals) {
    return value.toLocaleString(I18N[lang].locale, decimals === undefined ? undefined :
        { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function plural(count, singular, pluralForm) {
    return num(count) + ' ' + (Math.abs(count) < 2 ? singular : (pluralForm || singular + 's'));
}

function fmt(bytes) {
    var u = I18N[lang].units, i = 0, v = bytes;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    if (!i) return num(bytes) + ' ' + u[0];
    return num(v, v < 10 ? 2 : v < 100 ? 1 : 0) + ' ' + u[i];
}

function pct(part, whole) {
    if (!whole) return num(0) + ' %';
    var v = part * 100 / whole;
    return num(v, v < 10 ? 2 : 1) + ' %';
}

function nodeAt(idx) {
    var n = D.root;
    for (var i = 0; i < idx.length; i++) n = n[3][idx[i]];
    return n;
}

function pathOf(idx) {
    var n = D.root, parts = [D.path === '/' ? '' : D.path];
    for (var i = 0; i < idx.length; i++) { n = n[3][idx[i]]; parts.push(nameOf(n)); }
    return parts.join('/');
}

function keyOf(idx) { return idx.join('-'); }

var root = [];
var selected = null;
var rects = [];
var labels = [];
var rectOf = new Map();
var canvas = document.getElementById('map');
var ctx = canvas.getContext('2d');
var base = null;
var dimmed = null;
var dimmedId = null;
var pinnedType = null;
var hoverType = null;
var tip = document.getElementById('tip');
var treePane = document.getElementById('tree-pane');
var extList = document.getElementById('ext-list');
var extClear = document.getElementById('ext-clear');
var extHint = document.getElementById('ext-hint');

/* --- treemap : squarified layout + cushions van Wijk --- */

var H0 = 0.5, FSCALE = 0.75;
var LX = 0.09759, LY = -0.19518, LZ = 0.9759;
var AMBIENT = 0.32, DIFFUSE = 0.92;

function addRidge(s, lin, quad, a, b, h) {
    var d = b - a;
    if (d <= 0) return;
    s[lin] += 4 * h * (b + a) / d;
    s[quad] -= 4 * h / d;
}

function worstRatio(sum, maxv, minv, side, scale) {
    var area = sum * scale;
    if (area <= 0 || side <= 0) return Infinity;
    var t = area / side;
    var lmax = side * (maxv / sum), lmin = side * (minv / sum);
    var a = lmax > 0 ? Math.max(t / lmax, lmax / t) : Infinity;
    var b = lmin > 0 ? Math.max(t / lmin, lmin / t) : Infinity;
    return Math.max(a, b);
}

function squarify(vals, x, y, w, h, out) {
    var start = 0, remaining = 0, i;
    for (i = 0; i < vals.length; i++) remaining += vals[i];
    while (start < vals.length && remaining > 0 && w > 0.5 && h > 0.5) {
        var side = Math.min(w, h);
        var scale = (w * h) / remaining;
        var sum = 0, best = Infinity, end = start;
        while (end < vals.length) {
            var next = sum + vals[end];
            var r = worstRatio(next, vals[start], vals[end], side, scale);
            if (sum > 0 && r > best) break;
            best = r; sum = next; end++;
        }
        if (end === start) end = start + 1, sum = vals[start];
        var thick = (sum * scale) / side;
        var off = 0;
        for (i = start; i < end; i++) {
            var len = side * (vals[i] / sum);
            if (w >= h) out.push([x, y + off, thick, len]);
            else out.push([x + off, y, len, thick]);
            off += len;
        }
        if (w >= h) { x += thick; w -= thick; }
        else { y += thick; h -= thick; }
        remaining -= sum;
        start = end;
    }
    while (start < vals.length) { out.push([x, y, 0, 0]); start++; }
}

function layout(node, x, y, w, h, depth, surface, frame) {
    var kids = node[3], vals = [], keep = [], i;
    for (i = 0; i < kids.length; i++) {
        if (kids[i][1] > 0) { vals.push(kids[i][1]); keep.push(i); }
    }
    if (!vals.length) return;
    var boxes = [];
    squarify(vals, x, y, w, h, boxes);
    var hRidge = H0 * Math.pow(FSCALE, depth);
    for (i = 0; i < boxes.length; i++) {
        var b = boxes[i];
        if (b[2] < 0.6 || b[3] < 0.6) continue;
        var child = kids[keep[i]];
        var s = [surface[0], surface[1], surface[2], surface[3]];
        addRidge(s, 0, 2, b[0], b[0] + b[2], hRidge);
        addRidge(s, 1, 3, b[1], b[1] + b[3], hRidge);
        var childFrame = { p: frame, i: keep[i] };
        var box = { x: b[0], y: b[1], w: b[2], h: b[3], node: child, f: childFrame, s: s,
                    e: typeIdOf(child) };
        rectOf.set(child, box);
        if (isDir(child) && b[2] >= 4 && b[3] >= 4 && child[3].length) {
            box.container = true;
            if (depth <= 1 && b[2] >= 74 && b[3] >= 17) labels.push([b, child[0]]);
            layout(child, b[0], b[1], b[2], b[3], depth + 1, s, childFrame);
        } else {
            rects.push(box);
        }
    }
}

function idxOf(rect) {
    var tail = [], f = rect.f;
    while (f) { tail.push(f.i); f = f.p; }
    tail.reverse();
    return root.concat(tail);
}

function paintCushion(img, rect) {
    var s = rect.s, s0 = s[0], s1 = s[1], s2 = s[2], s3 = s[3];
    var W = img.width, data = img.data;
    var x0 = Math.round(rect.x), y0 = Math.round(rect.y);
    var x1 = Math.min(W, Math.round(rect.x + rect.w));
    var y1 = Math.min(img.height, Math.round(rect.y + rect.h));
    var c = rgbOf(colorOf(rect.node));
    var cr = c[0], cg = c[1], cb = c[2];
    var dnx = -2 * s2;
    for (var py = y0; py < y1; py++) {
        var ny = -(2 * s3 * (py + 0.5) + s1);
        var nyLit = ny * LY + LZ;
        var nySq = ny * ny + 1;
        var nx = -(2 * s2 * (x0 + 0.5) + s0);
        var o = (py * W + x0) * 4;
        for (var ix = x0; ix < x1; ix++) {
            var cosa = (nx * LX + nyLit) / Math.sqrt(nx * nx + nySq);
            var v = cosa > 0 ? AMBIENT + DIFFUSE * cosa : AMBIENT;
            data[o] = cr * v | 0;
            data[o + 1] = cg * v | 0;
            data[o + 2] = cb * v | 0;
            nx += dnx;
            o += 4;
        }
    }
}

function render() {
    var wrap = document.getElementById('canvas-wrap');
    var W = Math.max(1, wrap.clientWidth | 0), H = Math.max(1, wrap.clientHeight | 0);
    canvas.width = W;
    canvas.height = H;
    rects = [];
    labels = [];
    rectOf = new Map();
    var node = nodeAt(root);
    if (isDir(node)) layout(node, 0, 0, W, H, 0, [0, 0, 0, 0]);
    var img = ctx.createImageData(W, H);
    var data = img.data;
    var mb = rgbOf(tok('--map-bg'));
    for (var i = 0; i < data.length; i += 4) {
        data[i] = mb[0]; data[i + 1] = mb[1]; data[i + 2] = mb[2]; data[i + 3] = 255;
    }
    for (i = 0; i < rects.length; i++) paintCushion(img, rects[i]);
    ctx.putImageData(img, 0, 0);
    drawLabels();
    base = ctx.getImageData(0, 0, W, H);
    dimmed = null;
    dimmedId = null;
    repaintOverlay(null);
}

function drawLabels() {
    ctx.font = '600 11px -apple-system, "Segoe UI", Roboto, sans-serif';
    ctx.textBaseline = 'top';
    var placed = [];
    for (var i = 0; i < labels.length; i++) {
        var b = labels[i][0], text = labels[i][1];
        var tw = ctx.measureText(text).width;
        if (tw + 10 > b[2]) continue;
        var box = [b[0] + 3, b[1] + 3, tw + 6, 14];
        var clash = false;
        for (var k = 0; k < placed.length; k++) {
            var p = placed[k];
            if (box[0] < p[0] + p[2] && p[0] < box[0] + box[2] &&
                box[1] < p[1] + p[3] && p[1] < box[1] + box[3]) { clash = true; break; }
        }
        if (clash) continue;
        placed.push(box);
        ctx.fillStyle = 'rgba(0,0,0,.55)';
        ctx.fillRect(box[0], box[1], box[2], box[3]);
        ctx.fillStyle = '#f2f5fa';
        ctx.fillText(text, b[0] + 6, b[1] + 4);
    }
}

function outline(rect, color, width) {
    ctx.lineWidth = width;
    ctx.strokeStyle = color;
    ctx.strokeRect(rect.x + width / 2, rect.y + width / 2, Math.max(1, rect.w - width), Math.max(1, rect.h - width));
    if (width > 1) {
        ctx.lineWidth = 1;
        ctx.strokeStyle = tok('--outline-inner');
        ctx.strokeRect(rect.x + 2.5, rect.y + 2.5, Math.max(1, rect.w - 5), Math.max(1, rect.h - 5));
    }
}

function activeTypeId() {
    return hoverType !== null ? hoverType : pinnedType;
}

function dimmedImage(id) {
    if (dimmed && dimmedId === id && dimmed.width === base.width) return dimmed;
    if (!dimmed || dimmed.width !== base.width || dimmed.height !== base.height) {
        dimmed = ctx.createImageData(base.width, base.height);
    }
    var src = base.data, out = dimmed.data, i;
    var mb = rgbOf(tok('--map-bg'));
    var r0 = mb[0] * 0.8, g0 = mb[1] * 0.8, b0 = mb[2] * 0.8;
    for (i = 0; i < src.length; i += 4) {
        out[i] = r0 + src[i] * 0.2;
        out[i + 1] = g0 + src[i + 1] * 0.2;
        out[i + 2] = b0 + src[i + 2] * 0.2;
        out[i + 3] = 255;
    }
    var W = base.width;
    for (var k = 0; k < rects.length; k++) {
        var r = rects[k];
        if (r.e !== id) continue;
        var x0 = Math.round(r.x), y0 = Math.round(r.y);
        var x1 = Math.min(W, Math.round(r.x + r.w));
        var y1 = Math.min(base.height, Math.round(r.y + r.h));
        for (var py = y0; py < y1; py++) {
            var o = (py * W + x0) * 4;
            for (var px = x0; px < x1; px++) {
                out[o] = src[o];
                out[o + 1] = src[o + 1];
                out[o + 2] = src[o + 2];
                o += 4;
            }
        }
    }
    dimmedId = id;
    return dimmed;
}

function repaintOverlay(hover) {
    if (!base) return;
    var id = activeTypeId();
    ctx.putImageData(id === null ? base : dimmedImage(id), 0, 0);
    if (selected) outline(selected, tok('--outline-sel'), 2);
    if (hover && hover !== selected) {
        outline(hover, tok(hover.container ? '--outline-hover-box' : '--outline-hover'),
                hover.container ? 2 : 1);
    }
}

function hitTest(px, py) {
    for (var i = 0; i < rects.length; i++) {
        var r = rects[i];
        if (px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h) return r;
    }
    return null;
}

/* --- arbre --- */

function rowHtml(node, idx, depth, parentSize) {
    var dir = isDir(node);
    var cls = 'row ' + (dir ? 'dir' : isAgg(node) ? 'agg' : 'file');
    var caret = dir && node[3].length ? '&#9656;' : '';
    return '<div class="' + cls + '" data-idx="' + keyOf(idx) + '" data-depth="' + depth +
        '" style="padding-left:' + (4 + depth * 13) + 'px">' +
        '<span class="caret' + (caret ? '' : ' leaf') + '">' + (caret || '&#9656;') + '</span>' +
        '<i style="background:' + colorOf(node) + '"></i>' +
        '<span class="nm" title="' + esc(nameOf(node)) + '">' + esc(nameOf(node)) + '</span>' +
        '<span class="bar"><em style="width:' + Math.min(100, node[1] * 100 / (parentSize || 1)).toFixed(1) + '%"></em></span>' +
        '<span class="sz">' + fmt(node[1]) + '</span>' +
        '<span class="pc">' + pct(node[1], D.total) + '</span>' +
        '</div>';
}

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function expandRow(rowEl) {
    if (rowEl.dataset.open === '1') return;
    var idx = rowEl.dataset.idx ? rowEl.dataset.idx.split('-').map(Number) : [];
    var node = nodeAt(idx);
    if (!isDir(node) || !node[3].length) return;
    var depth = Number(rowEl.dataset.depth) + 1;
    var html = '';
    for (var i = 0; i < node[3].length; i++) {
        html += rowHtml(node[3][i], idx.concat([i]), depth, node[1]);
    }
    var holder = document.createElement('div');
    holder.className = 'kids';
    holder.innerHTML = html;
    rowEl.parentNode.insertBefore(holder, rowEl.nextSibling);
    rowEl.dataset.open = '1';
    rowEl.querySelector('.caret').innerHTML = '&#9662;';
}

function collapseRow(rowEl) {
    if (rowEl.dataset.open !== '1') return;
    var next = rowEl.nextSibling;
    if (next && next.className === 'kids') next.parentNode.removeChild(next);
    rowEl.dataset.open = '0';
    rowEl.querySelector('.caret').innerHTML = '&#9656;';
}

function buildTree() {
    treePane.innerHTML = rowHtml([D.path, D.root[1], D.root[2], D.root[3]], [], 0, D.root[1]);
    var rootRow = treePane.firstChild;
    expandRow(rootRow);
    var first = treePane.querySelectorAll('.kids > .row.dir');
    for (var i = 0; i < first.length && i < 8; i++) expandRow(first[i]);
}

function rowFor(idx) {
    return treePane.querySelector('[data-idx="' + keyOf(idx) + '"]');
}

function revealInTree(idx) {
    for (var i = 0; i < idx.length; i++) {
        var el = rowFor(idx.slice(0, i));
        if (el) expandRow(el);
    }
    var target = rowFor(idx);
    if (!target) return;
    var prev = treePane.querySelector('.row.sel');
    if (prev) prev.classList.remove('sel');
    target.classList.add('sel');
    var top = target.offsetTop, bottom = top + target.offsetHeight;
    if (top < treePane.scrollTop || bottom > treePane.scrollTop + treePane.clientHeight) {
        treePane.scrollTop = top - treePane.clientHeight / 3;
    }
}

/* --- navigation --- */

function zoomTo(idx) {
    root = idx.slice();
    selected = null;
    drawCrumbs();
    render();
    var el = rowFor(idx);
    if (el) expandRow(el);
}

function drawCrumbs() {
    var el = document.getElementById('crumbs');
    var html = '<a data-z="">' + esc(D.path) + '</a>';
    var acc = [];
    for (var i = 0; i < root.length; i++) {
        acc.push(root[i]);
        html += '<span class="sep">/</span><a data-z="' + keyOf(acc) + '">' + esc(nodeAt(acc)[0]) + '</a>';
    }
    html += '<span class="hint">' + fmt(nodeAt(root)[1]) + ' &middot; ' + t('hint') + '</span>';
    el.innerHTML = html;
}

function extRow(id, label, size, count) {
    var bar = size === null || !D.total ? '' :
        '<em style="width:' + Math.min(100, size * 100 / D.total).toFixed(2) + '%"></em>';
    return '<div class="ext" data-e="' + id + '" title="' + esc(label) + '">' +
        '<i style="background:' + typeColorOf(id) + '"></i>' +
        '<span class="xn">' + esc(label) + '</span>' +
        '<span class="xs">' + (size === null ? '' : fmt(size)) + '</span>' +
        '<span class="xc">' + (count === null ? '' : num(count)) + '</span>' +
        bar + '</div>';
}

function buildExtList() {
    var html = '';
    for (var i = 0; i < D.exts.length; i++) {
        html += extRow(i, extLabel(D.exts[i][0]), D.exts[i][1], D.exts[i][2]);
    }
    if (D.ext_other && D.ext_other[1]) {
        html += extRow(EXT_OTHER, t('otherExt'), D.ext_other[0], D.ext_other[1]);
    }
    html += extRow(EXT_DIR, t('compactFolders'), null, null);
    html += extRow(EXT_AGG, t('aggregated'), null, null);
    extList.innerHTML = html;
}

function syncExtSelection() {
    document.getElementById('ext-title').textContent = t('types');
    extHint.textContent = t('clickToHighlight');
    extClear.textContent = t('reset');
    var rows = extList.children;
    for (var i = 0; i < rows.length; i++) {
        rows[i].classList.toggle('on', Number(rows[i].dataset.e) === pinnedType);
    }
    extClear.hidden = pinnedType === null;
    extHint.hidden = pinnedType !== null;
}

/* --- evenements --- */

canvas.addEventListener('mousemove', function (ev) {
    var r = canvas.getBoundingClientRect();
    var hit = hitTest(ev.clientX - r.left, ev.clientY - r.top);
    if (hit === canvas._hover) { moveTip(ev); return; }
    canvas._hover = hit;
    repaintOverlay(hit);
    if (!hit) { tip.style.display = 'none'; return; }
    tip.innerHTML = '<div class="p">' + esc(pathOf(idxOf(hit))) + '</div>' +
        '<div class="m">' + fmt(hit.node[1]) + ' &middot; ' + pct(hit.node[1], D.total) + ' ' +
        t('ofTotal') + (isDir(hit.node) ? ' &middot; ' + t('files')(hit.node[2]) : '') + '</div>';
    tip.style.display = 'block';
    moveTip(ev);
});

function moveTip(ev) {
    if (tip.style.display !== 'block') return;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
    if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
}

canvas.addEventListener('mouseleave', function () {
    canvas._hover = null;
    tip.style.display = 'none';
    repaintOverlay(null);
});

canvas.addEventListener('click', function (ev) {
    var r = canvas.getBoundingClientRect();
    var hit = hitTest(ev.clientX - r.left, ev.clientY - r.top);
    if (!hit) return;
    selected = hit;
    repaintOverlay(canvas._hover);
    revealInTree(idxOf(hit));
});

canvas.addEventListener('dblclick', function (ev) {
    var r = canvas.getBoundingClientRect();
    var hit = hitTest(ev.clientX - r.left, ev.clientY - r.top);
    if (!hit) return;
    var full = idxOf(hit);
    var idx = isDir(hit.node) ? full : full.slice(0, -1);
    if (idx.length) zoomTo(idx);
});

document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') return;
    if (pinnedType !== null) {
        pinnedType = null;
        syncExtSelection();
        repaintOverlay(canvas._hover);
        return;
    }
    if (root.length) zoomTo(root.slice(0, -1));
});

extList.addEventListener('click', function (ev) {
    var row = ev.target.closest('.ext');
    if (!row) return;
    var id = Number(row.dataset.e);
    pinnedType = pinnedType === id ? null : id;
    syncExtSelection();
    repaintOverlay(canvas._hover);
});

extList.addEventListener('mouseover', function (ev) {
    var row = ev.target.closest('.ext');
    var id = row ? Number(row.dataset.e) : null;
    if (id === hoverType) return;
    hoverType = id;
    repaintOverlay(null);
});

extList.addEventListener('mouseleave', function () {
    if (hoverType === null) return;
    hoverType = null;
    repaintOverlay(canvas._hover);
});

extClear.addEventListener('click', function () {
    pinnedType = null;
    syncExtSelection();
    repaintOverlay(canvas._hover);
});

treePane.addEventListener('mouseover', function (ev) {
    var row = ev.target.closest('.row');
    var rect = row ? rectOf.get(nodeAt(row.dataset.idx ? row.dataset.idx.split('-').map(Number) : [])) : null;
    if (rect === treePane._hover) return;
    treePane._hover = rect || null;
    repaintOverlay(treePane._hover);
});

treePane.addEventListener('mouseleave', function () {
    if (!treePane._hover) return;
    treePane._hover = null;
    repaintOverlay(null);
});

document.getElementById('crumbs').addEventListener('click', function (ev) {
    var a = ev.target.closest('a[data-z]');
    if (!a) return;
    zoomTo(a.dataset.z ? a.dataset.z.split('-').map(Number) : []);
});

treePane.addEventListener('click', function (ev) {
    var row = ev.target.closest('.row');
    if (!row) return;
    var idx = row.dataset.idx ? row.dataset.idx.split('-').map(Number) : [];
    if (ev.target.classList.contains('caret')) {
        row.dataset.open === '1' ? collapseRow(row) : expandRow(row);
        return;
    }
    var prev = treePane.querySelector('.row.sel');
    if (prev) prev.classList.remove('sel');
    row.classList.add('sel');
    var node = nodeAt(idx);
    var rect = rectOf.get(node);
    if (rect) {
        selected = rect;
        repaintOverlay(null);
    } else if (idx.length) {
        zoomTo(isDir(node) ? idx : idx.slice(0, -1));
        selected = rectOf.get(node) || null;
        repaintOverlay(null);
        revealInTree(idx);
    }
});

treePane.addEventListener('dblclick', function (ev) {
    var row = ev.target.closest('.row');
    if (!row || !row.dataset.idx) return;
    var idx = row.dataset.idx.split('-').map(Number);
    if (isDir(nodeAt(idx))) zoomTo(idx);
});

(function () {
    var pane = document.getElementById('tree-pane'), split = document.getElementById('splitter');
    var dragging = false;
    split.addEventListener('mousedown', function (ev) { dragging = true; ev.preventDefault(); });
    document.addEventListener('mousemove', function (ev) {
        if (!dragging) return;
        pane.style.flexBasis = Math.max(220, Math.min(innerWidth * 0.7, ev.clientX)) + 'px';
    });
    document.addEventListener('mouseup', function () {
        if (!dragging) return;
        dragging = false;
        render();
    });
})();

var resizeTimer = null;
addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 120);
});

var themeBtn = document.getElementById('theme');
var langSel = document.getElementById('lang');
var metaEl = document.getElementById('s-meta');

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function applyTheme(name) {
    if (name === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    themeBtn.textContent = name === 'dark' ? t('themeLight') : t('themeDark');
    themeBtn.setAttribute('aria-label', themeBtn.textContent);
}

themeBtn.addEventListener('click', function () {
    var next = currentTheme() === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try {
        localStorage.setItem('superdirstat-theme', next);
    } catch (e) {}
    render();
});

function drawStats() {
    document.getElementById('s-size').textContent = fmt(D.total);
    document.getElementById('s-files').textContent = t('files')(D.files);
    document.getElementById('s-dirs').textContent = t('folders')(D.dirs);
}

function drawMeta() {
    var m = D.meta, bits = [];
    var stamp = new Date(m.generated);
    bits.push(esc(t('generated') + ' ' +
        (isNaN(stamp.getTime()) ? m.generated
            : stamp.toLocaleString(I18N[lang].locale, { dateStyle: 'short', timeStyle: 'short' }))));
    bits.push(esc(t('scan')(num(m.scan_seconds, 1))));
    bits.push(esc(t('sizeMode')(m.apparent)));
    bits.push(esc(t('nodes')(m.nodes, fmt(m.threshold))));
    if (m.errors) bits.push('<span class="warn">' + esc(t('errors')(m.errors)) + '</span>');
    if (m.skipped_mounts.length) {
        bits.push('<span title="' + esc(m.skipped_mounts.join(' ')) + '">' +
            esc(t('mounts')(m.skipped_mounts.length)) + '</span>');
    }
    if (m.symlinks) bits.push(esc(t('links')(m.symlinks)));
    if (m.dedup_hardlinks) bits.push(esc(t('hardlinks')(m.dedup_hardlinks)));
    if (m.too_deep) bits.push('<span class="warn">' + esc(t('deep')(m.too_deep)) + '</span>');
    metaEl.innerHTML = bits.join(' &middot; ');
    metaEl.title = metaEl.textContent;
}

function refreshTreeLabels() {
    var rows = treePane.querySelectorAll('.row');
    for (var i = 0; i < rows.length; i++) {
        var idx = rows[i].dataset.idx ? rows[i].dataset.idx.split('-').map(Number) : [];
        var node = idx.length ? nodeAt(idx) : D.root;
        var label = idx.length ? nameOf(node) : D.path;
        var nm = rows[i].querySelector('.nm');
        nm.textContent = label;
        nm.title = label;
        rows[i].querySelector('.sz').textContent = fmt(node[1]);
        rows[i].querySelector('.pc').textContent = pct(node[1], D.total);
    }
}

function setLang(next) {
    lang = I18N[next] ? next : 'en';
    document.documentElement.lang = lang;
    langSel.value = lang;
    applyTheme(currentTheme());
    drawStats();
    drawMeta();
    drawCrumbs();
    refreshTreeLabels();
    buildExtList();
    syncExtSelection();
    repaintOverlay(null);
}

langSel.addEventListener('change', function () {
    setLang(langSel.value);
    try {
        localStorage.setItem('superdirstat-lang', langSel.value);
    } catch (e) {}
});

var startLang = null;
try {
    startLang = localStorage.getItem('superdirstat-lang');
} catch (e) {}
if (!I18N[startLang]) {
    startLang = (navigator.language || 'en').toLowerCase().indexOf('fr') === 0 ? 'fr' : 'en';
}

buildTree();
setLang(startLang);
render();
</script>
</body>
</html>
"""


def build_report(payload, root_path):
    data = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    # The payload is handed to the browser as a JS string for JSON.parse(), not as an
    # array literal. V8 parses a nested literal recursively and dies with "Maximum call
    # stack size exceeded" past ~1400 levels of nesting; JSON.parse() clears 20000, well
    # past MAX_DEPTH. The ceiling matters because it moves with the available stack: a
    # deep tree would otherwise produce a report that opens in one browser and not the
    # next. This costs about 6 ms on a 1 MiB payload — the literal is the faster of the
    # two here, roughly 4 ms against 10 ms, since a tree of arrays is exactly what V8
    # parses well. Six milliseconds against "the report does not open" is a trade worth
    # making, but it is a trade, not a free win.
    #
    # Escaping order matters below: each step must not corrupt the previous one.
    # Backslashes first, so the escapes introduced afterwards survive as escapes.
    data = data.replace('\\', '\\\\').replace("'", "\\'")
    # Every `<` becomes the six characters \u003c, which JS turns back into `<` inside
    # the string, so JSON.parse() still sees the original text. A `<` can only occur
    # inside a JSON string, and this neutralises `</script>` as well as the `<!--` +
    # `<script` pair that switches the HTML parser into "script data double escaped",
    # where the closing `</script>` no longer ends the block.
    # U+2028/2029 are legal in JSON but terminate a line in a JS string literal.
    data = data.replace('<', '\\u003c')
    data = data.replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    data = "'" + data + "'"
    # The path comes from the command line but can hold anything: a directory may
    # legitimately be named `<script>`.
    safe_path = escape(root_path, quote=True)
    html = TEMPLATE.replace('/*@@DATA@@*/', data)
    html = html.replace('/*@@ROOTPATH@@*/', safe_path)
    html = html.replace('/*@@TITLE@@*/', safe_path)
    return html


def main():
    parser = argparse.ArgumentParser(
        prog='superdirstat',
        description='Write a self-contained HTML disk usage report (treemap + file tree).')
    parser.add_argument('path', help='directory to scan')
    parser.add_argument('output', nargs='?', help='output HTML file (default: <directory>-dirstat.html)')
    parser.add_argument('--apparent', action='store_true',
                        help='use apparent size (st_size) instead of real disk usage')
    parser.add_argument('-x', '--one-file-system', action='store_true',
                        help='do not cross mount points')
    parser.add_argument('-e', '--exclude', action='append', default=[], metavar='GLOB',
                        help='skip entries matching this glob, tested on both name and full path (repeatable)')
    parser.add_argument('--min-share', type=float, default=0.000002, metavar='RATIO',
                        help='aggregation threshold as a fraction of the total: 2e-6 is about two pixels '
                             'on a one-megapixel screen (default 2e-6)')
    parser.add_argument('--max-nodes', type=int, default=80000, metavar='N',
                        help='maximum number of nodes in the report. This is what really governs the file '
                             'size: the threshold is raised until the tree fits (default 80000)')
    parser.add_argument('--min-file', type=int, default=0, metavar='BYTES',
                        help='aggregate files below this size during the scan, saving memory on trees of '
                             'several million files')
    parser.add_argument('--include-pseudo', action='store_true',
                        help='include virtual filesystems (/proc, /sys, tmpfs...), skipped by default '
                             'because they occupy no disk space')
    parser.add_argument('-q', '--quiet', dest='progress', action='store_false',
                        help='do not print progress')
    opts = parser.parse_args()

    target = os.path.abspath(os.path.expanduser(opts.path))
    if not os.path.isdir(target):
        parser.error('%s is not a directory' % target)

    if opts.output:
        out_path = opts.output
    else:
        base = os.path.basename(target.rstrip('/')) or 'racine'
        out_path = '%s-dirstat.html' % base

    # Three things recurse once per directory level, and the deepest wins: scan(),
    # prune(), and json.dumps() — which spends two levels per node, since a folder is
    # a list holding a list of children. Hence the multiplier rather than MAX_DEPTH
    # plus a margin. A limit set far beyond what the walk can reach is not free: on
    # CPython < 3.11 every Python frame costs C stack too, and exhausting that is a
    # segfault instead of a RecursionError.
    sys.setrecursionlimit(MAX_DEPTH * 3 + 1000)

    scanner = Scanner(opts)
    scanner.root_dev = os.stat(target).st_dev
    started = time.time()
    if opts.progress:
        sys.stderr.write('Scanning %s\n' % target)
    tree = scanner.scan(target, target)
    elapsed = time.time() - started
    if opts.progress:
        sys.stderr.write('\r' + ' ' * 78 + '\r')
        sys.stderr.write('Scanned in %.1f s: %s, %s, %s\n' % (
            elapsed, plural(scanner.files, 'file'), plural(scanner.dirs, 'folder'),
            human(scanner.total_size)))

    total = tree[1]
    pruned, node_count, threshold = prune_adaptive(tree, total, opts.min_share, opts.max_nodes)

    ranked = sorted(scanner.ext_stats.items(), key=lambda kv: -kv[1][0])
    exts = ranked[:EXT_LIST_SIZE]
    other_size = sum(stats[0] for _, stats in ranked[EXT_LIST_SIZE:])
    other_count = sum(stats[1] for _, stats in ranked[EXT_LIST_SIZE:])
    payload = {
        'path': target,
        'root': pruned,
        'total': total,
        'files': scanner.files,
        'dirs': scanner.dirs,
        'exts': [[name, stats[0], stats[1]] for name, stats in exts],
        # Everything past the top N, grouped: without this row, files with a rare
        # extension are coloured in the treemap but absent from the side panel, and
        # so cannot be highlighted.
        'ext_other': [other_size, other_count],
        # Structured, not pre-rendered HTML: the report composes this line itself so
        # the language selector can rewrite it.
        'meta': {
            'version': VERSION,
            'generated': datetime.now().astimezone().isoformat(timespec='seconds'),
            'scan_seconds': round(elapsed, 1),
            'apparent': bool(opts.apparent),
            'nodes': node_count,
            'threshold': threshold,
            'errors': scanner.errors,
            'skipped_mounts': sorted(scanner.skipped_mounts),
            'symlinks': scanner.symlinks,
            'dedup_hardlinks': scanner.dedup_hardlinks,
            'too_deep': scanner.too_deep,
        },
    }

    html = build_report(payload, target)
    with open(out_path, 'w', encoding='utf-8') as handle:
        handle.write(html)

    size = os.path.getsize(out_path)
    if opts.progress:
        sys.stderr.write('Report: %s (%s, %s)\n' % (
            os.path.abspath(out_path), human(size), plural(node_count, 'node')))


if __name__ == '__main__':
    main()
