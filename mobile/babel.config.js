/**
 * Babel config for Metro.
 *
 * This file is not optional in an Expo project — without it Metro has no
 * preset, and the very first bundle fails on the JSX in App.tsx.
 *
 * It used to list react-native-reanimated/plugin. Reanimated 4 moved that
 * plugin to the separate react-native-worklets package, so the old path now
 * throws at bundle time. Rather than migrate it, reanimated was removed:
 * nothing in this app imports it, and neither of the navigators we use
 * (bottom-tabs, native-stack) requires it.
 *
 * If reanimated is ever added back, the plugin goes here LAST and comes from
 * 'react-native-worklets/plugin', not the reanimated package.
 */
module.exports = function (api) {
  api.cache(true);
  return {
    presets: ['babel-preset-expo'],
  };
};
