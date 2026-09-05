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

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
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

  const projectId =
    Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  const token = (await Notifications.getExpoPushTokenAsync(
    projectId ? { projectId } : undefined,
  )).data;

  try {
    await request('/me/push-token', { method: 'POST', query: { token } });
  } catch {
    // Not fatal — the app works fine without push. Retry next launch.
  }
  return token;
}

/** Register on launch, and route taps to the right screen. */
export function usePushNotifications(navigate: (route: string, params?: object) => void) {
  const responded = useRef(false);

  useEffect(() => {
    registerForPush();

    const sub = Notifications.addNotificationResponseReceivedListener((response) => {
      if (responded.current) return;
      const data = response.notification.request.content.data as Record<string, unknown>;
      const link = String(data?.deep_link ?? '');
      // neutriai://post/123 -> ['post', '123']
      const [route, id] = link.replace('neutriai://', '').split('/');
      const map: Record<string, string> = {
        home: 'Home', fasting: 'Fasting', billing: 'Paywall',
        post: 'Feed', profile: 'Profile', train: 'Train',
      };
      if (map[route]) navigate(map[route], id ? { id } : undefined);
    });

    return () => sub.remove();
  }, [navigate]);
}
