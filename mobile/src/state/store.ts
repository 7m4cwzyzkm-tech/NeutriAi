/**
 * Global state. Deliberately small: server data lives in React Query, and only
 * things that outlive a screen live here.
 */
import { create } from 'zustand';
import type { Session } from '@supabase/supabase-js';
import type { Celebration, Subscription } from '../api/types';

interface AppState {
  session: Session | null;
  subscription: Subscription | null;
  paywallOpen: boolean;
  paywallReason: string | null;
  celebration: Celebration | null;

  setSession: (s: Session | null) => void;
  setSubscription: (s: Subscription | null) => void;
  openPaywall: (reason?: string) => void;
  closePaywall: () => void;
  showCelebration: (c: Celebration) => void;
  dismissCelebration: () => void;
}

export const useApp = create<AppState>((set) => ({
  session: null,
  subscription: null,
  paywallOpen: false,
  paywallReason: null,
  celebration: null,

  setSession: (session) => set({ session }),
  setSubscription: (subscription) => set({ subscription }),
  openPaywall: (reason) => set({ paywallOpen: true, paywallReason: reason ?? null }),
  closePaywall: () => set({ paywallOpen: false, paywallReason: null }),
  showCelebration: (celebration) => set({ celebration }),
  dismissCelebration: () => set({ celebration: null }),
}));

export const isPro = (s: Subscription | null) => Boolean(s?.is_active);
