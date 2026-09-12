/**
 * In-app purchases — StoreKit on iOS, Play Billing on Android.
 *
 * Stripe Checkout for a digital subscription inside a native app violates
 * App Store rule 3.1.1 and Google Play's payments policy. This module is the
 * compliant path; Stripe stays for web.
 *
 * CURRENT STATE — READ THIS BEFORE "FIXING" THE STUBS BELOW
 * --------------------------------------------------------
 * Store billing is not implemented right now, and that is deliberate.
 *
 * expo-in-app-purchases was deprecated by Expo at SDK 48 and never had a
 * release for SDK 52, let alone 57. It was also not safe to merely "lazy
 * require": Metro resolves require('literal-string') at BUILD time wherever
 * the call sits — inside a function, inside a try/catch, it makes no
 * difference. A lazy require defers execution, not resolution, so referencing
 * an uninstalled module that way fails the entire bundle.
 *
 * billingAvailable() therefore returns false, and PaywallScreen falls through
 * to hosted Stripe Checkout in a browser — which works, and is the correct
 * behaviour anywhere that is not a real store build.
 *
 * The previous implementation is preserved verbatim in docs/BILLING_NATIVE.md,
 * along with the ordering constraint that must survive any rewrite:
 *
 *   purchase -> server verifies -> THEN finishTransaction
 *
 * Before store submission this gets rebuilt on a maintained library
 * (react-native-iap or RevenueCat). The server endpoints do not change.
 */
import { Platform } from 'react-native';

export const PRODUCTS = {
  monthly: 'app.neutriai.pro.monthly',
  annual: 'app.neutriai.pro.annual',
} as const;

export type PlanId = keyof typeof PRODUCTS;

/**
 * The shape we consume. Declared here rather than imported from a billing
 * library on purpose — swapping the underlying library should not ripple into
 * the screens.
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

/**
 * Whether native store billing can be used right now.
 *
 * The paywall must consult this before offering a store purchase. Showing a
 * "Subscribe" button that cannot charge anyone is worse than showing the web
 * checkout flow, which works everywhere.
 */
export function billingAvailable(): boolean {
  return false;
}

export const BILLING_UNAVAILABLE_MESSAGE =
  'Store purchases need the full NeutriAI app. Use the web checkout for now.';

/** No-op while billing is unavailable. Safe to call unconditionally. */
export function attachPurchaseListener(_onEntitlementChanged: () => void): void {
  if (!billingAvailable()) return;
}

export async function disconnect(): Promise<void> {
  if (!billingAvailable()) return;
}

/** Live prices from the store — never hardcode, they vary by region and tax. */
export async function loadProducts(): Promise<StoreProduct[]> {
  // Empty, not an error: PaywallScreen falls back to our own price catalogue
  // from the API, which is the right display when the store cannot be asked.
  if (!billingAvailable()) return [];
  return [];
}

export async function purchase(_plan: PlanId): Promise<void> {
  throw new Error(BILLING_UNAVAILABLE_MESSAGE);
}

/**
 * Restore Purchases. Apple rejects subscription apps that do not offer this,
 * so this must be real before submission — it is listed here to keep the API
 * surface honest about what still owes an implementation.
 */
export async function restorePurchases(): Promise<number> {
  throw new Error(BILLING_UNAVAILABLE_MESSAGE);
}

export function storeName(): string {
  return Platform.OS === 'ios' ? 'the App Store' : 'Google Play';
}
