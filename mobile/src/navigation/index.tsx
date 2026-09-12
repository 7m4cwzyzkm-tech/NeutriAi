/** Navigation tree + auth gate. */
import React, { useCallback, useEffect } from 'react';
import { Text } from 'react-native';
import { DarkTheme, NavigationContainer, useNavigationContainerRef } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import type { PushTarget, RootStackParamList, TabParamList } from './types';
import { supabase } from '../api/supabase';
import { useApp } from '../state/store';
import { usePushNotifications } from '../native/notifications';
import { useTheme } from '../theme';
import { HomeScreen } from '../screens/HomeScreen';
import { ScanScreen } from '../screens/ScanScreen';
import { FastingScreen } from '../screens/FastingScreen';
import { TrainScreen } from '../screens/TrainScreen';
import { FeedScreen } from '../screens/FeedScreen';
import { RecipesScreen } from '../screens/RecipesScreen';
import { ProfileScreen } from '../screens/ProfileScreen';
import { AuthScreen } from '../screens/AuthScreen';
import { OnboardingScreen } from '../screens/OnboardingScreen';
import PlateCalibrationScreen from '../screens/PlateCalibrationScreen';
import { MealDetailScreen } from '../screens/MealDetailScreen';
import { DeleteAccountScreen } from '../screens/DeleteAccountScreen';

const Tab = createBottomTabNavigator<TabParamList>();
const Stack = createNativeStackNavigator<RootStackParamList>();

const ICON: Record<string, string> = {
  Home: '◎', Train: '⌁', Scan: '⊕', Recipes: '♨', Feed: '⌂', Profile: '☺',
};

function Tabs() {
  const c = useTheme();
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarActiveTintColor: c.accent,
        tabBarInactiveTintColor: c.textFaint,
        tabBarStyle: {
          backgroundColor: c.surface,
          borderTopColor: c.border,
          height: 84,
          paddingTop: 8,
        },
        tabBarLabelStyle: { fontSize: 11, fontWeight: '600' },
        tabBarIcon: ({ color, focused }) => (
          <Text style={{ color, fontSize: focused ? 24 : 21 }}>{ICON[route.name] ?? '•'}</Text>
        ),
      })}
    >
      <Tab.Screen name="Home" component={HomeScreen} />
      <Tab.Screen name="Train" component={TrainScreen} />
      <Tab.Screen name="Scan" component={ScanScreen} options={{ tabBarLabel: 'Scan' }} />
      <Tab.Screen name="Recipes" component={RecipesScreen} />
      <Tab.Screen name="Feed" component={FeedScreen} />
      <Tab.Screen name="Profile" component={ProfileScreen} />
    </Tab.Navigator>
  );
}

const linking = {
  prefixes: ['neutriai://'],
  config: {
    screens: {
      Main: {
        screens: {
          Home: 'home', Train: 'train', Scan: 'scan',
          Recipes: 'recipes', Feed: 'feed', Profile: 'profile',
        },
      },
      Fasting: 'fasting',
      Onboarding: 'onboarding',
      MealDetail: 'meal/:mealId',
      DeleteAccount: 'account/delete',
    },
  },
};

export function RootNavigator() {
  const c = useTheme();
  const session = useApp((s) => s.session);
  const setSession = useApp((s) => s.setSession);
  const openPaywall = useApp((s) => s.openPaywall);
  const navRef = useNavigationContainerRef<RootStackParamList>();

  /**
   * Registers the device on every launch (tokens rotate) and routes taps.
   *
   * useCallback is load-bearing, not tidiness: usePushNotifications depends on
   * this function, so an inline arrow would re-register the listener and
   * re-run push registration on every render.
   */
  const openTarget = useCallback((target: PushTarget) => {
    // The paywall is a modal owned by app state, not a route.
    if (target.kind === 'paywall') {
      openPaywall();
      return;
    }
    if (!navRef.isReady()) return;
    switch (target.kind) {
      case 'fasting':
        navRef.navigate('Fasting');
        break;
      // Home/Train/Scan/Recipes are tabs INSIDE Main, not top-level routes.
      // Navigating to them by bare name is what the old `as never` cast was
      // hiding, and it does not reliably work.
      case 'tab':
        navRef.navigate('Main', { screen: target.tab });
        break;
      case 'feed':
        navRef.navigate('Main', { screen: 'Feed', params: { postId: target.postId } });
        break;
      case 'profile':
        navRef.navigate('Main', { screen: 'Profile', params: { userId: target.userId } });
        break;
    }
  }, [navRef, openPaywall]);

  usePushNotifications(openTarget);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, [setSession]);

  return (
    <NavigationContainer
      ref={navRef}
      linking={linking}
      // Spread the library's own dark theme rather than hand-building one.
      // The Theme type differs between React Navigation 6 and 7 (7 added a
      // `fonts` block, 6 rejects it), and hardcoding either shape breaks on
      // the other. Taking the installed theme and overriding only colours is
      // correct on both.
      theme={{
        ...DarkTheme,
        colors: {
          ...DarkTheme.colors,
          primary: c.accent, background: c.bg, card: c.surface,
          text: c.text, border: c.border, notification: c.accent,
        },
      }}
    >
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        {session ? (
          <>
            <Stack.Screen name="Main" component={Tabs} />
            <Stack.Screen
              name="Fasting"
              component={FastingScreen}
              options={{ presentation: 'modal' }}
            />
            {/* Onboarding is a stack screen, not a tab: it is a one-time flow
                with its own progress bar and no tab bar to escape through. */}
            <Stack.Screen name="Onboarding" component={OnboardingScreen} />
            <Stack.Screen
              name="PlateCalibration"
              component={PlateCalibrationScreen}
              options={{ title: 'Your plate' }}
            />
            <Stack.Screen
              name="MealDetail"
              component={MealDetailScreen}
              options={{ presentation: 'card' }}
            />
            <Stack.Screen
              name="DeleteAccount"
              component={DeleteAccountScreen}
              options={{ presentation: 'modal' }}
            />
          </>
        ) : (
          <Stack.Screen name="Auth" component={AuthScreen} />
        )}
      </Stack.Navigator>
    </NavigationContainer>
  );
}
