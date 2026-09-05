/**
 * Account deletion. Required by Apple 5.1.1(v) and Google's data deletion
 * policy — an app with accounts and no in-app deletion is rejected.
 *
 * Two things stores actually check for, beyond the button existing:
 *   1. It is reachable from inside the app, not a link to a support email.
 *   2. It deletes the account, not just the local session.
 *
 * Two things that are our own standard, not theirs:
 *   - The confirmation is *specific*. "This removes 143 meals and 22 workouts"
 *     lets someone make a real decision; "are you sure?" does not.
 *   - Store-managed subscriptions are called out explicitly, because deleting
 *     the account does not cancel them and someone would keep being charged.
 */
import React, { useEffect, useState } from 'react';
import { Alert, ScrollView, Text, TextInput, View } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Divider, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { request } from '../api/client';
import { supabase } from '../api/supabase';

interface Preview {
  meals: number; workouts: number; recipes: number; posts: number; photos: number;
  subscription_active: boolean;
  subscription_store_managed: boolean;
  subscription_note: string | null;
}

export function DeleteAccountScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirmText, setConfirmText] = useState('');
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    request<Preview>('/me/deletion-preview')
      .then(setPreview)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  async function doDelete() {
    setDeleting(true);
    try {
      const res = await request<{ ok: boolean; message?: string }>('/me/account', {
        method: 'DELETE',
        query: { confirm: 'DELETE' },
        timeoutMs: 45000,
        retries: 0,
      });
      await supabase.auth.signOut();
      Alert.alert('Account deleted', res.message ?? 'Your data has been removed.');
    } catch (e: any) {
      Alert.alert('Could not delete account', e?.message ?? 'Please try again or contact support.');
    } finally {
      setDeleting(false);
    }
  }

  if (loading) return <Screen><Loading /></Screen>;

  const rows: [string, number][] = preview
    ? [
        ['Meals logged', preview.meals],
        ['Workouts', preview.workouts],
        ['Recipes you wrote', preview.recipes],
        ['Posts', preview.posts],
        ['Photos', preview.photos],
      ].filter(([, n]) => (n as number) > 0) as [string, number][]
    : [];

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 60 }}>
          <View>
            <Label>Account</Label>
            <H1>Delete your account</H1>
            <Body dim>This is permanent. There is no undo and no recovery window.</Body>
          </View>

          <Card style={{ gap: space.md }}>
            <H2>What gets deleted</H2>
            {rows.length ? (
              rows.map(([label, n]) => (
                <Row key={label} style={{ justifyContent: 'space-between' }}>
                  <Text style={[type.body, { color: c.text }]}>{label}</Text>
                  <Text style={[type.body, { color: c.textDim, fontVariant: ['tabular-nums'] }]}>
                    {n}
                  </Text>
                </Row>
              ))
            ) : (
              <Body dim>You haven’t logged anything yet.</Body>
            )}
            <Divider />
            <Body dim>
              Your profile, targets, streaks, saved recipes, meal photos and every setting
              are removed from our servers. Nothing is retained.
            </Body>
          </Card>

          {preview?.subscription_active ? (
            <Card style={{ borderColor: preview.subscription_store_managed ? c.warn : c.border, gap: space.sm }}>
              <Label>Your subscription</Label>
              <Body>{preview.subscription_note}</Body>
              {preview.subscription_store_managed ? (
                <Body dim>
                  Deleting your account here does not stop the billing. Cancel it in your
                  device’s subscription settings first, or you will keep being charged.
                </Body>
              ) : null}
            </Card>
          ) : null}

          <Card style={{ gap: space.md, borderColor: c.danger }}>
            <Label>Type DELETE to confirm</Label>
            <TextInput
              value={confirmText}
              onChangeText={setConfirmText}
              autoCapitalize="characters"
              autoCorrect={false}
              placeholder="DELETE"
              placeholderTextColor={c.textFaint}
              style={{
                backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                padding: space.lg, color: c.text, fontSize: 17, letterSpacing: 2,
              }}
            />
            <Button
              title="Permanently delete my account"
              variant="danger"
              disabled={confirmText !== 'DELETE'}
              loading={deleting}
              onPress={() =>
                Alert.alert(
                  'Last check',
                  'This deletes everything immediately and cannot be undone.',
                  [
                    { text: 'Keep my account', style: 'cancel' },
                    { text: 'Delete', style: 'destructive', onPress: doDelete },
                  ],
                )
              }
            />
          </Card>

          <Button title="Never mind, keep my account" variant="ghost" onPress={() => nav.goBack()} />

          <Body dim>
            Just want a break? Signing out keeps everything and you can come back any time.
          </Body>
          <Button title="Sign out instead" variant="secondary" onPress={() => supabase.auth.signOut()} />
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
