#!/usr/bin/env node
/**
 * Independent numeric cross-check for cardGauge.ts -- NOT a test runner, NOT
 * a test of cardGauge.ts's actual code.
 *
 * mobile/ has no test suite (jest, or any other runner) and this task was
 * asked not to add one. This file does NOT import cardGauge.ts; TypeScript
 * needs a compile step to run under plain Node, and this repo has no
 * ts-node/tsx dependency to do that without adding a new tool. Instead,
 * this is a SEPARATE, independent re-derivation of the same formulas in
 * plain JavaScript, checked against numbers verified once, offline, with
 * Python (the same language card_gauge.py itself is written in) -- so a
 * typo in the TypeScript port is likely to disagree with BOTH this file and
 * the Python source, not silently agree with a duplicated mistake.
 *
 * HOW TO RUN IT (no install, no build -- Node ships on any machine that can
 * run this project's other npm scripts):
 *
 *     node mobile/src/lib/cardGauge.selfcheck.js
 *
 * It prints PASS/FAIL for each check and exits non-zero if anything failed.
 *
 * WHAT THIS DOES NOT PROVE: that cardGauge.ts itself is free of typos, or
 * that ScanScreen.tsx calls it correctly. For that, run the existing
 * `npm run typecheck` (mobile/package.json's own script, already in this
 * repo, no new tool) from mobile/, which will fail to compile if
 * cardGauge.ts or its use in ScanScreen.tsx has a type error. Neither check
 * runs the app; both are static, and neither has ever been run in this
 * session (this container cannot run npm install or start Expo -- see this
 * task's own report for what remains unverified until Gil runs the app on
 * his phone).
 */
'use strict';

const CARD_LONG_MM = 85.6;
const CARD_SHORT_MM = 53.98;
const TARGET_DISTANCE_MM = 304.8;
const DEFAULT_FOV_DEG = 68.0;
const DEFAULT_ASPECT_RATIO = 4 / 3;

function expectedCardPx(axisPx, axisFovDeg, distanceMm) {
  distanceMm = distanceMm === undefined ? TARGET_DISTANCE_MM : distanceMm;
  const focalPx = axisPx / 2 / Math.tan(((axisFovDeg * Math.PI) / 180) / 2);
  return (focalPx * CARD_LONG_MM) / distanceMm;
}

function expectedCardBoxPoints(w, h, fovDeg, distanceMm) {
  fovDeg = fovDeg === undefined ? DEFAULT_FOV_DEG : fovDeg;
  distanceMm = distanceMm === undefined ? TARGET_DISTANCE_MM : distanceMm;
  const longAxis = Math.max(w, h);
  const cardLong = expectedCardPx(longAxis, fovDeg, distanceMm);
  return { widthPoints: cardLong, heightPoints: cardLong * (CARD_SHORT_MM / CARD_LONG_MM) };
}

function distanceErrorPct(actualMm, targetMm) {
  targetMm = targetMm === undefined ? TARGET_DISTANCE_MM : targetMm;
  return (actualMm / targetMm - 1) * 100;
}

function gaugeState(measuredCardPx, expectedPx, tiltDeg) {
  if (tiltDeg >= 5.0) return 'tilted';
  const ratio = measuredCardPx / expectedPx;
  if (ratio < 0.95) return 'too_far';
  if (ratio > 1.05) return 'too_close';
  return 'ok';
}

function frameGroundCoverageMm(fovDeg, aspectRatio, distanceMm) {
  fovDeg = fovDeg === undefined ? DEFAULT_FOV_DEG : fovDeg;
  aspectRatio = aspectRatio === undefined ? 1 / DEFAULT_ASPECT_RATIO : aspectRatio;
  distanceMm = distanceMm === undefined ? TARGET_DISTANCE_MM : distanceMm;
  const longAxisMm = 2 * distanceMm * Math.tan(((fovDeg * Math.PI) / 180) / 2);
  const shortAxisMm = aspectRatio >= 1 ? longAxisMm / aspectRatio : longAxisMm * aspectRatio;
  return { longAxisMm, shortAxisMm };
}

// --- Checks, each against a value computed independently in Python. ---

let failures = 0;
function check(label, actual, expected, tol) {
  tol = tol === undefined ? 1e-6 : tol;
  const ok = Math.abs(actual - expected) <= tol;
  console.log((ok ? 'PASS' : 'FAIL') + '  ' + label + '  got=' + actual + '  want=' + expected);
  if (!ok) failures += 1;
}

// A 390x844 pt screen (an iPhone-shaped example, not a real device query --
// this file cannot read a real screen). Verified in Python:
//   expected_card_box_points(390, 844) -> (175.70487656751772, 110.80080884479679)
const box = expectedCardBoxPoints(390, 844);
check('box widthPoints (390x844 screen)', box.widthPoints, 175.70487656751772, 1e-6);
check('box heightPoints (390x844 screen)', box.heightPoints, 110.80080884479679, 1e-6);

// Orientation independence: swapping which number is "width" and which is
// "height" must not change the result (max()/min() symmetry) -- this is
// the whole point of card_gauge.py's orientation-independence fix.
const boxSwapped = expectedCardBoxPoints(844, 390);
check('box widthPoints is unchanged when width/height are swapped',
  boxSwapped.widthPoints, box.widthPoints, 1e-9);

check('distanceErrorPct at exactly the target', distanceErrorPct(304.8), 0.0);
check('distanceErrorPct(350)', distanceErrorPct(350), 14.82939632545932, 1e-6);
check('distanceErrorPct(250)', distanceErrorPct(250), -17.979002624671914, 1e-6);

check('gaugeState: exact size, no tilt -> ok', gaugeState(100, 100, 0) === 'ok' ? 1 : 0, 1);
check('gaugeState: exact size, tilted -> tilted (tilt checked first)',
  gaugeState(100, 100, 6) === 'tilted' ? 1 : 0, 1);
check('gaugeState: 10% small, no tilt -> too_far', gaugeState(90, 100, 0) === 'too_far' ? 1 : 0, 1);
check('gaugeState: 10% big, no tilt -> too_close', gaugeState(110, 100, 0) === 'too_close' ? 1 : 0, 1);

const coverage = frameGroundCoverageMm();
check('frameGroundCoverageMm default longAxisMm', coverage.longAxisMm, 411.1803918671434, 1e-6);
check('frameGroundCoverageMm default shortAxisMm', coverage.shortAxisMm, 308.3852939003575, 1e-6);

console.log('');
if (failures > 0) {
  console.log(failures + ' check(s) FAILED.');
  process.exit(1);
} else {
  console.log('All checks passed. This does not prove cardGauge.ts itself has no typos --');
  console.log('run `npm run typecheck` from mobile/ for that, and test on device for the rest.');
}
