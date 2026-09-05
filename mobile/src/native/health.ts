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
 */
import { Platform } from 'react-native';
import { api } from '../api/client';

const READ_WINDOW_DAYS = 7;

type PushProvider = 'apple_health' | 'samsung_health';

interface DayPayload {
  day: string;
  provider: PushProvider;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Apple HealthKit (react-native-health)
// ---------------------------------------------------------------------------
async function readAppleHealth(): Promise<DayPayload[]> {
  const AppleHealthKit = require('react-native-health').default;

  const permissions = {
    permissions: {
      read: [
        'StepCount', 'DistanceWalkingRunning', 'FlightsClimbed',
        'ActiveEnergyBurned', 'BasalEnergyBurned', 'HeartRate',
        'RestingHeartRate', 'HeartRateVariability', 'Vo2Max', 'SleepAnalysis',
        'Workout',
      ],
      write: ['Water', 'Workout'],
    },
  };

  await new Promise<void>((resolve, reject) => {
    AppleHealthKit.initHealthKit(permissions, (err: string) =>
      err ? reject(new Error(err)) : resolve(),
    );
  });

  const end = new Date();
  const start = new Date(end.getTime() - READ_WINDOW_DAYS * 86400_000);
  const opts = { startDate: start.toISOString(), endDate: end.toISOString() };

  const call = <T>(fn: string): Promise<T> =>
    new Promise((resolve) =>
      AppleHealthKit[fn](opts, (_err: unknown, results: T) => resolve(results ?? ([] as unknown as T))),
    );

  const [steps, active, basal, distance, resting, sleep] = await Promise.all([
    call<any[]>('getDailyStepCountSamples'),
    call<any[]>('getActiveEnergyBurned'),
    call<any[]>('getBasalEnergyBurned'),
    call<any[]>('getDailyDistanceWalkingRunningSamples'),
    call<any[]>('getRestingHeartRateSamples'),
    call<any[]>('getSleepSamples'),
  ]);

  // Bucket every sample stream by calendar day and merge.
  const byDay = new Map<string, DayPayload>();
  const bucket = (iso: string) => {
    const day = iso.slice(0, 10);
    if (!byDay.has(day)) byDay.set(day, { day, provider: 'apple_health' });
    return byDay.get(day)!;
  };

  const sum = (rows: any[], key: string, field = 'value') => {
    for (const r of rows ?? []) {
      const d = bucket(r.startDate ?? r.date);
      d[key] = (Number(d[key]) || 0) + Number(r[field] ?? 0);
    }
  };

  sum(steps, 'stepCount');
  sum(active, 'activeEnergyBurned');
  sum(basal, 'basalEnergyBurned');
  sum(distance, 'distanceWalkingRunning');

  for (const r of resting ?? []) {
    bucket(r.startDate).restingHeartRate = Number(r.value);
  }
  for (const r of sleep ?? []) {
    const d = bucket(r.startDate);
    const minutes =
      (new Date(r.endDate).getTime() - new Date(r.startDate).getTime()) / 60000;
    if (r.value === 'ASLEEP' || r.value === 'CORE' || r.value === 'DEEP' || r.value === 'REM') {
      d.sleepAnalysisAsleep = (Number(d.sleepAnalysisAsleep) || 0) + minutes;
      if (r.value === 'DEEP') d.sleepAnalysisDeep = (Number(d.sleepAnalysisDeep) || 0) + minutes;
      if (r.value === 'REM') d.sleepAnalysisREM = (Number(d.sleepAnalysisREM) || 0) + minutes;
    }
  }

  return [...byDay.values()].map((d) => ({
    ...d,
    stepCount: d.stepCount ? Math.round(Number(d.stepCount)) : undefined,
    activeEnergyBurned: d.activeEnergyBurned ? Math.round(Number(d.activeEnergyBurned)) : undefined,
    basalEnergyBurned: d.basalEnergyBurned ? Math.round(Number(d.basalEnergyBurned)) : undefined,
    sleepAnalysisAsleep: d.sleepAnalysisAsleep ? Math.round(Number(d.sleepAnalysisAsleep)) : undefined,
  }));
}

// ---------------------------------------------------------------------------
// Health Connect (Android / Samsung)
// ---------------------------------------------------------------------------
async function readHealthConnect(): Promise<DayPayload[]> {
  const HC = require('react-native-health-connect');

  const available = await HC.initialize();
  if (!available) throw new Error('Health Connect is not available on this device.');

  await HC.requestPermission([
    { accessType: 'read', recordType: 'Steps' },
    { accessType: 'read', recordType: 'ActiveCaloriesBurned' },
    { accessType: 'read', recordType: 'BasalMetabolicRate' },
    { accessType: 'read', recordType: 'HeartRate' },
    { accessType: 'read', recordType: 'RestingHeartRate' },
    { accessType: 'read', recordType: 'SleepSession' },
    { accessType: 'read', recordType: 'Distance' },
  ]);

  const end = new Date();
  const start = new Date(end.getTime() - READ_WINDOW_DAYS * 86400_000);
  const filter = {
    timeRangeFilter: {
      operator: 'between' as const,
      startTime: start.toISOString(),
      endTime: end.toISOString(),
    },
  };

  const read = async (type: string) => {
    try {
      const { records } = await HC.readRecords(type, filter);
      return records ?? [];
    } catch {
      return [];
    }
  };

  const [steps, active, distance, sleep, resting] = await Promise.all([
    read('Steps'), read('ActiveCaloriesBurned'), read('Distance'),
    read('SleepSession'), read('RestingHeartRate'),
  ]);

  const byDay = new Map<string, DayPayload>();
  const bucket = (iso: string) => {
    const day = String(iso).slice(0, 10);
    if (!byDay.has(day)) byDay.set(day, { day, provider: 'samsung_health' });
    return byDay.get(day)!;
  };

  for (const r of steps) bucket(r.startTime).steps = (Number(bucket(r.startTime).steps) || 0) + Number(r.count ?? 0);
  for (const r of active)
    bucket(r.startTime).activeCalories =
      (Number(bucket(r.startTime).activeCalories) || 0) + Number(r.energy?.inKilocalories ?? 0);
  for (const r of distance)
    bucket(r.startTime).distance =
      (Number(bucket(r.startTime).distance) || 0) + Number(r.distance?.inMeters ?? 0);
  for (const r of sleep) {
    const mins = (new Date(r.endTime).getTime() - new Date(r.startTime).getTime()) / 60000;
    bucket(r.startTime).sleepDuration = (Number(bucket(r.startTime).sleepDuration) || 0) + mins;
  }
  for (const r of resting) bucket(r.time).restingHeartRate = Number(r.beatsPerMinute ?? 0);

  return [...byDay.values()].map((d) => ({
    ...d,
    steps: d.steps ? Math.round(Number(d.steps)) : undefined,
    activeCalories: d.activeCalories ? Math.round(Number(d.activeCalories)) : undefined,
    sleepDuration: d.sleepDuration ? Math.round(Number(d.sleepDuration)) : undefined,
  }));
}

/**
 * Read the last week from the platform health store and push it to NeutriAI.
 * Returns the number of days uploaded. Safe to call repeatedly — the server
 * upserts on (user, day, provider).
 */
export async function syncHealthToServer(provider?: PushProvider): Promise<number> {
  const target: PushProvider =
    provider ?? (Platform.OS === 'ios' ? 'apple_health' : 'samsung_health');

  const days =
    target === 'apple_health' ? await readAppleHealth() : await readHealthConnect();

  if (!days.length) return 0;
  await api.fitness.pushHealth(days);
  return days.length;
}

/** Write a water log back to the platform store, when the user opts in. */
export async function writeWaterToHealth(ml: number): Promise<void> {
  if (Platform.OS !== 'ios') return;
  const AppleHealthKit = require('react-native-health').default;
  await new Promise<void>((resolve) => {
    AppleHealthKit.saveWater({ value: ml / 1000, unit: 'liter' }, () => resolve());
  });
}
