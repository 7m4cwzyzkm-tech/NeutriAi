/**
 * Local, on-device reminders: water (repeating slots, refreshed on
 * foreground) and fasting (one-shot, see scheduleLocalReminder below).
 *
 * Server push cannot reach this device today: registerForPush() in
 * notifications.ts skips registration entirely because this project has no
 * EAS projectId (`eas init` was never run -- confirmed in Gil's own Expo
 * logs). A water reminder therefore has to be a LOCAL notification
 * (Notifications.scheduleNotificationAsync), which needs no EAS project and
 * works in Expo Go.
 *
 * BE HONEST ABOUT WHAT THIS CAN GUARANTEE. Without a background task (which
 * itself needs a dev client / EAS, not available today), this module cannot
 * assume it will run again before the reminder window ends or before a new
 * day starts. What it does instead: compute the CURRENT day's remaining
 * reminder slots from the live settings and the current time, cancel every
 * previously-scheduled water reminder, and schedule fresh one-shot local
 * notifications for exactly those remaining slots. This is a
 * foreground-refreshed approximation, not a guaranteed background
 * scheduler -- call refreshWaterReminders() whenever the app comes to the
 * foreground and whenever the user changes reminder settings (WaterScreen.tsx
 * does both). Already-scheduled slots for today still fire while the app is
 * backgrounded -- iOS and Android both keep pending local notifications
 * without the app running -- but tomorrow's slots are only scheduled the
 * next time the app is opened. If NeutriAI is never reopened tomorrow, no
 * reminder fires tomorrow. That's the real limit of local scheduling here,
 * not a bug in this file.
 */
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

// Tags every notification THIS module schedules, so it can find and cancel
// exactly its own without touching anything else that might be
// scheduled/pushed (a celebration, a fasting reminder, ...).
const WATER_REMINDER_KIND = 'water_reminder';

export interface WaterReminderSettings {
  reminder_enabled: boolean;
  reminder_start: string; // "HH:MM" or "HH:MM:SS", as the backend sends it
  reminder_end: string;
  reminder_every_min: number;
}

function parseTimeToday(hhmm: string, base: Date): Date {
  const [h, m] = hhmm.split(':').map((n) => parseInt(n, 10));
  const d = new Date(base);
  d.setHours(Number.isFinite(h) ? h : 0, Number.isFinite(m) ? m : 0, 0, 0);
  return d;
}

/**
 * Same permission-request pattern registerForPush() uses in notifications.ts
 * -- reused, not copied, so there is exactly one place that knows how this
 * app asks for notification permission.
 */
export async function ensureNotificationPermission(): Promise<boolean> {
  if (!Device.isDevice) return false;
  const existing = await Notifications.getPermissionsAsync();
  let status = existing.status;
  if (status !== 'granted') {
    status = (await Notifications.requestPermissionsAsync()).status;
  }
  return status === 'granted';
}

/** Cancel every pending local notification this app tagged with `kind`. */
export async function cancelRemindersOfKind(kind: string): Promise<void> {
  const pending = await Notifications.getAllScheduledNotificationsAsync();
  await Promise.all(
    pending
      .filter((n) => n.content.data?.kind === kind)
      .map((n) => Notifications.cancelScheduledNotificationAsync(n.identifier)),
  );
}

async function cancelWaterReminders(): Promise<void> {
  await cancelRemindersOfKind(WATER_REMINDER_KIND);
}

/**
 * Schedule ONE local notification at a fixed future date, tagged with `kind`
 * so cancelRemindersOfKind() can find it again. A date in the past (or less
 * than a few seconds out) schedules nothing rather than firing immediately.
 *
 * A one-shot DATE trigger is handed to the OS, not kept by the app: iOS gets
 * a UNTimeIntervalNotificationTrigger registered with UNUserNotificationCenter,
 * Android an AlarmManager setExactAndAllowWhileIdle alarm (inexact
 * setAndAllowWhileIdle if the exact-alarm permission is off on Android 12+),
 * which expo-notifications re-arms on BOOT_COMPLETED. So it fires with the app
 * backgrounded or swiped away -- the app does NOT need to be reopened. It will
 * not fire if the user force-stops the app on Android (that clears its alarms)
 * or has notifications turned off.
 */
export async function scheduleLocalReminder({
  kind, title, body, date, data = {},
}: {
  kind: string;
  title: string;
  body: string;
  date: Date;
  data?: Record<string, unknown>;
}): Promise<string | null> {
  if (!Device.isDevice || date.getTime() <= Date.now() + 5_000) return null;
  return Notifications.scheduleNotificationAsync({
    content: { title, body, data: { ...data, kind } },
    trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date },
  });
}

/**
 * Recompute and reschedule today's remaining reminder slots.
 *
 * Always cancels every previously-scheduled water reminder first, even when
 * turning reminders off or when nothing is left to schedule -- a settings
 * change or an app foreground must never leave a stale slot from the last
 * settings sitting there. This IS the "reschedule instead of assume" answer
 * to local scheduling's honest limitation, not an optimization.
 *
 * `requestPermission` defaults to false -- the OS permission prompt must
 * only appear when the user actually turns the reminder toggle on
 * (WaterScreen.tsx passes true from exactly that one call site). Every other
 * call (on mount, on app foreground) passes false/omits it, so it only ever
 * proceeds when permission was already granted in an earlier session, and
 * silently schedules nothing otherwise -- never a surprise prompt from
 * simply reopening the app.
 */
export async function refreshWaterReminders(
  settings: WaterReminderSettings,
  { requestPermission = false }: { requestPermission?: boolean } = {},
): Promise<void> {
  await cancelWaterReminders();
  if (!settings.reminder_enabled || !Device.isDevice) return;

  const granted = requestPermission
    ? await ensureNotificationPermission()
    : (await Notifications.getPermissionsAsync()).status === 'granted';
  if (!granted) return;

  const now = new Date();
  const start = parseTimeToday(settings.reminder_start, now);
  const end = parseTimeToday(settings.reminder_end, now);
  const stepMs = Math.max(15, settings.reminder_every_min) * 60_000;
  if (end <= start) return; // a misconfigured window schedules nothing rather than guessing

  // The next slot at or after "now", but aligned to the grid that starts at
  // `reminder_start` -- so the times are the same every day (08:00, 09:30, ...)
  // rather than drifting to whatever minute the app happened to foreground at.
  const earliest = new Date(Math.max(start.getTime(), now.getTime() + 60_000));
  const stepsFromStart = Math.ceil((earliest.getTime() - start.getTime()) / stepMs);
  const firstSlot = new Date(start.getTime() + stepsFromStart * stepMs);

  const slots: Date[] = [];
  for (let t = firstSlot.getTime(); t <= end.getTime(); t += stepMs) {
    slots.push(new Date(t));
  }

  await Promise.all(
    slots.map((date) =>
      scheduleLocalReminder({
        kind: WATER_REMINDER_KIND,
        title: 'Time for water',
        body: "Log what you've had, or tap to catch up.",
        date,
        data: { deep_link: 'neutriai://water' },
      }),
    ),
  );
}

/** Turning reminders off, or leaving the settings screen -- stop asking. */
export async function cancelAllWaterReminders(): Promise<void> {
  await cancelWaterReminders();
}
