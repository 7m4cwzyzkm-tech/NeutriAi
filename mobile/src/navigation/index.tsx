/** Navigation tree + auth gate. */
import React, { useEffect } from 'react';
import { Text } from 'react-native';
import { NavigationContainer, useNavigationContainerRef } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
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
import { MealDetailScreen } from '../screens/MealDetailScreen';
import { DeleteAccountScreen } from '../screens/DeleteAccountScreen';

const Tab = createBottomTabNavigator();
const Stack = createNativeStackNavigator();

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
  const navRef = useNavigationContainerRef();

  // Registers the device on every launch (tokens rotate) and routes taps.
  usePushNotifications((route, params) => {
    if (route === 'Paywall') return openPaywall();
    if (navRef.isReady()) navRef.navigate(route as never, params as never);
  });

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, [setSession]);

  return (
    <NavigationContainer
      ref={navRef}
      linking={linking}
      theme={{
        dark: true,
        colors: {
          primary: c.accent, background: c.bg, card: c.surface,
          text: c.text, border: c.border, notification: c.accent,
        },
        fonts: {
          regular: { fontFamily: 'System', fontWeight: '400' },
          medium: { fontFamily: 'System', fontWeight: '500' },
          bold: { fontFamily: 'System', fontWeight: '700' },
          heavy: { fontFamily: 'System', fontWeight: '800' },
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
