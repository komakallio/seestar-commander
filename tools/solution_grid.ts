/**
 * Sample a projections-solution (github.com/lkangas/projections) on a pixel grid and
 * print JSON: { width, height, step, rows: [[x, y, vx, vy, vz] | null ...] } where v is
 * the unit direction in the solution's sky frame (J2000 for blind solves).
 *
 * usage: PROJECTIONS_DIR=... node tools/solution_grid.ts <solution.json> [step]
 */
import { readFileSync } from 'node:fs';

const dir = process.env.PROJECTIONS_DIR;
if (!dir) throw new Error('set PROJECTIONS_DIR');
const { unproject } = await import(`${dir}/src/core/camera/model.ts`);
const sol = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const step = Number(process.argv[3] ?? 60);
const { width, height } = sol.model.lens;
const pts: (number[] | null)[] = [];
for (let y = 0; y <= height; y += step) {
  for (let x = 0; x <= width; x += step) {
    const v = unproject(sol.model, x, y);
    pts.push(v ? [x, y, v[0], v[1], v[2]] : null);
  }
}
const c = unproject(sol.model, width / 2, height / 2);
console.log(JSON.stringify({ width, height, step, center: c, fit: sol.fit, pts }));
