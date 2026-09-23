/**
 * Push registration and tap handling.
 *
 * Registration runs on every launch, not just the first: Expo push tokens
 * rotate on reinstall and after some OS updates, and a stale token fails
 * silently — the user simply stops getting reminders and never knows why.
 */
import { useEffect, useRef } from 'react';
import { Platform } from 'react-native';
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import Constants from 'expo-constants';
import { request } from '../api/client';
import type { PushTarget } from '../navigation/types';

Notifications.setNotificationHandler({
  // shouldShowAlert used to mean both of the flags below. iOS 14 separated
  // them, and expo-notifications followed: a banner is the drop-down at the
  // top of the screen, the list is the entry that stays in Notification
  // Centre. Both are wanted here — a fasting reminder the user swipes away
  // should still be findable afterwards.
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: true,
  }),
});

/** Android needs channels declared or notifications arrive silently. */
async function ensureChannels(): Promise<void> {
  if (Platform.OS !== 'android') return;
  const channels: [string, string, Notifications.AndroidImportance][] = [
    ['reminder', 'Reminders', Notifications.AndroidImportance.DEFAULT],
    ['motivation', 'Motivation', Notifications.AndroidImportance.LOW],
    ['celebration', 'Celebrations', Notifications.AndroidImportance.HIGH],
    ['social', 'Social', Notifications.AndroidImportance.LOW],
    ['system', 'Account', Notifications.AndroidImportance.HIGH],
    ['alert', 'Alerts', Notifications.AndroidImportance.HIGH],
  ];
  for (const [id, name, importance] of channels) {
    await Notifications.setNotificationChannelAsync(id, { name, importance });
  }
}

export async function registerForPush(): Promise<string | null> {
  // A simulator cannot receive push, and asking for permission there trains
  // developers to ignore a prompt that will matter on a real device.
  if (!Device.isDevice) return null;

  await ensureChannels();

  const existing = await Notifications.getPermissionsAsync();
  let status = existing.status;
  if (status !== 'granted') {
    status = (await Notifications.requestPermissionsAsync()).status;
  }
  if (status !== 'granted') return null;

  // An Expo push token is minted against an EAS project. Without a projectId
  // getExpoPushTokenAsync THROWS rather than returning null, and because this
  // function is called fire-and-forget from a useEffect, that surfaced as an
  // unhandled promise rejection on a screen that has nothing to do with push.
  //
  // Not having one yet is an expected state, not a failure: the project is not
  // linked to EAS until `eas init`, which happens when we make a development
  // build. Skip cleanly and say why.
  const projectId =
    Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  if (!projectId) {
    console.warn(
      '[push] no EAS projectId — push registration skipped. ' +
      'Run `eas init` to link this project; push cannot work until then.',
    );
    return null;
  }

  let token: string;
  try {
    token = (await Notifications.getExpoPushTokenAsync({ projectId })).data;
  } catch (err) {
    // Expo's push service being unreachable must not take down the app.
    console.warn('[push] could not obtain an Expo push token', err);
    return null;
  }

  try {
    await request('/me/push-token', { method: 'POST', query: { token } });
  } catch {
    // Not fatal — the app works fine without push. Retry next launch.
  }
  return token;
}

/**
 * Translate a deep link into a navigation target, or null if we do not
 * recognise it.
 *
 * The links the backend actually emits today, and nothing else:
 *   neutriai://home            motivation nudges
 *   neutriai://fasting         fast-window reminders
 *   neutriai://billing         payment failed / subscription lapsed
 *   neutriai://post/{id}       likes, comments, mentions
 *   neutriai://profile/{id}    new follower
 *
 * Plus one the backend does NOT emit: neutriai://water. It only ever arrives
 * on a LOCAL notification scheduled on-device by localReminders.ts (see that
 * file for why -- there is no EAS projectId, so server push cannot reach this
 * device at all today). This listener does not distinguish local from remote
 * taps -- expo-notifications delivers both through the same
 * addNotificationResponseReceivedListener -- so reusing this exact function
 * and switch, rather than a second parallel handler, is what keeps a local
 * reminder tap and a real push tap landing the same way.
 *
 * Returning null for anything else is the point: an old build receiving a link
 * a newer server invented should do nothing, not navigate somewhere arbitrary.
 */
export function targetForDeepLink(link: string): PushTarget | null {
  const [route, id] = link.replace('neutriai://', '').split('/');
  switch (route) {
    case 'home':     return { kind: 'tab', tab: 'Home' };
    case 'train':    return { kind: 'tab', tab: 'Train' };
    case 'scan':     return { kind: 'tab', tab: 'Scan' };
    case 'recipes':  return { kind: 'tab', tab: 'Recipes' };
    case 'fasting':  return { kind: 'fasting' };
    case 'water':    return { kind: 'water' };
    case 'billing':  return { kind: 'paywall' };
    case 'post':     return { kind: 'feed', postId: id || undefined };
    case 'profile':  return { kind: 'profile', userId: id || undefined };
    default:         return null;
  }
}

/**
 * Register on launch, and route taps to the right screen.
 *
 * `onOpen` MUST be stable across renders — wrap it in useCallback. The effect
 * below depends on it, and an inline arrow is a new function every render,
 * which would tear down and re-add the listener and re-run registerForPush()
 * on every single render: repeated permission checks and a push-token POST per
 * render.
 */
export function usePushNotifications(onOpen: (target: PushTarget) => void) {
  const responded = useRef(false);

  useEffect(() => {
    // Deliberately not awaited — push registration must never delay the UI.
    // But an un-caught floating promise is how a failure in here became a
    // full-screen error toast on the auth screen, so it is caught explicitly.
    registerForPush().catch((err) => {
      console.warn('[push] registration failed', err);
    });

    const sub = Notifications.addNotificationResponseReceivedListener((response) => {
      if (responded.current) return;
      const data = response.notification.request.content.data as Record<string, unknown>;
      const target = targetForDeepLink(String(data?.deep_link ?? ''));
      if (target) onOpen(target);
    });

    return () => sub.remove();
  }, [onOpen]);
}
