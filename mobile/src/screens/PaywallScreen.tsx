/**
 * Paywall — StoreKit on iOS, Play Billing on Android, Stripe on web.
 *
 * Prices come from the store, never from our own config: they vary by region,
 * by local tax, and by whatever promotional pricing is live. A hardcoded
 * "$6.99" that shows to a user who will be charged €8.99 is both a bad
 * experience and, in several jurisdictions, a legal problem.
 *
 * Restore Purchases is not optional. Apple rejects subscription apps without
 * it, and it is the only way a user who reinstalled gets their access back.
 */
import React, { useEffect, useState } from 'react';
import { Alert, Modal, Platform, ScrollView, Text, View } from 'react-native';
import * as WebBrowser from 'expo-web-browser';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { space, type, useTheme } from '../theme';
import { Body, Button, Card, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { keys, useSubscription } from '../hooks/useApi';
import { useApp } from '../state/store';
import {
  attachPurchaseListener, billingAvailable, loadProducts, purchase, restorePurchases,
  storeName, type PlanId, type StoreProduct,
} from '../native/purchases';

/**
 * Store billing needs BOTH a store platform and the native billing module.
 * In Expo Go, and in any build without it, the module is absent — offering a
 * "Subscribe" button that cannot charge anyone is worse than falling through
 * to Stripe web checkout, which works everywhere.
 */
const NATIVE = (Platform.OS === 'ios' || Platform.OS === 'android') && billingAvailable();

export function PaywallSheet() {
  const c = useTheme();
  const open = useApp((s) => s.paywallOpen);
  const reason = useApp((s) => s.paywallReason);
  const close = useApp((s) => s.closePaywall);
  const qc = useQueryClient();

  const [plan, setPlan] = useState<PlanId>('annual');
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [storeProducts, setStoreProducts] = useState<StoreProduct[]>([]);

  const { data: plans } = useQuery({
    queryKey: ['plans'], queryFn: api.billing.plans, enabled: open,
  });
  const { data: sub } = useSubscription();

  const refreshEntitlement = React.useCallback(() => {
    qc.invalidateQueries({ queryKey: keys.subscription });
    close();
  }, [qc, close]);

  useEffect(() => {
    if (!NATIVE) return;
    attachPurchaseListener(refreshEntitlement);
  }, [refreshEntitlement]);

  useEffect(() => {
    if (!open || !NATIVE) return;
    loadProducts().then(setStoreProducts).catch(() => setStoreProducts([]));
  }, [open]);

  /** Store price when we have it; our own catalogue as the fallback. */
  function priceFor(id: PlanId): string {
    const productId = id === 'monthly' ? 'app.neutriai.pro.monthly' : 'app.neutriai.pro.annual';
    const fromStore = storeProducts.find((p) => p.productId === productId);
    if (fromStore?.price) return fromStore.price;
    const fallback = plans?.find((p) => p.id === id);
    return fallback ? `$${(fallback.amount_cents / 100).toFixed(2)}` : '—';
  }

  async function startPurchase() {
    setBusy(true);
    try {
      if (NATIVE) {
        // Resolves in the global listener, which is what makes an interrupted
        // purchase recoverable on next launch.
        await purchase(plan);
      } else {
        const session = await api.billing.checkout(plan);
        const result = await WebBrowser.openAuthSessionAsync(
          session.url, 'neutriai://billing/success',
        );
        if (result.type === 'success') {
          setTimeout(refreshEntitlement, 1500);
        }
      }
    } catch (e: any) {
      Alert.alert('Purchase failed', e?.message ?? 'Please try again.');
    } finally {
      setBusy(false);
    }
  }

  async function doRestore() {
    setRestoring(true);
    try {
      const n = await restorePurchases();
      qc.invalidateQueries({ queryKey: keys.subscription });
      Alert.alert(
        n > 0 ? 'Purchases restored' : 'Nothing to restore',
        n > 0
          ? 'Your subscription is active again.'
          : `No active NeutriAI subscription was found on this ${storeName()} account.`,
      );
      if (n > 0) close();
    } catch (e: any) {
      Alert.alert('Could not restore', e?.message ?? 'Please try again.');
    } finally {
      setRestoring(false);
    }
  }

  const selected = plans?.find((p) => p.id === plan);
  const ordered = [plans?.find((p) => p.id === 'annual'), plans?.find((p) => p.id === 'monthly')]
    .filter(Boolean) as NonNullable<typeof plans>;

  return (
    <Modal visible={open} animationType="slide" onRequestClose={close}>
      <Screen>
        <SafeAreaView style={{ flex: 1 }}>
          <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 60 }}>
            <View>
              <Label>NeutriAI Pro</Label>
              <H1>
                {sub?.is_active
                  ? 'Manage your plan'
                  : `${selected?.trial_days ?? 15} days free`}
              </H1>
              {reason ? <Body dim>{reason}</Body> : null}
            </View>

            {!plans ? <Loading /> : null}

            {ordered.map((p) => {
              const id = p.id as PlanId;
              const active = plan === id;
              return (
                <Card
                  key={p.id}
                  onPress={() => setPlan(id)}
                  style={{
                    borderColor: active ? c.accent : c.border,
                    borderWidth: active ? 2 : 1, gap: space.sm,
                  }}
                >
                  <Row style={{ justifyContent: 'space-between' }}>
                    <View style={{ flex: 1 }}>
                      <H2>{p.name}</H2>
                      {p.savings_note ? (
                        <Text style={[type.caption, { color: c.accent }]}>{p.savings_note}</Text>
                      ) : null}
                    </View>
                    <View style={{ alignItems: 'flex-end' }}>
                      <Text style={[type.h1, { color: c.text }]}>{priceFor(id)}</Text>
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        per {p.interval}
                      </Text>
                    </View>
                  </Row>
                </Card>
              );
            })}

            <Card style={{ gap: space.sm }}>
              <Label>What you get</Label>
              {(selected?.features ?? []).map((f, i) => (
                <Row key={i} gap={space.sm} style={{ alignItems: 'flex-start' }}>
                  <Text style={{ color: c.accent, fontSize: 15 }}>✓</Text>
                  <Text style={[type.body, { color: c.text, flex: 1 }]}>{f}</Text>
                </Row>
              ))}
            </Card>

            {sub?.is_active ? (
              <>
                <Card style={{ gap: space.sm }}>
                  <Label>Current plan</Label>
                  <H2>
                    {sub.tier === 'trial' ? 'Free trial'
                      : sub.plan_interval === 'year' ? 'Annual' : 'Monthly'}
                  </H2>
                  {sub.trial_end ? (
                    <Body dim>Trial ends {new Date(sub.trial_end).toLocaleDateString()}.</Body>
                  ) : null}
                  {sub.current_period_end ? (
                    <Body dim>
                      {sub.cancel_at_period_end ? 'Access ends' : 'Renews'}{' '}
                      {new Date(sub.current_period_end).toLocaleDateString()}.
                    </Body>
                  ) : null}
                </Card>
                <Button
                  title={NATIVE ? `Manage in ${storeName()}` : 'Manage billing'}
                  variant="secondary"
                  onPress={async () => {
                    if (NATIVE) {
                      await WebBrowser.openBrowserAsync(
                        Platform.OS === 'ios'
                          ? 'https://apps.apple.com/account/subscriptions'
                          : 'https://play.google.com/store/account/subscriptions',
                      );
                    } else {
                      const { url } = await api.billing.portal();
                      await WebBrowser.openBrowserAsync(url);
                    }
                  }}
                />
              </>
            ) : (
              <>
                <Button
                  title={`Start ${selected?.trial_days ?? 15}-day free trial`}
                  loading={busy}
                  onPress={startPurchase}
                />
                <Text style={[type.caption, { color: c.textFaint, textAlign: 'center' }]}>
                  {NATIVE
                    ? `Billed through ${storeName()} after the trial. Cancel any time in your ${Platform.OS === 'ios' ? 'Apple ID' : 'Google'} subscription settings.`
                    : 'No card needed to start. Cancel any time in one tap.'}
                </Text>
                {sub ? (
                  <Text style={[type.caption, { color: c.textFaint, textAlign: 'center' }]}>
                    On the free plan you get {sub.ai_scans_quota} AI scans a day —{' '}
                    {sub.ai_scans_used_today} used today.
                  </Text>
                ) : null}
              </>
            )}

            {/* Apple requires this to be present and discoverable. */}
            {NATIVE ? (
              <Button
                title="Restore purchases"
                variant="ghost"
                loading={restoring}
                onPress={doRestore}
              />
            ) : null}

            <Button title="Not now" variant="ghost" onPress={close} />
          </ScrollView>
        </SafeAreaView>
      </Screen>
    </Modal>
  );
}
