/**
 * In-app purchases — StoreKit on iOS, Play Billing on Android.
 *
 * Stripe Checkout for a digital subscription inside a native app violates
 * App Store rule 3.1.1 and Google Play's payments policy. This module is the
 * compliant path; Stripe stays for web.
 *
 * The ordering below is the part that matters and the part most
 * implementations get wrong:
 *
 *   purchase → server verifies → THEN finishTransaction
 *
 * Finishing the transaction before the server confirms means a crash or a
 * dropped connection in between leaves a user who paid with no entitlement and
 * no receipt left to replay. Doing it in this order, the worst case is a
 * transaction that stays pending and gets picked up on next launch.
 */
import { Platform } from 'react-native';
import * as IAP from 'expo-in-app-purchases';
import { request } from '../api/client';

export const PRODUCTS = {
  monthly: 'app.neutriai.pro.monthly',
  annual: 'app.neutriai.pro.annual',
} as const;

export type PlanId = keyof typeof PRODUCTS;

let connected = false;
let listenerAttached = false;

async function connect(): Promise<void> {
  if (connected) return;
  await IAP.connectAsync();
  connected = true;
}

export async function disconnect(): Promise<void> {
  if (!connected) return;
  await IAP.disconnectAsync();
  connected = false;
  listenerAttached = false;
}

/** Verify one purchase server-side and apply the entitlement. */
async function verify(purchase: IAP.InAppPurchase): Promise<void> {
  if (Platform.OS === 'ios') {
    await request('/billing/iap/apple', {
      method: 'POST',
      query: {
        // StoreKit 2 transaction id — not the deprecated base64 receipt blob.
        transaction_id: String(purchase.orderId ?? purchase.transactionId ?? ''),
        sandbox: __DEV__,
      },
      timeoutMs: 30000,
      retries: 1,
    });
  } else {
    await request('/billing/iap/google', {
      method: 'POST',
      query: {
        product_id: purchase.productId,
        purchase_token: String(purchase.purchaseToken ?? ''),
      },
      timeoutMs: 30000,
      retries: 1,
    });
  }
}

/**
 * Attach the purchase listener once, for the app's lifetime.
 *
 * It has to be global rather than per-screen: a purchase interrupted by a
 * crash or a phone call is redelivered on next launch, and if nothing is
 * listening then, the user has paid and we never hear about it.
 */
export function attachPurchaseListener(onEntitlementChanged: () => void): void {
  if (listenerAttached) return;
  listenerAttached = true;

  IAP.setPurchaseListener(async ({ responseCode, results, errorCode }) => {
    if (responseCode === IAP.IAPResponseCode.USER_CANCELED) return;
    if (responseCode !== IAP.IAPResponseCode.OK) {
      console.warn('[iap] purchase failed', { responseCode, errorCode });
      return;
    }
    for (const purchase of results ?? []) {
      if (purchase.acknowledged) continue;
      try {
        await verify(purchase);
        // Only now is it safe to finish: the entitlement is durable server-side.
        await IAP.finishTransactionAsync(purchase, false);
        onEntitlementChanged();
      } catch (err) {
        // Leave the transaction unfinished. The store redelivers it on next
        // launch, and this listener will retry then.
        console.warn('[iap] verification failed, leaving transaction pending', err);
      }
    }
  });
}

export interface StoreProduct {
  productId: string;
  price: string;
  priceAmountMicros?: number;
  currencyCode?: string;
  title?: string;
  description?: string;
}

/** Live prices from the store — never hardcode, they vary by region and tax. */
export async function loadProducts(): Promise<StoreProduct[]> {
  await connect();
  const { responseCode, results } = await IAP.getProductsAsync(Object.values(PRODUCTS));
  if (responseCode !== IAP.IAPResponseCode.OK) return [];
  return (results ?? []) as StoreProduct[];
}

export async function purchase(plan: PlanId): Promise<void> {
  await connect();
  await IAP.purchaseItemAsync(PRODUCTS[plan]);
  // Resolution happens in the listener, not here — that is what makes an
  // interrupted purchase recoverable.
}

/**
 * Restore Purchases. Apple rejects subscription apps that do not offer this.
 * Returns how many entitlements were restored, so the UI can say something
 * truthful rather than a generic "done".
 */
export async function restorePurchases(): Promise<number> {
  await connect();
  const history = await IAP.getPurchaseHistoryAsync();
  const purchases = history.results ?? [];
  let restored = 0;
  for (const p of purchases) {
    if (!Object.values(PRODUCTS).includes(p.productId as never)) continue;
    try {
      await verify(p as IAP.InAppPurchase);
      restored += 1;
    } catch {
      // A lapsed subscription legitimately fails verification — not an error.
    }
  }
  return restored;
}

export function storeName(): string {
  return Platform.OS === 'ios' ? 'the App Store' : 'Google Play';
}
