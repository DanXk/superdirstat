# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-08-17

### Added

- Right-clicking a rectangle or a tree row opens a context menu that copies shell
  commands: the path, `cd` and `ls -la` on the containing folder, and — on a file only —
  `rm -i --` on it. Folders instead offer a rescan of that subtree and a `find` of their
  largest files, which is what the treemap leaves you wanting once a region has been
  pruned. Each entry shows the command it will copy.
- Paths are POSIX single-quoted, so a name holding a quote, a space or a newline yields a
  command that resolves to exactly one target. `tests/context.js` asserts this against a
  real shell for every node in a report.

### Notes

- The report still executes nothing: the menu writes to the clipboard, and running the
  command remains a deliberate act. Deletion is offered on files only — a recursive
  delete composed by the tool and one paste from running is not a service.

## [1.0.0] - 2026-08-17

First public release.

### Scanning

- Recursive `os.scandir` walk with no dependencies: 221k files in 2.1 s, 1.26M files in
  13 s.
- Disk usage (`st_blocks * 512`) by default, apparent size with `--apparent`.
- Symlinks are never followed; their own blocks are counted, as `du` does, and the count
  is reported. Hardlinks are counted once, and deduplications are reported too.
- Directory depth is capped at 2048 levels, which is past what PATH_MAX allows for real
  paths. Folders beyond the cap are counted and reported in the header rather than
  aborting the scan.
- Virtual filesystems (`/proc`, `/sys`, `tmpfs`…) are skipped, with mount points read
  from `/proc/self/mountinfo` instead of a hard-coded list. `--include-pseudo` disables
  this. Scanning `/` is an ordinary case rather than a trap.
- `--exclude` globs are matched against both the entry name and the full path.
- Adaptive pruning: nodes too small to be visible are folded into `+ N items`, and the
  threshold rises automatically until the tree fits under `--max-nodes`. A 87 GiB, 1.26M
  file scan produces a 2 MiB report.
- A directory's own blocks are materialised as a *folder overhead* leaf, so rectangle
  areas stay exactly proportional to the sizes they represent.

### Report

- Squarified treemap with van Wijk cushion shading, rendered on a canvas: ~37 ms for
  37,000 rectangles.
- File tree synchronised with the map in both directions. Hovering a folder outlines its
  whole region; hovering a file outlines its rectangle.
- File types panel listing every extension with size and count. Clicking one fades the
  rest of the map out; `Esc` clears the filter.
- Zoom by double-click, breadcrumb navigation, `Esc` to go up.
- Light theme by default, dark theme on a toggle, choice remembered.
- English and French interface, switchable in the report with no re-scan. The scan
  emits data only — counts and structured metadata — never rendered labels.
- Everything is inlined: no external asset, no network access, works over `file://`.
- The scan data is embedded as a string parsed with `JSON.parse`, not as an array
  literal: V8's parser is recursive and gives up past roughly 1400 levels of nesting, at
  a depth that varies with the available stack.

[1.1.0]: https://github.com/DanXk/superdirstat/releases/tag/v1.1.0
[1.0.0]: https://github.com/DanXk/superdirstat/releases/tag/v1.0.0
