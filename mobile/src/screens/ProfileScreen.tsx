/** Profile, targets breakdown, integrations, subscription. */
import React from 'react';
import { Alert, Linking, ScrollView, Switch, Text, View } from 'react-native';
import * as WebBrowser from 'expo-web-browser';
import { useNavigation } from '@react-navigation/native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { space, type, useTheme } from '../theme';
import { Body, Button, Card, Divider, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { supabase } from '../api/supabase';
import { useIntegrations, useProfile, useSubscription, useTargets } from '../hooks/useApi';
import { useApp } from '../state/store';
import { syncHealthToServer } from '../native/health';

const PROVIDER_LABEL: Record<string, string> = {
  apple_health: 'Apple Health', fitbit: 'Fitbit', garmin: 'Garmin Connect',
  google_fit: 'Google Fit', samsung_health: 'Samsung Health',
};

export function ProfileScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const { data: profile, isLoading } = useProfile();
  const { data: targets } = useTargets();
  const { data: sub } = useSubscription();
  const { data: integrations, refetch: refetchIntegrations } = useIntegrations();
  const openPaywall = useApp((s) => s.openPaywall);

  async function connect(provider: string) {
    try {
      const res = await api.fitness.connect(provider);
      if (res.flow === 'oauth' && res.authorize_url) {
        await WebBrowser.openAuthSessionAsync(res.authorize_url, 'neutriai://integrations');
        refetchIntegrations();
      } else {
        // Push-only providers: ask the OS for permission, then upload.
        const count = await syncHealthToServer(provider as 'apple_health' | 'samsung_health');
        Alert.alert('Synced', `${count} days sent to NeutriAI.`);
        refetchIntegrations();
      }
    } catch (e: any) {
      Alert.alert('Could not connect', e?.message ?? 'Try again.');
    }
  }

  if (isLoading) return <Screen><Loading /></Screen>;

  const r = (targets?.rationale ?? {}) as Record<string, unknown>;

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
          <View>
            <Label>Profile</Label>
            <H1>{profile?.display_name || profile?.handle}</H1>
            <Text style={[type.caption, { color: c.textFaint }]}>@{profile?.handle}</Text>
          </View>

          {/* ---- subscription ---- */}
          <Card onPress={() => openPaywall()} style={{ gap: space.sm }}>
            <Row style={{ justifyContent: 'space-between' }}>
              <View>
                <Label>Plan</Label>
                <H2>
                  {sub?.tier === 'trial' ? 'Free trial'
                    : sub?.tier === 'pro' ? 'Pro monthly'
                    : sub?.tier === 'pro_annual' ? 'Pro annual'
                    : 'Free'}
                </H2>
              </View>
              <Text style={{ color: c.accent, fontSize: 15, fontWeight: '600' }}>
                {sub?.is_active ? 'Manage' : sub?.free_launch_mode ? 'See plans' : 'Upgrade'}
              </Text>
            </Row>
            {/* free_launch_mode (settings.free_launch_mode, via GET
                /billing/subscription -- already fetched here, no new
                endpoint) means there is no real scan limit right now, so
                "X of 3 free scans" and "Upgrade" would both be lying to
                someone who is not actually blocked by anything. The card
                stays tappable either way -- opening the paywall is a
                voluntary look at plans, never a forced gate, and the
                purchase flow still needs to work for anyone who wants to
                support the app early. */}
            {!sub?.is_active ? (
              <Body dim>
                {sub?.free_launch_mode
                  ? 'NeutriAI is free right now — no limits.'
                  : `${sub?.ai_scans_used_today ?? 0} of ${sub?.ai_scans_quota ?? 3} free AI scans used today.`}
              </Body>
            ) : null}
          </Card>

          {/* ---- targets, with the reasoning shown ---- */}
          {targets ? (
            <Card style={{ gap: space.md }}>
              <H2>Your daily targets</H2>
              <Row style={{ justifyContent: 'space-between' }}>
                {[
                  ['kcal', targets.target_kcal, c.text],
                  ['protein', `${targets.protein_g}g`, c.protein],
                  ['carbs', `${targets.carbs_g}g`, c.carbs],
                  ['fat', `${targets.fat_g}g`, c.fat],
                ].map(([l, v, col]) => (
                  <View key={String(l)} style={{ alignItems: 'center' }}>
                    <Text style={[type.h2, { color: col as string }]}>{String(v)}</Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>{String(l)}</Text>
                  </View>
                ))}
              </Row>
              <Divider />
              {/* The BMR/TDEE math (and the goal/macro-method lines that
                  explained it) is gone -- Gil's call, nobody needs to see the
                  calculation here. An estimated BMI, computed client-side
                  from the profile fields the app already has, stands in its
                  place. The calorie-floor warning stays: it's a real
                  exception a user might otherwise wonder about, not "the
                  calculation." */}
              <View style={{ gap: 4 }}>
                {profile?.height_cm && profile?.weight_kg ? (
                  <Text style={[type.caption, { color: c.textDim }]}>
                    Estimated BMI: {(profile.weight_kg / (profile.height_cm / 100) ** 2).toFixed(1)}
                  </Text>
                ) : null}
                {r.calorie_floor_applied ? (
                  <Text style={[type.caption, { color: c.warn }]}>
                    A minimum-calorie floor was applied — we won't prescribe below it.
                  </Text>
                ) : null}
              </View>
              <Button
                title="Recalculate"
                variant="secondary"
                onPress={() => api.profile.targets(true)}
              />
            </Card>
          ) : null}

          {/* ---- wearables ---- */}
          <Card style={{ gap: space.md }}>
            <H2>Connected devices</H2>
            {(integrations ?? []).map((i) => (
              <Row key={i.provider} style={{ justifyContent: 'space-between' }}>
                <View style={{ flex: 1 }}>
                  <Text style={[type.body, { color: c.text }]}>
                    {PROVIDER_LABEL[i.provider] ?? i.provider}
                  </Text>
                  <Text style={[type.caption, { color: c.textFaint }]}>
                    {i.connected
                      ? i.last_sync_at
                        ? `Synced ${new Date(i.last_sync_at).toLocaleString()}`
                        : 'Connected'
                      : i.supports_pull ? 'Not connected' : 'Syncs from this phone'}
                  </Text>
                </View>
                <Button
                  title={i.connected ? 'Sync' : 'Connect'}
                  variant="secondary"
                  onPress={() =>
                    i.connected && i.supports_pull
                      ? api.fitness.sync(i.provider).then(() => refetchIntegrations())
                      : connect(i.provider)
                  }
                />
              </Row>
            ))}
            <Body dim>
              Apple Health and Samsung Health have no server API — the phone reads them locally and
              uploads. Fitbit, Garmin and Google Fit sync in the background every four hours.
            </Body>
          </Card>

          <Button
            title="Sign out"
            variant="ghost"
            onPress={() => supabase.auth.signOut()}
          />

          {/* Required by Apple 5.1.1(v) and Google's data deletion policy:
              deletion must be reachable from inside the app. */}
          <Button
            title="Delete account"
            variant="ghost"
            onPress={() => nav.navigate('DeleteAccount')}
          />
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
