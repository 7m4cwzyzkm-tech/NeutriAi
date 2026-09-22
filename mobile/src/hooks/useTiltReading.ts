/**
 * Live tilt-from-level, in degrees, from the phone's accelerometer.
 *
 * "Level" here means the classic overhead-shot pose this screen's guide
 * assumes: the phone lying screen-up, camera-back facing straight down at
 * the food. At rest in that pose, expo-sensors' Accelerometer reads
 * (x, y, z) close to (0, 0, +/-1) g -- gravity almost entirely along the
 * phone's own z-axis, whichever sign that platform reports. Tilting the
 * phone rotates that vector away from the z-axis, and the angle between the
 * measured vector and the z-axis is exactly the tilt away from level:
 *
 *     tiltDeg = degrees(acos(|z| / |(x, y, z)|))
 *
 * Using |z| rather than z makes this robust to either sign convention
 * (iOS and Android report gravity's sign differently, and this was never
 * verified on a device -- see this task's own report). A phone held
 * upright instead of overhead (e.g. photographing something on a wall)
 * would read a false ~90 degree tilt under this formula; that is fine for
 * this app's one intended pose and not corrected for, since there is no
 * other signal (no gyroscope fusion) to disambiguate "held sideways
 * deliberately" from "actually tilted."
 *
 * UPDATE RATE: 4 Hz -- every 250 ms, not every accelerometer sample.
 * card_gauge.GAUGE_TILT_LIMIT_DEG is a 5-degree threshold; it does not need
 * frame-rate precision, and sampling faster would only add re-renders (and
 * battery cost) for a signal a human hand cannot change meaningfully within
 * a single video frame. 4 Hz is fast enough to feel live while holding a
 * phone still, and slow enough that ordinary accelerometer jitter at rest
 * (a few hundredths of a g) does not flicker the displayed state between
 * "ok" and "tilted."
 *
 * UNVERIFIED ON HARDWARE: this hook has never run on a real device or
 * simulator (this session cannot run the app at all -- see the task's hard
 * rules). The formula and rate are a considered design, not a measured one;
 * expect to tune the rate and/or re-check the sign assumption once Gil sees
 * it move on his phone, per the task's own "Notes for Gil."
 */
import { useEffect, useState } from 'react';
import { Accelerometer } from 'expo-sensors';

const UPDATE_INTERVAL_MS = 250;

/** Below this, the reading is a sensor glitch (device in freefall, or the
 * very first callback before a real sample has arrived), not a real pose. */
const MIN_PLAUSIBLE_MAGNITUDE_G = 0.5;

/**
 * @param enabled Whether the accelerometer subscription should be running.
 *   ScanScreen renders one component with several conditional return
 *   branches (capture / uploading / results), and React's Rules of Hooks
 *   mean this hook is called on every render regardless of which branch is
 *   showing -- `enabled` is how the caller stops the subscription (and its
 *   battery cost) when the camera guide is not even on screen, without
 *   conditionally calling the hook itself.
 * @returns The current tilt in degrees, or null before the first reading
 *   arrives (or while disabled).
 */
export function useTiltReading(enabled: boolean = true): number | null {
  const [tiltDeg, setTiltDeg] = useState<number | null>(null);

  useEffect(() => {
    if (!enabled) {
      setTiltDeg(null);
      return;
    }
    Accelerometer.setUpdateInterval(UPDATE_INTERVAL_MS);
    const subscription = Accelerometer.addListener(({ x, y, z }) => {
      const magnitude = Math.sqrt(x * x + y * y + z * z);
      if (magnitude < MIN_PLAUSIBLE_MAGNITUDE_G) return;
      const cosTilt = Math.min(1, Math.abs(z) / magnitude);
      setTiltDeg((Math.acos(cosTilt) * 180) / Math.PI);
    });
    return () => subscription.remove();
  }, [enabled]);

  return tiltDeg;
}
