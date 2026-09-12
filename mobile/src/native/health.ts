/**
 * Phone-side health bridge.
 *
 * Apple Health and Health Connect (Samsung/Android) have no server API — data
 * only leaves the device if the app reads it and uploads it. This module wraps
 * both behind one function so the UI does not branch on platform.
 *
 * Note the deliberate asymmetry: we READ steps, energy, heart rate and sleep,
 * but WRITE back only water and workouts. Reading everything and writing
 * nothing back makes the app a data black hole; writing back health data we
 * merely inferred would pollute the user's medical record.
 *
 * CURRENT STATE — READ THIS BEFORE "FIXING" THE STUBS BELOW
 * --------------------------------------------------------
 * The reads are not implemented right now, and that is deliberate.
 *
 * react-native-health and react-native-health-connect are old-architecture
 * bridge modules. The legacy architecture was REMOVED in Expo SDK 55 /
 * React Native 0.82, so neither library can load on this runtime at all —
 * this is not a packaging problem that a reinstall fixes.
 *
 * They were also not safe to merely "lazy require". Metro resolves
 * require('literal-string') at BUILD time no matter where the call sits —
 * inside a function, inside a try/catch, it makes no difference. A lazy
 * require defers execution, not resolution. An uninstalled module referenced
 * that way fails the whole bundle, which is exactly how this stub came to be.
 *
 * The previous working implementation is preserved verbatim in
 * docs/HEALTH_NATIVE.md. The permission strings and record types below are
 * kept live rather than buried in that doc, because they are the part that is
 * tedious to rediscover and easy to get subtly wrong.
 *
 * To reinstate: pick new-architecture libraries, implement readAppleHealth /
 * readHealthConnect against the constants below, and return DayPayload[]. The
 * server contract does not change.
 */
import { Platform } from 'react-native';
import { api } from '../api/client';

export const READ_WINDOW_DAYS = 7;

export type PushProvider = 'apple_health' | 'samsung_health';

export interface DayPayload {
  day: string;
  provider: PushProvider;
  [key: string]: unknown;
}

/**
 * HealthKit identifiers, exactly as Apple spells them. Wrong casing here fails
 * silently — the permission is simply never granted and reads return empty.
 */
export const APPLE_HEALTH_PERMISSIONS = {
  read: [
    'StepCount', 'DistanceWalkingRunning', 'FlightsClimbed',
    'ActiveEnergyBurned', 'BasalEnergyBurned', 'HeartRate',
    'RestingHeartRate', 'HeartRateVariability', 'Vo2Max', 'SleepAnalysis',
    'Workout',
  ],
  write: ['Water', 'Workout'],
} as const;

/** Health Connect record types, in the read set the targets actually need. */
export const HEALTH_CONNECT_RECORD_TYPES = [
  'Steps', 'ActiveCaloriesBurned', 'BasalMetabolicRate', 'HeartRate',
  'RestingHeartRate', 'SleepSession', 'Distance',
] as const;

/**
 * Whether this build can read the platform health store.
 *
 * Always false today. Kept as a function rather than a constant so callers are
 * written against a runtime check from the start — when a development build
 * gains the native module, only this file changes.
 */
export function healthAvailable(): boolean {
  return false;
}

/** The one message the UI should show. Phrased for a user, not a developer. */
export const HEALTH_UNAVAILABLE_MESSAGE =
  Platform.OS === 'ios'
    ? 'Apple Health sync needs the full NeutriAI app. It is not available in Expo Go.'
    : 'Health Connect sync needs the full NeutriAI app. It is not available in Expo Go.';

/**
 * Read the last week from the platform health store and push it to NeutriAI.
 * Returns the number of days uploaded. Safe to call repeatedly — the server
 * upserts on (user, day, provider).
 */
export async function syncHealthToServer(provider?: PushProvider): Promise<number> {
  const target: PushProvider =
    provider ?? (Platform.OS === 'ios' ? 'apple_health' : 'samsung_health');

  if (!healthAvailable()) {
    // Throwing rather than returning 0 on purpose: 0 means "synced, nothing
    // new", and a user who taps Sync deserves to be told it did not happen
    // rather than watching it silently succeed forever.
    throw new Error(HEALTH_UNAVAILABLE_MESSAGE);
  }

  const days: DayPayload[] = [];
  void target;

  if (!days.length) return 0;
  await api.fitness.pushHealth(days);
  return days.length;
}

/** Write a water log back to the platform store, when the user opts in. */
export async function writeWaterToHealth(_ml: number): Promise<void> {
  // Silent no-op by design, unlike the sync above. Water logging succeeds in
  // NeutriAI either way; mirroring it to Apple Health is a bonus, and failing
  // the user's water log because a bonus is unavailable would be wrong.
  if (!healthAvailable()) return;
}
