/**
 * Guided solving with the projections solver core (github.com/lkangas/projections).
 *
 *   orient: for blind-solved frames (J2000 solutions), print camera<-ENU matrices
 *           (unrefracted ENU at the frame's mid-exposure time).
 *   solve:  for each frame, start from a predicted camera<-ENU matrix, keep the lens
 *           fixed, and let solveFrame() match and refine. Writes per-frame results with
 *           a pixel grid of observed (refracted) ENU directions.
 *
 * usage: PROJECTIONS_DIR=... node tools/guided_solve.ts orient <job.json>
 *        PROJECTIONS_DIR=... node tools/guided_solve.ts solve <job.json>
 * job.json: { site:{lat,lon,height} (deg, m), lens_solution: path, frames:[
 *   {id, png, timeMs, solution?: path, start?: number[9]} ], out_dir }
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const P = process.env.PROJECTIONS_DIR;
if (!P) throw new Error('set PROJECTIONS_DIR');
const T = await import(`${P}/src/core/astro/transforms.ts`);
const Q = await import(`${P}/src/core/math/quat.ts`);
const M3 = await import(`${P}/src/core/math/mat3.ts`);
const CM = await import(`${P}/src/core/camera/model.ts`);
const CAT = await import(`${P}/src/core/catalog/catalog.ts`);
const DET = await import(`${P}/src/core/detect/detect.ts`);
const MD = await import(`${P}/src/core/match/detections.ts`);
const SF = await import(`${P}/src/core/sequence/solveFrame.ts`);
const IMG = await import(`${P}/scripts/lib/image.ts`);

const [mode, jobPath] = process.argv.slice(2);
const job = JSON.parse(readFileSync(jobPath, 'utf8'));
const D2R = Math.PI / 180;
const site = { lat: job.site.lat * D2R, lon: job.site.lon * D2R, height: job.site.height };

if (mode === 'orient') {
  const out = [];
  for (const f of job.frames) {
    const sol = JSON.parse(readFileSync(f.solution, 'utf8'));
    const frame = T.createSkyFrame(f.timeMs, site);
    const Rj = Q.toMat3(sol.model.q); // camera <- J2000
    const Rh = M3.multiply(Rj, M3.transpose(frame.m)); // camera <- ENU (unrefracted)
    out.push({ id: f.id, R: Rh, rms: sol.fit.rms, stars: sol.fit.stars });
  }
  console.log(JSON.stringify(out));
} else if (mode === 'solve') {
  const lensSol = JSON.parse(readFileSync(job.lens_solution, 'utf8'));
  const lens = lensSol.model.lens;
  const catalog = CAT.parseCatalog(readFileSync(`${P}/data/catalog/bsc5.csv`, 'utf8'));
  const detectParams = { ...DET.DEFAULT_DETECT_PARAMS, circle: 'none' };
  mkdirSync(job.out_dir, { recursive: true });
  const step = job.grid_step ?? 40;
  for (const f of job.frames) {
    const t0 = performance.now();
    const res: Record<string, unknown> = { id: f.id };
    try {
      const pixels = IMG.openFrame(f.png, readFileSync(f.png));
      const det = DET.detectStars(pixels.rgba, pixels.width, pixels.height, detectParams);
      const start = Q.normalize(Q.fromMat3(f.start));
      const frame = T.createSkyFrame(f.timeMs, site);
      const s = SF.solveFrame({
        frame,
        detections: MD.matchDetections(det.stars),
        catalog,
        lens,
        start,
        reference: { q: start, rms: lensSol.fit.rms },
        fitLens: false,
        acceptance: { minStars: job.min_stars ?? 15, maxRmsFactor: job.max_rms_factor ?? 4 },
      });
      Object.assign(res, {
        accepted: s.accepted,
        reason: s.reason,
        stars: s.stars,
        rms: s.rms,
        detections: det.stars.length,
        moved_deg: s.movement.angle / D2R,
        R: Q.toMat3(s.model.q),
      });
      const pts = [];
      for (let y = 0; y <= lens.height; y += step) {
        for (let x = 0; x <= lens.width; x += step) {
          const v = CM.unproject(s.model, x, y);
          pts.push(v ? [x, y, v[0], v[1], v[2]] : null);
        }
      }
      res.grid = { step, pts };
    } catch (e) {
      res.accepted = false;
      res.reason = e instanceof Error ? e.message : String(e);
    }
    res.ms = Math.round(performance.now() - t0);
    writeFileSync(join(job.out_dir, `frame_${String(f.id).padStart(3, '0')}.json`), JSON.stringify(res));
    console.log(
      `#${f.id} ${res.accepted ? 'ACCEPTED' : 'rejected'} stars ${res.stars} rms ${Number(res.rms).toFixed(2)} ` +
        `moved ${Number(res.moved_deg).toFixed(2)} deg ${res.reason ?? ''} ${res.ms} ms`,
    );
  }
} else {
  throw new Error('mode: orient | solve');
}
