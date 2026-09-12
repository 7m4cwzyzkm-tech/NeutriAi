/**
 * Expo config.
 *
 * Everything static lives in app.json. This file exists for one reason: the
 * three values the app cannot start without are per-environment, and two of
 * them must not be committed.
 *
 *   EXPO_PUBLIC_SUPABASE_URL       your project URL
 *   EXPO_PUBLIC_SUPABASE_ANON_KEY  the anon/publishable key (safe on a device
 *                                  — it is what RLS is designed around — but
 *                                  it is still per-project, so it is not
 *                                  hardcoded here)
 *   EXPO_PUBLIC_API_URL            the NeutriAI backend, including /v1
 *
 * Put them in mobile/.env (see .env.example). Expo loads that automatically.
 *
 * NEVER put the Supabase service key here. Anything reaching this file reaches
 * the app bundle, and the service key bypasses RLS entirely — one decompile and
 * every user's meals and body metrics are readable.
 */
const DEFAULT_API_URL = 'https://api.neutriai.com/v1';

/**
 * Missing config used to surface as `TypeError: Cannot read property 'replace'
 * of undefined` from deep inside supabase-js at startup, which tells you
 * nothing. Fail here instead, where the fix is obvious.
 *
 * Only a warning, not a throw: `expo prebuild`, EAS metadata reads and CI all
 * evaluate this file without needing a working backend, and hard-failing them
 * is worse than a clear message plus a runtime guard in src/api/supabase.ts.
 */
function readConfig() {
  const url = process.env.EXPO_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY;
  const apiUrl = process.env.EXPO_PUBLIC_API_URL || DEFAULT_API_URL;

  const missing = [
    !url && 'EXPO_PUBLIC_SUPABASE_URL',
    !anonKey && 'EXPO_PUBLIC_SUPABASE_ANON_KEY',
  ].filter(Boolean);

  if (missing.length) {
    console.warn(
      `\n[neutriai] Missing ${missing.join(' and ')}.\n` +
      `[neutriai] Copy mobile/.env.example to mobile/.env and fill it in, ` +
      `then restart with: npx expo start -c\n`,
    );
  }

  return { url, anonKey, apiUrl };
}

module.exports = ({ config }) => {
  const { url, anonKey, apiUrl } = readConfig();
  return {
    ...config,
    extra: {
      ...config.extra,
      supabaseUrl: url,
      supabaseAnonKey: anonKey,
      apiUrl,
      // Publishable by definition; absent is fine — the paywall falls through
      // to hosted Stripe Checkout, which does not need it on the device.
      stripePublishableKey: process.env.EXPO_PUBLIC_STRIPE_PUBLISHABLE_KEY,
    },
  };
};
