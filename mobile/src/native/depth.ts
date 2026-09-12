/**
 * Camera geometry for the portion estimator.
 *
 * Why this exists
 * ---------------
 * A single photo does not contain volume. The estimator infers it, and the
 * quality of that inference depends entirely on whether anything in the frame
 * has a known real-world size. Its rungs, best to worst:
 *
 *   plate_reference   a calibrated plate          ~10-15% error
 *   depth_model       camera distance + lens FOV  ~15-20%
 *   vessel_reference  "it's a bowl", typical size ~18-25%
 *   pixel_area        assume a 27 cm dinner plate ~25-35%
 *   ai_prior          a serving-size guess        ~40%+
 *
 * Depth is the only rung that works with NO reference object in frame. Food on
 * paper, on a cutting board, on a restaurant table -- all of it currently falls
 * to the bottom rung. Measured against weighed meals, a cheesesteak on its
 * wrapper came out 39% low because nothing in the photo had a known size.
 *
 * Given distance d and horizontal field of view f, the frame's real width is
 * 2 * d * tan(f / 2). That is trigonometry, not a prior, which is why this is
 * worth native code.
 *
 * What ships today
 * ----------------
 * Expo's managed runtime exposes neither ARKit depth nor ARCore's Depth API,
 * so this module reads from an optional native module and returns null when it
 * is absent. Null is a supported answer: the backend treats every field as
 * optional and falls back to the vessel rung, exactly as it does now. Nothing
 * regresses on a device that cannot measure.
 *
 * To turn it on you need a development build (not Expo Go) plus a native
 * module named `NeutriDepth` exposing:
 *
 *   measure(): Promise<{
 *     distanceMm: number;      // camera to nearest surface in the centre region
 *     fovDeg?: number;         // horizontal FOV of the active lens
 *     aspectRatio?: number;    // frame width / height
 *   } | null>
 *
 *   iOS      ARKit. Run an ARSession, raycast from the frame centre to the
 *            detected horizontal plane (the table), take the hit distance.
 *            On LiDAR devices ARFrame.sceneDepth is more accurate still.
 *            FOV: derive from ARCamera.intrinsics -- 2*atan(width / (2*fx)).
 *   Android  ARCore Depth API, or Camera2 LENS_FOCUS_DISTANCE as a cruder
 *            fallback. FOV: SENSOR_INFO_PHYSICAL_SIZE with LENS_FOCAL_LENGTH.
 *
 * Sanity rules live on the server (implausible values are ignored rather than
 * clamped), but obvious nonsense is filtered here too so a broken sensor never
 * silently degrades an estimate.
 */
import { NativeModules, Platform } from 'react-native';

export type CameraGeometry = {
  camera_distance_mm?: number;
  camera_fov_deg?: number;
  camera_aspect_ratio?: number;
};

/** Ranges outside which a reading is treated as a sensor fault, not data. */
const DISTANCE_MIN_MM = 80;      // closer than this and the lens cannot focus
const DISTANCE_MAX_MM = 2000;    // further and it is not a photo of your meal
const FOV_MIN_DEG = 40;
const FOV_MAX_DEG = 100;

type NativeDepth = {
  measure: () => Promise<{
    distanceMm?: number;
    fovDeg?: number;
    aspectRatio?: number;
  } | null>;
};

const native: NativeDepth | undefined = (NativeModules as Record<string, unknown>)
  .NeutriDepth as NativeDepth | undefined;

/** True when this build can actually measure. Drives UI copy, nothing else. */
export function isDepthAvailable(): boolean {
  return typeof native?.measure === 'function';
}

function usable(v: unknown, min: number, max: number): number | undefined {
  const n = typeof v === 'number' ? v : NaN;
  if (!Number.isFinite(n) || n < min || n > max) return undefined;
  return Math.round(n * 10) / 10;
}

/**
 * Measure the capture geometry, or return {} if this device cannot.
 *
 * Never throws and never blocks the shutter: a scan must still work when the
 * sensor is missing, slow or wrong. A measurement that arrives late is worth
 * nothing, so it is abandoned after a short timeout.
 */
export async function measureCameraGeometry(timeoutMs = 400): Promise<CameraGeometry> {
  if (!isDepthAvailable()) return {};
  try {
    const reading = await Promise.race([
      native!.measure(),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), timeoutMs)),
    ]);
    if (!reading) return {};

    const out: CameraGeometry = {};
    const d = usable(reading.distanceMm, DISTANCE_MIN_MM, DISTANCE_MAX_MM);
    // Distance is the whole point. Without it the other two mean nothing, so
    // send nothing rather than a partial hint the server would have to guess at.
    if (d === undefined) return {};
    out.camera_distance_mm = d;

    const fov = usable(reading.fovDeg, FOV_MIN_DEG, FOV_MAX_DEG);
    if (fov !== undefined) out.camera_fov_deg = fov;

    const aspect = usable(reading.aspectRatio, 0.5, 2.5);
    if (aspect !== undefined) out.camera_aspect_ratio = aspect;

    return out;
  } catch {
    // A sensor failure is not a scan failure.
    return {};
  }
}

/** Platform note for settings/debug screens. */
export function depthCapabilityNote(): string {
  if (isDepthAvailable()) return 'Distance sensing is on — portions are measured, not estimated.';
  return Platform.select({
    ios: 'This build cannot measure distance. Portions are estimated from what your food is served on.',
    android: 'This build cannot measure distance. Portions are estimated from what your food is served on.',
    default: 'Distance sensing is unavailable here.',
  })!;
}
