# Native in-app purchases — reference implementation

This is the StoreKit / Play Billing implementation that shipped in
`mobile/src/native/purchases.ts`, preserved verbatim.

It was removed from the live module because `expo-in-app-purchases` was
deprecated by Expo at SDK 48 and has no release for SDK 52, let alone 57.
Billing must be rebuilt on a maintained library (react-native-iap or
RevenueCat) before store submission.

## The one thing that must survive the rewrite

The ordering:

    purchase -> server verifies -> THEN finishTransaction

Finishing the transaction before the server confirms means a crash or a
dropped connection in between leaves a user who paid with no entitlement and
no receipt left to replay. In this order the worst case is a transaction that
stays pending and gets picked up on next launch.

The listener is global for the same reason: a purchase interrupted by a crash
or a phone call is redelivered on next launch, and if nothing is listening
then, the user has paid and we never hear about it.

Server endpoints are unchanged by the library swap:
`POST /billing/iap/apple` (transaction_id, sandbox) and
`POST /billing/iap/google` (product_id, purchase_token).

```ts
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
 *   purchase -> server verifies -> THEN finishTransaction
 *
 * Finishing the transaction before the server confirms means a crash or a
 * dropped connection in between leaves a user who paid with no entitlement and
 * no receipt left to replay. Doing it in this order, the worst case is a
 * transaction that stays pending and gets picked up on next launch.
 *
 * WHY THE MODULE IS LOADED LAZILY
 * -------------------------------
 * The store billing library is a native module. It does not exist in Expo Go,
 * and it may not be installed at all in a given build. A top-level
 * `import * as IAP` therefore throws while the bundle is being evaluated —
 * before any screen renders, before any try/catch of ours runs — and takes the
 * entire app down, not just the paywall.
 *
 * So it is required on first use, once, and cached. When it is absent,
 * `billingAvailable()` returns false and the paywall falls back to Stripe web
 * checkout, which is the correct behaviour anywhere that is not a real
 * store build.
 */
import { Platform } from 'react-native';
import { request } from '../api/client';

export const PRODUCTS = {
  monthly: 'app.neutriai.pro.monthly',
  annual: 'app.neutriai.pro.annual',
} as const;

export type PlanId = keyof typeof PRODUCTS;

/**
 * The shape we actually consume. Declared here rather than imported so this
 * module does not need the billing package present just to typecheck — and so
 * swapping the underlying library does not ripple into the screens.
 */
export interface StorePurchase {
  productId: string;
  orderId?: string;
  transactionId?: string;
  purchaseToken?: string;
  acknowledged?: boolean;
}

export interface StoreProduct {
  productId: string;
  price: string;
  priceAmountMicros?: number;
  currencyCode?: string;
  title?: string;
  description?: string;
}

// ---------------------------------------------------------------------------
// Lazy, failure-tolerant module load
// ---------------------------------------------------------------------------

type IAPModule = any;

let iapModule: IAPModule | null = null;
let iapResolved = false;

function loadIAP(): IAPModule | null {
  if (iapResolved) return iapModule;
  iapResolved = true;
  if (Platform.OS !== 'ios' && Platform.OS !== 'android') {
    iapModule = null;
    return null;
  }
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require('expo-in-app-purchases');
    // A module that resolves but has no connectAsync is a stub or a version
    // whose API moved; treating it as present would fail later and less
    // legibly than treating it as absent now.
    iapModule = mod && typeof mod.connectAsync === 'function' ? mod : null;
  } catch {
    iapModule = null;
  }
  if (!iapModule) {
    console.warn('[iap] store billing unavailable in this build; using web checkout');
  }
  return iapModule;
}

/**
 * Whether native store billing can be used right now.
 *
 * The paywall must consult this before offering a store purchase. Showing a
 * "Subscribe" button that cannot charge anyone is worse than showing the web
 * checkout flow, which works everywhere.
 */
export function billingAvailable(): boolean {
  return loadIAP() !== null;
}

function requireIAP(): IAPModule {
  const mod = loadIAP();
  if (!mod) {
    throw new Error('In-app purchases are not available in this build.');
  }
  return mod;
}

let connected = false;
let listenerAttached = false;

async function connect(): Promise<void> {
  if (connected) return;
  await requireIAP().connectAsync();
  connected = true;
}

export async function disconnect(): Promise<void> {
  if (!connected) return;
  const mod = loadIAP();
  if (mod) await mod.disconnectAsync();
  connected = false;
  listenerAttached = false;
}

/** Verify one purchase server-side and apply the entitlement. */
async function verify(purchase: StorePurchase): Promise<void> {
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
 *
 * Safe to call unconditionally — it is a no-op where billing is unavailable.
 */
export function attachPurchaseListener(onEntitlementChanged: () => void): void {
  if (listenerAttached) return;
  const IAP = loadIAP();
  if (!IAP) return;
  listenerAttached = true;

  IAP.setPurchaseListener(async ({ responseCode, results, errorCode }: any) => {
    if (responseCode === IAP.IAPResponseCode.USER_CANCELED) return;
    if (responseCode !== IAP.IAPResponseCode.OK) {
      console.warn('[iap] purchase failed', { responseCode, errorCode });
      return;
    }
    for (const purchase of (results ?? []) as StorePurchase[]) {
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

/** Live prices from the store — never hardcode, they vary by region and tax. */
export async function loadProducts(): Promise<StoreProduct[]> {
  const IAP = loadIAP();
  if (!IAP) return [];
  await connect();
  const { responseCode, results } = await IAP.getProductsAsync(Object.values(PRODUCTS));
  if (responseCode !== IAP.IAPResponseCode.OK) return [];
  return (results ?? []) as StoreProduct[];
}

export async function purchase(plan: PlanId): Promise<void> {
  const IAP = requireIAP();
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
  const IAP = requireIAP();
  await connect();
  const history = await IAP.getPurchaseHistoryAsync();
  const purchases = (history.results ?? []) as StorePurchase[];
  let restored = 0;
  for (const p of purchases) {
    if (!Object.values(PRODUCTS).includes(p.productId as never)) continue;
    try {
      await verify(p);
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
```
