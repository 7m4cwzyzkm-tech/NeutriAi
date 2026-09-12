/**
 * The navigation contract.
 *
 * Lives in its own file so both the navigator and the push handler can import
 * it without importing each other. Before this existed, `navigate()` was
 * called with `as never` casts, which silence the compiler rather than
 * satisfying it — and what they were silencing was three real bugs:
 *
 *   1. Push taps navigated to "Home", "Feed", "Profile" and "Train" as if they
 *      were top-level routes. They are tabs nested inside "Main", so the
 *      correct call is navigate('Main', { screen: 'Home' }).
 *   2. A tap on a post notification passed { id }, but nothing declares or
 *      reads an `id` param — the post reference was silently dropped.
 *   3. Nothing checked that a route named in a server-sent payload exists at
 *      all, so a renamed screen would fail at runtime with no warning.
 */
import type { NavigatorScreenParams } from '@react-navigation/native';

export type TabParamList = {
  Home: undefined;
  Train: undefined;
  Scan: undefined;
  Recipes: undefined;
  // Both carry an optional subject from a push tap. No screen reads them yet,
  // but the server does send them, and dropping data it sent is worse than
  // carrying it until the screen is ready to use it.
  Feed: { postId?: string } | undefined;
  Profile: { userId?: string } | undefined;
};

export type RootStackParamList = {
  Auth: undefined;
  Main: NavigatorScreenParams<TabParamList> | undefined;
  Fasting: undefined;
  Onboarding: undefined;
  PlateCalibration: undefined;
  // `edit` is passed by ScanScreen to open straight into correction mode.
  MealDetail: { mealId: string; edit?: boolean };
  DeleteAccount: undefined;
};

/**
 * Where a notification tap should land.
 *
 * Deliberately not a route name and a bag of params: a push payload is
 * attacker-adjacent input from the network, and the set of things it is
 * allowed to ask for should be a closed list the compiler can check, not any
 * string that happens to match a screen. Every variant here maps to exactly
 * one navigation call in RootNavigator, and the switch over them is
 * exhaustive — adding a variant without handling it is a compile error.
 */
export type PushTarget =
  | { kind: 'paywall' }
  | { kind: 'fasting' }
  | { kind: 'tab'; tab: 'Home' | 'Train' | 'Scan' | 'Recipes' }
  | { kind: 'feed'; postId?: string }
  | { kind: 'profile'; userId?: string };
