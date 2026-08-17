# Tests

The interesting half of superdirstat runs in a browser, so these tests load a generated
report's JavaScript into Node with a stubbed DOM and check the parts that are easy to
break and hard to see.

```sh
../superdirstat.py /usr /tmp/r.html
node geometry.js  /tmp/r.html
node highlight.js /tmp/r.html
node bench.js     /tmp/r.html
```

Requires Node 14+. Nothing to install.

| File | What it checks |
| --- | --- |
| `harness.js` | Shared loader: DOM stubs, and the probe that exposes the report's internals |
| `geometry.js` | The treemap tiles the canvas exactly, areas are proportional, hit-testing agrees with the layout, zoom rebuilds a valid layout |
| `highlight.js` | The type filter keeps matching pixels byte for byte and fades the rest towards the map background, in both themes and after a zoom |
| `bench.js` | Timings for layout, the per-pixel cushion loop, a full render, and a highlight |

## Two things to know before changing the harness

**It compiles the report with `vm.runInThisContext`, never `eval()`.** V8 does not
optimise the body of a direct `eval()` the way it optimises script code. An early version
of this harness used `eval()` and reported a 423 ms render for what actually takes 37 ms
— consistently, reproducibly, and wrongly. Two "optimisations" were made on the strength
of that number before the harness itself turned out to be the problem. If you change how
the report is loaded, re-check a known timing before trusting anything it prints.

**`rgbOf()` is replaced with a stub.** The real one converts a CSS colour through a 1×1
canvas, which does not exist here. Colours in these tests are therefore fake but
self-consistent — enough for geometry and for the masking arithmetic, which is all these
tests assert on. It also means `highlight.js` derives its expected fade from the same
stub, so the check stays valid whatever the palette does.

## What is not covered

Real rendering, real fonts, real event dispatch. Those were verified with headless
Chromium during development — dispatching genuine `MouseEvent`s at tree rows and panel
rows, then reading pixels back — but that needs a browser on the machine and is not
wired into this directory.
