# Fast telephoto plate solving (offline research)

2026-11-08, Skyshot evaluation added 2026-11-09. Research and offline benchmarks only. Nothing here talked to the SeeStar, and no code changes are included.

**Conclusion:** the 40–55 s solve-to-solve time is mostly **our use of the `astrometry` package**, not astrometry.net itself.
- `Solver.solve()` does not stop at the first match. With the default `SolutionParameters` it keeps searching every star slice in every index, and it runs a SIP tune-up (about 0.4 s) for each of the 9–90 matches it finds. That makes **25–35 s per solve**.
- The first match arrives after **0.4 s**. With an early stop and no SIP, the same solve takes **0.01–0.09 s**.
- With that fixed, a **2 s** solve exposure and the current download, centring should cost about **7 s per solve and 11 s solve-to-solve**, including the sync and the fine move.

**Recommendation** (updated 2026-11-09 after the [Skyshot evaluation](#skyshot-evaluation-2026-11-09)):
- **Primary: ASTAP CLI with the D05 database.**
  - It runs as a subprocess on the raw FITS, so our code doesn't link to it (MPL-2.0).
  - It solves in **0.12–0.37 s** including FITS read and star detection, and also solves **blind** in 0.16–0.37 s.
  - It is mature, ships prebuilt CLIs for amd64, armhf and aarch64, and its database is 107 MB.
- **Fallback: Skyshot CLI with the Full learned index**, replacing the `astrometry` path.
  - It solved all 174 test runs, blind or hinted, in **30–80 ms warm and 50–120 ms cold** including FITS read and detection. Position agrees with astrometry.net and ASTAP to **≤ 2.5″**, rotation to **0.06°**.
  - Apache-2.0 code, run as a subprocess. It returns the full WCS with rotation and parity.
  - It is not primary yet because it is three weeks old: one author, no tagged release, no Linux runtime qualification by the author, an 843 MB index whose data terms are "non-commercial intended use" with incomplete provenance, and a source build (Rust + C linker).
  - Its speed advantage over ASTAP is 0.1–0.3 s per solve, about 2 % of an 11 s centring cycle. Revisit the order once it has a tagged release.
- **The `astrometry` path** becomes an optional legacy extra (GPL-3.0, in-process). Note that `rig.py` reports `Match.center_ra_deg/center_dec_deg`, which is roughly the centre of the detected stars' extent, not of the frame: **1–6′ off** on these frames (see [Skyshot evaluation](#accuracy)).
- cedar-solve was fastest at 1–100 ms, but it needs a self-built database and its PyPI release is broken on numpy 2. Skyshot now covers the same ground with a downloadable index.

## Where the time goes now

### On the sky (sequencer log, 2026-11-08)

Times run from the log's `at … tracking on` line to the `solve N:` line, for 11 centring solves:

| | M31 (4 solves) | M45 (7 solves, 3 runs) |
|---|---|---|
| expose + download + extract + solve | 44.2–48.7 s | 33.5–42.3 s |
| sync | 0.6–1.0 s | 0.6–0.9 s |
| fine move after sync | 3–4 s (16 s for the first, 2° correction) | 3–4 s (10 s for the first) |
| **solve-to-solve** | **48–50 s** | **38–42 s** |

The download can't be timed directly from the log. It is bounded by the light frames:
- A 60 s light takes **66.5–68 s** from one log line to the next.
- Of that, our own post-processing takes about 1.5 s on the benchmark PC (table below), so about 2.5–3 s on the 4-core Haswell site server.
- That leaves **about 3.5–5 s** for `ImageReady` polling plus the 16 MB ImageArray download.

Split of a typical M31 solve (about 46 s):

| stage | time | source |
|---|---|---|
| exposure (`solve_exposure`) | 10 s | plan |
| `ImageReady` polling (0.5 s poll) | ≤ 0.5 s | code |
| ImageArray download + `np.array` | ~4 s | light-frame bound above |
| green superpixels + `stars(n=120)` + preview JPEG | 0.2 s (PC), ~0.4 s (server) | benchmark |
| **astrometry solve** | **~30 s** | remainder; reproduced offline: 31–38 s |

### Offline benchmark (same frames)

Run on a Ryzen 5 7600X with the saved 60 s lights (M31 ×3, M45 ×3) and one real 10 s M31 frame. The setup matches production: `astrometry` 4.3.0, indexes 4110–4112 (scales 10–12), 120 stars, position hint 6°, hint off target by 1.5°/1.0°.

| configuration | M31 60 s | M45 60 s | M31 10 s (real) |
|---|---|---|---|
| **current** (`SolutionParameters()` defaults) | **31.4 s** (73 matches) | **6.4 s** (9 matches) | **38.2 s** (90 matches) |
| stop at first match (logodds > 40) | 0.40 s | 0.47 s | 0.41 s |
| stop at first match, no tune-up/SIP | **0.01 s** | **0.09 s** | **0.01 s** |
| same, only 40 stars | 0.01 s | fail | 0.01 s |
| ASTAP D05, hint 6°, `-z 2` (whole CLI run incl. FITS read + detection) | 0.25 s | 0.24 s | 0.15 s |
| ASTAP D05, hint off by 2.5°, `-r 6` | 0.27 s | 0.23 s | 0.14 s |
| ASTAP D05, blind (`-r 180`) | 0.34 s | 0.38 s | 0.23 s |
| ASTAP D20 instead of D05 | same | same | same |
| cedar-solve 0.5.1 `solve_from_centroids`, `fov_estimate=2.2`, 120 / 30 stars (partial-sky Tycho DB, see below) | 3.1 / 1.3 ms | 95 / 50 ms | 2.3 / 1.2 ms |
| cedar-solve, no FOV estimate (blind) | 2.5 ms | 111 ms | 2.2 ms |

Other measurements:
- **cedar-solve database:** built from Tycho (`tyc_main`, 355 MB) with `max_fov=4.2, min_fov=2.0, star_max_magnitude=11`, limited to RA 0–70°, Dec 15–55° to keep the test short.
  - That took 61 s and gave a 14 MB file covering about 5 % of the sky. By area, a full-sky build would take about 20 min and produce about 250 MB (an estimate, not built).
  - A full-sky database also has more candidate patterns, so its blind solves will be somewhat slower than the partial one's.
  - tetra3 measures FOV along the image **width**: `fov_estimate=3.97` (the long axis) failed every solve after the 5 s timeout. Use 2.2.
- **Solution agreement:** all solutions agree within about 0.002° of each other, so the early stop doesn't change the result.
- **Index loading** is **not** a cost: the solver is created once (`AstrometrySolver._get` caches it) and `astrometry.Solver(...)` loads the three index files in 0.09 s.
- **Measured scale:** 7.34″ per green superpixel, which is **3.67″/px** at full resolution (config: 3.74). Use 3.67 ±10 % as the size hint.

Light-frame post-processing on the benchmark PC, for reference:

| stage | time |
|---|---|
| green + stars + QA | 0.17 s |
| `shift` (phase correlation) | 0.57 s |
| stack add | 0.37 s |
| stack save (FITS + JPEG) | 0.33 s |
| preview | 0.05 s |
| save FITS | 0.01 s |

Converting the download into an array is not a bottleneck either way:
- From alpyca's ImageBytes nested `array.array` rows, `np.array(...)` takes 0.01 s.
- If the server falls back to JSON, `json.loads` + `np.array` of the 50 MB body costs about 1 s. The JSON body is also 3× the bytes on the wire.

### Shorter exposures (simulated)

Method: each frame was scaled to a shorter exposure, then Gaussian noise was added so the background noise matches a shot-noise-limited shorter frame. Read noise was ignored, so the short-exposure counts are optimistic.

Stars found by `imaging.stars` (15σ, capped at 120). Every case solved with both astrometry (early stop) and ASTAP.

| exposure | M31 (from 60 s) | M45 (from 60 s) | M31 (from real 10 s) |
|---|---|---|---|
| 10 s | 120 | 120 | 120 (real frame) |
| 5 s | 120 | 120 | 120 |
| 2 s | 120 | 120 | 97 |
| 1 s | 104 | 120 | 78 |
| 0.5 s | 76 | 109 | 43 |

- **2 s at gain 80** keeps about 100 stars, which is plenty for either solver.
- **1 s** should also work. A higher gain (for example 200) lowers read noise and protects the short exposures through haze or moonlight.

## Pipeline speedups, ranked

1. **Stop the astrometry search at the first good match** (saves 6–35 s per solve).
   - Pass `logodds_callback=lambda l: astrometry.Action.STOP if max(l) > 40 else astrometry.Action.CONTINUE`.
   - Also pass `sip_order=0, tune_up_logodds_threshold=None` for centring, since SIP is useless for a centre position.
   - This is a few lines in `AstrometrySolver.solve`.
2. **`solve_exposure` 10 s → 2 s** (saves 8 s). Consider a higher gain for solve frames only.
3. **Poll `ImageReady` every 0.1 s** instead of 0.5 s (saves ≤ 0.4 s per frame, for lights too).
4. **Download** (~4 s): the largest remaining cost, and it needs a hardware test.
   - alpyca 3.1.3 already asks for `application/imagebytes`, but whether the SeeStar's Alpaca server answers with ImageBytes is unknown. Check with `Camera.ImageArrayRaw`, which raises if the server answers in JSON.
   - Check `CanSubframe` too: a centred 2160×2160 subframe (`StartX/StartY/NumX/NumY`) would roughly halve the bytes. Reset the subframe before lights.
   - The site LAN path (wired vs WiFi to the scope) matters more than anything in software.
5. **Skip the preview during centring**, or write it in a thread: 0.05–0.1 s. This is small and optional; the status page can keep it.
6. **Fewer stars:** with the early stop, 120 stars cost nothing extra. 40 stars **failed** on M45, so keep 60–120. Software binning is already done (green superpixels), so ASTAP `-z 2` on the raw frame is the equivalent.
7. **Tighter hints:** a 6° hint radius is fine. Neither solver got faster with a smaller radius, and ASTAP's blind solve is only 0.1 s slower.

## Predicted centring time

| stage | now | after 1–3 | after 1–4 (if subframe/ImageBytes help) |
|---|---|---|---|
| exposure | 10 s | 2 s | 2 s |
| polling | ≤ 0.5 s | ≤ 0.1 s | ≤ 0.1 s |
| download | ~4 s | ~4 s | ~2 s |
| extract + preview | 0.4 s | 0.4 s | 0.3 s |
| solve | 6–35 s | ≤ 0.3 s | ≤ 0.3 s |
| sync + fine move | 4–5 s | 4–5 s | 4–5 s |
| **solve-to-solve** | **40–55 s** | **≈ 11 s** | **≈ 9 s** |

A 3-iteration centring would drop from about 2.5 min to about 35 s. The first solve after a goto would take about 7 s instead of about 45 s.

## Skyshot evaluation (2026-11-09)

**Verdict:** technically the best solver tested. It is blind, the fastest of the subprocess options and as accurate as ASTAP, and it returns rotation. Use it as the **fallback** behind ASTAP for now, because of project age, index size and data terms (below).

### What it is

- **Repository:** [ashtree74/skyshot-solver](https://github.com/ashtree74/skyshot-solver), commit `76f0181` (2026-10-18). Package `skyshot-solver` 0.1.0, Rust edition 2024, `publish = false` (not on crates.io).
- **Licence:** verified **Apache-2.0** in `LICENSE`, `Cargo.toml` and GitHub's licence detection. The research PDFs in `research/` are MIT. The **index data is not covered** by either (see Licences below).
- **Maintainer:** one author, Adam Jesionkiewicz (Warsaw), who wrote all 12 commits.
- **Activity:** created 2026-10-17, last push 2026-10-18. There are 22 stars and 2 forks, no tags and no releases. The one open issue (a FITS uint64 BZERO bug, 2026-11-03) has no reply. The README calls it a "development snapshot" that is "not a published binary release".
- **Docs:** a README, `docs/` (CLI contract, data install, provenance, architecture, a verification record), two papers ([method](https://github.com/ashtree74/skyshot-solver/blob/main/research/skyshot-method.pdf), [LGQ-GD learned key](https://github.com/ashtree74/skyshot-solver/blob/main/research/skyshot-learned-key.pdf)), a [web demo](https://skyshot.app/app) and the [AstroBin thread](https://app.astrobin.com/forum/topic/259285).
- **Algorithm:** **blind** (no position, scale, rotation or parity needed). Optional hints are used as follows:
  - Each star gets a "dominance distance" (to its nearest brighter neighbour), and nested thresholds pick the same prominent stars at several densities.
  - Local neighbourhoods are hashed into 32-bit keys, either handcrafted or a small learned encoder (LGQ-GD).
  - Each catalogue hit proposes position, scale, rotation and parity. Hits from independent anchors vote, then the candidates are verified statistically, and a WCS (TAN, optionally SIP) is fitted.
  - Indexes are memory-mapped.
  - Hints come **only from FITS headers** (`RA/DEC` in degrees, `OBJCTRA/OBJCTDEC` in hours, CRVAL, `PIXSCALE` or `FOCALLEN` + `XPIXSZ`), and there is no CLI flag for them. A hinted attempt gets at most 25 % of the deadline (≤ 200 ms) in a cone of at least 3°, then falls back to the blind search (`src/solve.rs`).
- **Database:** its own index built from **ASTAP's D80 catalogue (Gaia DR3, epoch 2025)**: 12.5 M index stars in 9 levels, from 248′ down to 0.97′ neighbourhoods.

### Using it from Python

- There are **no Python bindings** and no C ABI. It offers a **CLI** and a Rust library (`skyshot::Engine`).
- The supported Python route is a subprocess (`examples/python_cli.py`):
  - `skyshot solve frame.fits --index DIR [--wcs out.wcs] [--deadline-ms 2000]` prints one JSON object.
  - The JSON has `solution.status`, `ra`, `dec`, `scale_arcsec`, `rotation_deg`, `parity`, `wcs.cd/crpix` and `timings`.
  - Exit code 0 means solved, 1 unsolved, 2 an error.
  - `skyshot batch` opens the index once for many files. A persistent JSONL request mode is "planned".
- It reads FITS (BITPIX 16 with BZERO works), PNG, JPEG and TIFF, and does its own star detection.
  - It has no Bayer handling. The raw GRBG frame solves as-is.
  - So does our 2×2 green superpixel image, which is slightly faster.
- **Build:**
  - The minimal stable toolchain installed with rustup in user space is about 600 MB.
  - `cargo build --release --locked` compiles 109 crates in **53 s** on the Ryzen 5 7600X.
  - The result is a 3.7 MB binary that links only libc and libm.
  - Cargo needs a **C linker** (`build-essential`). This WSL box had none, so `zig cc` from the `ziglang` wheel stood in.
  - Rust ≥ 1.85 is required (edition 2024). The author pins 1.95; 1.99.1 worked here.

### Star database

- **Required:** the **Full learned** index (version `2026.10.17.2`, for engine 0.1.0), downloaded from [skyshot.app/downloads](https://skyshot.app/downloads/) with `tools/fetch_data.py --preset full` (Python 3.11+, standard library only).
  - The download is an 838.5 MB archive: **6 min 13 s** here, at about 2.3 MB/s from skyshot.app. The installer checks SHA256 sums, resumes interrupted downloads and installs only into a new directory.
  - It installs to **843 MB**: `codes.bin` 581 MB, `deep.bin` 219 MB, `stars.bin` 75 MB, plus keys, cells and the coder. The 800 MB download cache can be deleted afterwards.
  - Nothing is generated locally.
- The author's real-image checks for the Full index cover longest sides of **0.24°–12.5°**. Our telephoto field is 4.0° along its longest side, well inside that range. The optional DEEP index (+4.09 GB) is for fields under about 0.3° and isn't needed.
- **Wide camera (32°×54°):** **untested**.
  - No wide frames are saved locally or on koma-server.
  - 54° is outside every tested range: Full goes to 12.5°, and the "historical wide companion" (22 MB) covers 5.3°–12.5° from three images.
  - The engine's search bounds go to 75°, and `--arc` is offered for very wide frames.
  - Treat the wide camera as unsupported until a frame is tried.
- **Building our own index:** possible with `skyshot build --d80` from ASTAP's D80 (1.3 GB of tiles). It needs "substantial RAM" and gives no benefit over the download.

### Benchmark

Run on the Ryzen 5 7600X, with frames on NVMe:
- the 8 saved telephoto frames (M31 ×3 and M45 ×3 at 60 s, real M31 frames of 60 s and 10 s);
- 2 s and 1 s simulated frames made from M31 60 s, M45 60 s and M31 10 s (same method and seed as above).

Each frame was solved in several header variants:
- **blind**: all position and scale keywords removed;
- **scale**: `FOCALLEN` + `XPIXSZ` only;
- **hint**: scale, plus `RA/DEC` off target by +1.5°/−1.0°, like the earlier benchmark;
- **as-is**: the original header.

Runs:
- **cold**: the CLI is started with the index evicted from the page cache (`posix_fadvise DONTNEED`);
- **warm**: two more CLI runs.

The CLI time is the whole process wall time: start, index mmap, FITS decode, detection and solve.

| solver / config (same PC, same frames) | real 60 s / 10 s (8 frames) | sim 2 s (3) | sim 1 s (3) | success |
|---|---|---|---|---|
| **Skyshot CLI, hint**, warm | **35–45 ms** | 30–36 ms | 29–40 ms | 28/28 |
| **Skyshot CLI, blind**, warm | **35–61 ms** | 30–75 ms | 32–79 ms | 28/28 |
| Skyshot CLI, scale only, warm | 35–44 ms | 31–39 ms | 29–37 ms | 28/28 |
| Skyshot CLI, hint / blind, **cold** | 50–76 / 59–93 ms | 58–110 / 61–113 ms | 67–98 / 100–118 ms | 28/28 |
| Skyshot CLI, green superpixel FITS, scale only, warm | 25–34 ms | – | – | 16/16 |
| Skyshot `batch` (index opened once, 14 frames, blind) | 17–36 ms per frame, 0.34 s in all | | | 14/14 |
| ASTAP D05 `-z 2`, hint `-r 6` | 132–236 ms | 125–132 ms | 117–128 ms | 14/14 |
| ASTAP D05 `-z 2`, blind `-r 180` | 212–370 ms | 182–219 ms | 162–198 ms | 14/14 |
| astrometry, early stop, no SIP (`imaging.stars` 147–171 ms + solve) | 153–238 ms | 158–175 ms | 155–166 ms | 14/14 |

Other results:
- **Overall success:** **174/174** Skyshot runs, across every variant, cold and warm. Detection found 650–2020 stars at 60 s or 10 s, and 280–510 at 2 s and 1 s.
- **Hint gain:** small. Warm medians are 39–40 ms either way. Blind solve time is mostly 6–30 ms with a tail to 80 ms, and a hint mainly removes that tail (warm maximum ≤ 45 ms).
- **Headers as written by the sequencer:**
  - The sequencer writes `OBJCTRA` in **degrees** (10.68 for M31), but Skyshot (and the FITS convention) read decimal `OBJCTRA` as **hours**.
  - So the M31 lights carry a wrong hint (160°). Skyshot spends its 200 ms hint budget and then solves blind: **134–140 ms** instead of about 30.
  - For M45, `OBJCTRA` = 56.6 is out of range as hours, so it is ignored and the correct `RA/DEC` are used.
  - If Skyshot is adopted, write `RA/DEC` in degrees and either drop `OBJCTRA` or write it in hours.
- **False positives:** none. A pure-noise frame returned `unsolved` in 0.08 s, and a frame with 60 random fake stars returned `unsolved` after its 1.3 s deadline (exit code 1 for both).
- **Memory:** peak RSS was 338 MB over the 14-frame batch. The figure is mostly mapped index pages, so it shrinks under memory pressure.

### Accuracy

- **Reference:** astrometry.net (`astrometry` 4.3.0, 120 stars, tune-up with SIP 2), with the frame centre evaluated from its WCS.
  - That evaluation carries a one-superpixel (7″) pixel-origin ambiguity. ASTAP settles it: it agrees with this reference to **1.0–1.2″**.

| | Skyshot vs reference (all 174 runs) | ASTAP vs reference |
|---|---|---|
| frame centre | **0.4–2.5″** (median 0.8″) | 1.0–1.2″ |
| rotation | **−0.055° … +0.054°** | +0.026° … +0.051° |
| scale | ≤ 0.08 % | – |
| parity | same in every run | same |

- The 2 s and 1 s simulated frames give the same position as their 60 s source to ≤ 1.5″.
- Skyshot and ASTAP agree with each other at the arcsecond level. Their common ≤ 0.05° rotation offset from the reference is likely the reference's SIP-vs-TAN fit, which is irrelevant for centring and for the polar tool.
- **Orientation:** yes. Every solution has `rotation_deg` (177.9° for M31, 179.5–179.8° for M45 here), `parity` and the full CD matrix, and can write a FITS WCS file (`--wcs`). That is everything the polar tool needs.
- **Side finding, the current astrometry path:** `Match.center_ra_deg/center_dec_deg` from the `astrometry` wrapper is **not the frame centre**.
  - It sits close to the centre of the detected stars' bounding box: **1.1–5.8′** from the frame centre on these 8 frames.
  - `rig.py` returns it as the solved pointing, so the current centring and sync carry that error.
  - The frame centre should be evaluated from `wcs_fields` instead. This is not fixed here.

### Fit for seestar-commander

| question | finding |
|---|---|
| **Licence vs MIT** | **Code: yes.** Apache-2.0, used as a separate executable and not vendored. **Index data:** "public preview, non-commercial intended use". Provenance of the D80 compilation and the model training lineage is "pending", and no redistribution grant is given. Downloading it for our own non-commercial observatory use is fine. **Don't redistribute or bundle the index** in the repo or in images. |
| **Ubuntu 26.04 x86_64 (koma-server)** | Expected to work: it built and ran here on Ubuntu 24.04 x86_64 (WSL) and needs only glibc. The author doesn't claim Linux x86_64 runtime support (CI exists but "not run"). The server needs rustup or a distro Rust ≥ 1.85, `build-essential` and 843 MB of disk (plus a temporary 800 MB download cache). Not built on the server, because it is read-only for this task. |
| **Raspberry Pi** | **aarch64: plausible but unverified.** The author ran its 147 tests in a Linux ARM64 container (synthetic data only). A Pi 4/5 with ≥ 2 GB RAM and 1 GB of storage fits the 843 MB mmap index. Solves will be slower (my estimate, not measured: about 5–10×, still under 1 s), and cold reads from SD will cost more. A native Pi build takes minutes; cross-compiling with `cargo-zigbuild` also works. **armhf (32-bit): not tested anywhere**; an 843 MB mmap on 32-bit is risky. ASTAP ships prebuilt for both. |
| **Maturity / risk** | **High.** It is one author's project, three weeks old, at version 0.1.0 with no release. The API, CLI JSON ("detailed diagnostic fields are experimental") and the index format can change, and the index is tied to the exact engine version. The only issue so far has no reply. Mitigations: pin the commit and the index version, keep it out of process, and check `solution.status`. |
| **Orientation for the polar tool** | **Yes:** `rotation_deg`, `parity`, CD matrix and optional WCS file. |

Why fallback and not primary:
- ASTAP is already fast enough: 0.12–0.37 s against a 2 s exposure plus about 4 s download.
- It is mature, has prebuilt binaries for every target, a 107 MB database and clear terms.
- Skyshot's gain is about 0.2 s per solve. As a fallback, its immaturity matters less, and it adds a fast, independent blind solver with its own detector.
- That makes it more useful than the GPL astrometry path. Swap the order once Skyshot has a tagged release, Linux qualification and settled data terms.

## Solver survey

| solver | algorithm | licence | speed for our field | hint needed | database | fit for 2.2°×4°, 3.7″/px |
|---|---|---|---|---|---|---|
| **astrometry.net** via `astrometry` PyPI | geometric hashing of 4-star quads, Bayesian verification | astrometry.net BSD-3; **PyPI wrapper GPL-3.0** | 0.01–0.5 s with early stop (25–35 s without) | no (hint speeds it up) | 4100 series (Tycho-2), 4110–4112 ≈ 40 MB, all 4100 ≈ 0.36 GB | good, already working |
| **ASTAP** CLI | quads of 4 stars against a magnitude-limited Gaia-based database | **MPL-2.0** | 0.12–0.38 s incl. detection, blind 0.23–0.38 s | no | D05 107 MB, D20 407 MB (D05 and D20 equal here) | good |
| **cedar-solve** (tetra3 fork) | 4-star pattern hash (tetra3) | **Apache-2.0** | **1–100 ms** (partial-sky DB), blind | no (FOV estimate speeds it up) | must be generated from Tycho (`tyc_main`) for a 2–4° field: full sky about 250 MB, about 20 min (estimate). The bundled default is for wide fields | works; packaging friction (below) |
| SharpCap (Polar Align, SharpSolve) | own quad solver, database tiled and searched outward from the start position, in memory, multithreaded | proprietary | about 1 s (polar align); whole-sky at 0.5° FOV 9 s | uses the start position | bundled | n/a (Windows app) |
| **Skyshot** CLI | dominance-level neighbourhoods hashed to 32-bit (learned) keys, voting, verification, WCS fit | **Apache-2.0** code; index data "non-commercial intended use" | **30–80 ms warm, 50–120 ms cold** incl. FITS read + detection, blind | no (hint trims the tail) | Full learned index 843 MB (download, from ASTAP D80 / Gaia DR3) | **tested: 174/174, ≤ 2.5″** ([evaluation](#skyshot-evaluation-2026-11-09)); young project |
| skysolver.app | 4-star quads against Gaia DR3 | no source | tens of ms (author's figures) | no | hosted | n/a (browser) |

### Notes per question

**skysolve.app.** No site by exactly that name was found. There are three similar projects:
- [SkySolve](https://github.com/githubdoe/skysolve) (GPL-3.0) is a Raspberry Pi push-to that installs astrometry.net index files and also carries a `tetra3.py`. It quotes "about every 1 to 10 seconds" per solve.
- [skysolver.app](https://skysolver.app) is a free browser solver that matches 4-star quads against 13.1 M Gaia DR3 quads. It quotes tens of ms per photo (48 ms average on its demos), and is closed source with no hint needed ([AstroBin thread](https://app.astrobin.com/forum/topic/259219)).
- The open-source one in this space is [Skyshot](https://github.com/ashtree74/skyshot-solver) (Rust, Apache-2.0, [thread](https://app.astrobin.com/forum/topic/259285)). It quotes 4 ms solves on an Apple M5 Max. It is evaluated in its own section above.

**PiFinder.** It uses cedar-solve's `tetra3` with `cedar_detect_client`, and calls `solve_from_centroids(…, fov_estimate, fov_max_error, solve_timeout=1000)` ([solver.py](https://github.com/brickbots/PiFinder/blob/main/python/PiFinder/solver.py)).
- Its database `pifinder_fov10-5_m7_hip.npz` covers 5–10° fields from Hipparcos to magnitude 7.
- [cedar-solve](https://github.com/smroid/cedar-solve) is Apache-2.0.
  - PyPI 0.5.1 pins `numpy<2` and `Pillow<9`. Pillow 8 doesn't build on current Python.
  - `pip install --no-deps cedar-solve` installs, but `solve_from_centroids` then crashes on numpy 2 (`np.math.factorial`); a one-line shim (`np.math = math`) worked around it here.
  - GitHub has a newer v0.6.0 tag that isn't on PyPI.
  - Its own docs say Hipparcos is "sufficient down to about >3 deg" and recommend `tyc_main` for smaller fields.
  - Small-FOV databases "can take many hours" to generate.
- [cedar-detect](https://github.com/smroid/cedar-detect) (Rust gRPC star extractor, "usually less than 10 ms" per Mpx on a Pi 4) is **FSL-1.1-MIT**, not Apache.
- Our 2.2°×4° field at 3.7″/px needs a Tycho-2 database with `max_fov≈4.2, min_fov≈2`.

**SharpCap.**
- **Polar alignment** uses SharpCap's own solver with a small bundled database around the pole, built for "really fast performance" ([forum](https://forums.sharpcap.co.uk/viewtopic.php?p=9037), [forum](https://forums.sharpcap.co.uk/viewtopic.php?p=658)).
- **SharpSolve** (2023) is a built-in quad solver ([forum](https://forums.sharpcap.co.uk/viewtopic.php?p=39123)):
  - its whole index is held in memory, split into 522 tiles searched in order of distance from the start position, and multithreaded;
  - it is Gaia-based;
  - a whole-sky solve at 0.5° FOV takes 9 s.
- **Push-To** can also call ASTAP (a log shows `-ra -spd -r 15 -z 0 -fov 3.60`, [forum](https://forums.sharpcap.co.uk/viewtopic.php?p=10282)).
- Why it's fast: the index is resident in memory, the search starts at the known position, and it stops at the first verified match. Astrometry.net does the same once it is told to stop.

**ASTAP** ([hnsky.org](https://www.hnsky.org/astap.htm), [SourceForge](https://sourceforge.net/projects/astap-program/), MPL-2.0).
- A 0.9 MB static CLI for Linux amd64, armhf and aarch64. Tested: `astap_cli` CLI-2026.10.30.
- Command: `astap_cli -f frame.fits -ra <h> -spd <dec+90> -r 6 -fov 3.99 -z 2 -d <dir> -D d05`. The result goes to `frame.ini` / `frame.wcs`, and the exit code is 0 when solved.
- Databases: D05/D20/D50/D80 hold up to 500/2000/5000/8000 stars per square degree, for fields of 0.6° and up. D05 is enough here.
- ASTAP does its own star detection on the raw Bayer frame, so it would replace `imaging.stars` for solving.

**astrometry.net with hints.**
- The [astrometry.net docs](https://astrometry.net/doc/readme.html) suggest index quads of about 10–100 % of the image size, which means roughly 4107–4113 for our field. More indexes are slower.
- The [`astrometry` package README](https://github.com/neuromorphicsystems/astrometry) documents `logodds_callback` (return `Action.STOP`), `tune_up_logodds_threshold`, `sip_order`, `maximum_quads` and `PositionHint`/`SizeHint`.
- Its `Solver` keeps index files open until `close()`, and our code keeps one `Solver` for the life of the service.

## Licences (MIT public repo)

| component | licence | how we'd use it | OK for MIT repo? |
|---|---|---|---|
| ASTAP CLI + D05 | MPL-2.0 (program), databases free to download | separate executable via `subprocess`; not vendored | **yes**: no linking, nothing redistributed |
| `astrometry` (PyPI) | GPL-3.0 | imported in-process (current code) | as an **optional, user-installed** extra it is defensible, since we don't distribute it. Don't vendor it or make it a hard dependency. Document it as optional (it already is) |
| astrometry.net index files | BSD-3 / data | downloaded at first use | yes |
| cedar-solve | Apache-2.0 | in-process | yes |
| cedar-detect | FSL-1.1-MIT | separate gRPC server | yes to call; it becomes MIT after 2 years |
| Skyshot CLI | Apache-2.0 | separate executable via `subprocess`, built from source, not vendored | **yes** |
| Skyshot Full index | "public preview, non-commercial intended use"; upstream D80/Gaia terms and redistribution rights unresolved | downloaded by the user at install | **yes for our own use**; never commit or redistribute it |

## Proposed next steps (not in this PR)

1. Add the early stop and no-SIP option to `AstrometrySolver.solve`, change the `solve_exposure` default from 10 to 2 s, and poll `ImageReady` every 0.1 s.
2. **Fix the centring position in `rig.py`:** evaluate the frame centre from `Match.wcs_fields` rather than using `center_ra_deg/center_dec_deg`, which is 1–6′ off.
3. Add an `AstapSolver` (config `[solver] kind = "astap"`, `astap_path`, `db_dir`). It writes the raw frame to a temporary FITS, runs `astap_cli` and parses the `.ini`. Make it the default when the binary is found.
4. Add a `SkyshotSolver` behind the same interface (`skyshot_path`, `index_dir`). It writes a temporary FITS with `RA/DEC` in degrees and `FOCALLEN`/`XPIXSZ`, and no degree-valued `OBJCTRA`. It runs `skyshot solve --deadline-ms 1000` and parses `solution.ra/dec/rotation_deg/parity`. Use it as the fallback, with astrometry as an optional legacy extra. Pin the Skyshot commit and the index version `2026.10.17.2`.
5. On the next clear night, log per-stage times for exposure, wait, download, extract and solve. Probe `ImageArrayRaw` (ImageBytes) and `CanSubframe` once. Save one wide-camera frame, to try Skyshot and ASTAP on 32°×54°.

## Reproducing

Benchmark environment:
- a scratch venv with `astrometry==4.3.0`, `cedar-solve==0.5.1 --no-deps`, numpy 2.5 and scipy 1.18;
- the `astap_cli` amd64 zip with `d05_star_database.zip` and `d20_star_database.zip`;
- the 4100 index files copied from the server cache, and Tycho `tyc_main.dat` (VizieR I/239) for the cedar-solve database;
- frames: `data/sessions/20261108/{M31,M45}/lights/*.fits` and a 10 s M31 test frame from the proof-of-concept run;
- Skyshot at `76f0181`, built with rustup stable 1.99.1 (`zig cc` as linker), Full learned index `2026.10.17.2`. Header variants were written with astropy, and the CLI was timed from Python `subprocess`.

The scripts were throwaway, and these tables are their output.
