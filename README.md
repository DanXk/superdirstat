# superdirstat

WinDirStat for Linux, as a single file that writes a single file.

```sh
./superdirstat.py /usr report.html
```

You get one self-contained HTML report — a squarified treemap with cushion shading next
to a synchronised file tree — that you can open with `file://`, keep, or email. No
server, no build step, no dependencies.

![superdirstat report of /usr](docs/screenshot-light.jpg)

## Why this exists

`ncdu`, `gdu` and `dust` are excellent, and they live in your terminal. `filelight` and
`qdirstat` draw the map you want, and they need a desktop session. Neither gives you an
**artefact**: a file you can produce over SSH on a headless box, open a week later, or
send to someone who will not install anything.

That is the whole point of this tool. The Python side only walks the tree; the browser
does the drawing.

## Install

Requires Python 3.7+ (developed and tested on 3.9). Nothing else — the standard library
does the scan.

```sh
curl -O https://raw.githubusercontent.com/DanXk/superdirstat/main/superdirstat.py
chmod +x superdirstat.py
./superdirstat.py ~/Downloads report.html
```

Or, for a `superdirstat` command on your PATH:

```sh
pipx install git+https://github.com/DanXk/superdirstat.git
```

## Usage

```
superdirstat [options] path [output]
```

| Option | What it does |
| --- | --- |
| `--apparent` | Use apparent size (`st_size`) instead of real disk usage |
| `-x`, `--one-file-system` | Do not cross mount points |
| `-e GLOB`, `--exclude GLOB` | Skip entries matching this glob, tested on both name and full path (repeatable) |
| `--max-nodes N` | Maximum nodes in the report (default 80000) — this is what governs the file size |
| `--min-share RATIO` | Aggregation threshold as a fraction of the total (default 2e-6, about two pixels) |
| `--min-file BYTES` | Aggregate files below this size during the scan, to save memory on huge trees |
| `--include-pseudo` | Include `/proc`, `/sys`, `tmpfs`… (skipped by default) |
| `-q`, `--quiet` | No progress output |

Scanning `/` is a supported, ordinary case:

```sh
sudo ./superdirstat.py / root.html
# Scanned in 13.1 s: 1,258,492 files, 229,951 folders, 87.4 GiB
# Report: root.html (1.96 MiB, 70,075 nodes)
```

## Reading the report

- **Treemap** — one rectangle per file, area proportional to size, colour by extension.
  Cushion shading (van Wijk) is what makes the nesting readable rather than a flat
  mosaic. Hover for the full path, click to select, double-click a folder to zoom,
  `Esc` to go back up.
- **File tree** — sizes, share of the parent, share of the total. Hovering a row
  outlines the matching rectangles; hovering a *folder* outlines its whole region.
- **File types panel** — every extension with its size and file count. Click one and
  the rest of the map fades out, leaving only the squares of that type. `Esc` clears it.
- **Theme and language** — light or dark, English or French, both remembered. The
  language selector rewrites the interface only; nothing is re-scanned.

![the same report, dark theme](docs/screenshot-dark.jpg)

## Things worth knowing

**Sizes are disk usage, not file length.** `st_blocks * 512`, like WinDirStat, so a
sparse or tiny file costs what it really costs. `--apparent` switches to `st_size`.

**A folder's own blocks are shown.** A directory entry occupies disk too, and that space
belongs to no child. It appears as a *folder overhead* leaf, because leaving it out lets
children share an area larger than their real sum — worth up to 17 % of distortion on
deep, mostly-empty trees.

**The tree is pruned, and the report says so.** Everything too small to be visible is
folded into a `+ N items` leaf; the threshold rises automatically until the node count
fits under `--max-nodes`. The header states the resulting threshold. Without this, a
scan of a few million files produces a payload no browser will open.

**Virtual filesystems are skipped.** `/proc`, `/sys`, `tmpfs` and friends occupy no
disk. Their mount points are read from `/proc/self/mountinfo` rather than hard-coded,
because the real list depends on the machine — containers, cgroup v1 vs v2, bind mounts.

**Symlinks are never followed; hardlinks are counted once.** A symlink's own blocks are
counted where the link sits, as `du` does — what is not done is walking through it. Both
counts appear in the header, so a total that differs from another tool is explainable
rather than mysterious.

**Depth is capped at 2048 levels.** No real path goes that deep — PATH_MAX runs out
first, at roughly 2000 levels of single-letter directory names — so this only ever fires
on a tree something has mangled. When it does, the folders left out are counted in the
header instead of taking the scan down with them.

## Performance

On a busy server, cold cache excluded:

| Tree | Scan | Report | Render in browser |
| --- | --- | --- | --- |
| 221k files, 9 GiB | 2.1 s | 1.9 MiB | 37 ms |
| 1.26M files, 87 GiB | 13.1 s | 2.0 MiB | 40 ms |

Memory during the scan is roughly 400 MB at two million files, since the whole tree is
held before pruning. `--min-file 65536` aggregates during the walk if you need to go
further.

## Tests

The report's JavaScript can be checked without a browser:

```sh
./superdirstat.py /usr /tmp/r.html
node tests/geometry.js  /tmp/r.html    # exact tiling, proportional areas, hit-testing
node tests/highlight.js /tmp/r.html    # the type filter, pixel by pixel
node tests/bench.js     /tmp/r.html    # layout / paint / highlight timings
```

See [tests/README.md](tests/README.md) — in particular the reason those tests compile
the report with `vm.runInThisContext` and never `eval()`.

## Limitations

- **Linux only.** `st_blocks` and `/proc/self/mountinfo` are the two hard dependencies.
  macOS would need a different mount enumeration; `--apparent` works anywhere.
- **Read-only by design.** No delete, no move. A tool that draws your disk and a tool
  that empties it should not be the same binary.
- **A report is a snapshot.** There is no watching, no diffing between two reports yet.

## License

MIT — see [LICENSE](LICENSE).

The cushion shading comes from Jarke van Wijk and Huub van de Wetering's *Cushion
Treemaps* (1999), by way of WinDirStat and KDirStat. The implementation here is
independent.
