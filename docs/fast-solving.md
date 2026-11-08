# Fast telephoto plate solving (offline research)

2026-11-08. Research and offline benchmarks only. Nothing here talked to the SeeStar, and no code changes are included.

**Conclusion:** the 40–55 s solve-to-solve time is mostly **our use of the `astrometry` package**, not astrometry.net itself.
- `Solver.solve()` does not stop at the first match. With the default `SolutionParameters` it keeps searching every star slice in every index, and it runs a SIP tune-up (about 0.4 s) for each of the 9–90 matches it finds. That makes **25–35 s per solve**.
- The first match arrives after **0.4 s**. With an early stop and no SIP, the same solve takes **0.01–0.09 s**.
- With that fixed, a **2 s** solve exposure and the current download, centring should cost about **7 s per solve and 11 s solve-to-solve**, including the sync and the fine move.

**Recommendation** (cedar-solve was fastest at 1–100 ms, but it needs a self-built database and its PyPI release is broken on numpy 2, so it is the option to revisit later):
- **Primary: ASTAP CLI with the D05 database.**
  - It runs as a subprocess on the raw FITS, so our code doesn't link to it (MPL-2.0).
  - It solves in **0.12–0.38 s** including FITS read and star detection, and also solves **blind** in 0.3 s.
- **Fallback: the existing `astrometry` path with an early stop.**
  - It costs nothing new: it is already installed and its index files are already in the cache.
  - The package is GPL-3.0 and is imported in-process, so it should stay an optional extra (see Licences).

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

## Solver survey

| solver | algorithm | licence | speed for our field | hint needed | database | fit for 2.2°×4°, 3.7″/px |
|---|---|---|---|---|---|---|
| **astrometry.net** via `astrometry` PyPI | geometric hashing of 4-star quads, Bayesian verification | astrometry.net BSD-3; **PyPI wrapper GPL-3.0** | 0.01–0.5 s with early stop (25–35 s without) | no (hint speeds it up) | 4100 series (Tycho-2), 4110–4112 ≈ 40 MB, all 4100 ≈ 0.36 GB | good, already working |
| **ASTAP** CLI | quads of 4 stars against a magnitude-limited Gaia-based database | **MPL-2.0** | 0.12–0.38 s incl. detection, blind 0.23–0.38 s | no | D05 107 MB, D20 407 MB (D05 and D20 equal here) | good |
| **cedar-solve** (tetra3 fork) | 4-star pattern hash (tetra3) | **Apache-2.0** | **1–100 ms** (partial-sky DB), blind | no (FOV estimate speeds it up) | must be generated from Tycho (`tyc_main`) for a 2–4° field: full sky about 250 MB, about 20 min (estimate). The bundled default is for wide fields | works; packaging friction (below) |
| SharpCap (Polar Align, SharpSolve) | own quad solver, database tiled and searched outward from the start position, in memory, multithreaded | proprietary | about 1 s (polar align); whole-sky at 0.5° FOV 9 s | uses the start position | bundled | n/a (Windows app) |
| skysolver.app / Skyshot | 4-star quads against Gaia DR3 (skysolver.app); Rust quad solver (Skyshot) | skysolver.app: no source; **Skyshot: Apache-2.0** | tens of ms (author's figures) | no | Skyshot index 838 MB (+4 GB deep) | untested; young project |

### Notes per question

**skysolve.app.** No site by exactly that name was found. There are three similar projects:
- [SkySolve](https://github.com/githubdoe/skysolve) (GPL-3.0) is a Raspberry Pi push-to that installs astrometry.net index files and also carries a `tetra3.py`. It quotes "about every 1 to 10 seconds" per solve.
- [skysolver.app](https://skysolver.app) is a free browser solver that matches 4-star quads against 13.1 M Gaia DR3 quads. It quotes tens of ms per photo (48 ms average on its demos), and is closed source with no hint needed ([AstroBin thread](https://app.astrobin.com/forum/topic/259219)).
- The open-source one in this space is [Skyshot](https://github.com/ashtree74/skyshot-solver) (Rust, Apache-2.0, [thread](https://app.astrobin.com/forum/topic/259285)). It is blind and quotes 4 ms solves on an Apple M5 Max (author's research numbers). Linux ARM support isn't stated.

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

## Proposed next steps (not in this PR)

1. Add the early stop and no-SIP option to `AstrometrySolver.solve`, change the `solve_exposure` default from 10 to 2 s, and poll `ImageReady` every 0.1 s.
2. Add an `AstapSolver` (config `[solver] kind = "astap"`, `astap_path`, `db_dir`). It writes the raw frame to a temporary FITS, runs `astap_cli` and parses the `.ini`. Make it the default when the binary is found, with astrometry as the fallback.
3. On the next clear night, log per-stage times for exposure, wait, download, extract and solve. Probe `ImageArrayRaw` (ImageBytes) and `CanSubframe` once.

## Reproducing

Benchmark environment:
- a scratch venv with `astrometry==4.3.0`, `cedar-solve==0.5.1 --no-deps`, numpy 2.5 and scipy 1.18;
- the `astap_cli` amd64 zip with `d05_star_database.zip` and `d20_star_database.zip`;
- the 4100 index files copied from the server cache, and Tycho `tyc_main.dat` (VizieR I/239) for the cedar-solve database;
- frames: `data/sessions/20261108/{M31,M45}/lights/*.fits` and a 10 s M31 test frame from the proof-of-concept run.

The scripts were throwaway, and these tables are their output.
