/** React Query hooks. Keys are centralized so invalidation is never guesswork. */
import { Alert } from 'react-native';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { ApiError } from '../api/types';
import { useApp } from '../state/store';

export const keys = {
  dashboard: (day?: string) => ['dashboard', day ?? 'today'] as const,
  profile: ['profile'] as const,
  targets: ['targets'] as const,
  meals: (day?: string) => ['meals', day ?? 'today'] as const,
  water: (day?: string) => ['water', day ?? 'today'] as const,
  hydrationSettings: ['water', 'settings'] as const,
  fast: ['fast', 'current'] as const,
  workouts: ['workouts'] as const,
  plan: ['plan', 'current'] as const,
  prs: ['prs'] as const,
  recipes: (params: unknown) => ['recipes', params] as const,
  feed: (scope: string) => ['feed', scope] as const,
  subscription: ['subscription'] as const,
  integrations: ['integrations'] as const,
  notifications: ['notifications'] as const,
};

const GENERIC_ERROR = 'Something went wrong. Please try again.';

/**
 * What to tell a person about a failed request. The backend's own message is
 * used when it was written for people: AppError subclasses (4xx with a real
 * code), the unhandled-500 text ("Something went wrong on our side."), and
 * the client's own network/timeout text (status 0). Not used when it can be
 * technical: upstream_error can embed a provider's raw exception text,
 * validation_error is "Request body failed validation.", and a bare
 * http_404-style code carries Starlette's "Not Found".
 */
export function userMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return GENERIC_ERROR;
  if (
    error.code === 'upstream_error' ||
    error.code === 'validation_error' ||
    error.code.startsWith('http_') ||
    !error.message
  ) {
    return GENERIC_ERROR;
  }
  return error.message;
}

/**
 * onError for mutations that call a paid AI endpoint. A 402/quota error opens
 * the paywall -- exactly as before, and ONLY that, no second alert. Every
 * other failure (network, timeout, 5xx, a 4xx) gets a plain alert: before
 * this, those failed silently, and "couldn't create a workout plan" showed
 * nothing at all. The screens using these mutations leave the alert to this
 * handler, so it is shown once.
 */
function useApiErrorHandler(title: string) {
  const openPaywall = useApp((s) => s.openPaywall);
  return (error: unknown) => {
    if (error instanceof ApiError && error.needsUpgrade) {
      openPaywall(error.message);
      return;
    }
    Alert.alert(title, userMessage(error));
  };
}

export const useDashboard = (day?: string) =>
  useQuery({
    queryKey: keys.dashboard(day),
    queryFn: () => api.profile.dashboard(day),
    staleTime: 30_000,
  });

export const useProfile = () =>
  useQuery({ queryKey: keys.profile, queryFn: api.profile.get, staleTime: 300_000 });

export const useTargets = () =>
  useQuery({ queryKey: keys.targets, queryFn: () => api.profile.targets(), staleTime: 300_000 });

export const useWater = (day?: string) =>
  useQuery({ queryKey: keys.water(day), queryFn: () => api.water.day(day), staleTime: 15_000 });

export const useHydrationSettings = () =>
  useQuery({
    queryKey: keys.hydrationSettings,
    queryFn: api.water.getSettings,
    staleTime: 300_000,
  });

export const useCurrentFast = () =>
  useQuery({
    queryKey: keys.fast,
    queryFn: api.fasting.current,
    // The ring counts up; refresh often enough that it never looks frozen.
    refetchInterval: 60_000,
  });

export const useSubscription = () => {
  const setSubscription = useApp((s) => s.setSubscription);
  return useQuery({
    queryKey: keys.subscription,
    queryFn: async () => {
      const sub = await api.billing.subscription();
      setSubscription(sub);
      return sub;
    },
    staleTime: 60_000,
  });
};

export const useCurrentPlan = () =>
  useQuery({ queryKey: keys.plan, queryFn: api.fitness.currentPlan, staleTime: 300_000 });

export const usePRs = () =>
  useQuery({ queryKey: keys.prs, queryFn: api.fitness.prs, staleTime: 120_000 });

export const useFeed = (scope: 'following' | 'discover' | 'mine') =>
  useQuery({ queryKey: keys.feed(scope), queryFn: () => api.social.feed(scope) });

export const useIntegrations = () =>
  useQuery({ queryKey: keys.integrations, queryFn: api.fitness.integrations });

// --------------------------------------------------------------------------
// Mutations
// --------------------------------------------------------------------------
export function useLogWater() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ml, container }: { ml: number; container?: string }) =>
      api.water.log(ml, container),
    // Optimistic: tapping a glass should move the bar before the network answers.
    onMutate: async ({ ml }) => {
      await qc.cancelQueries({ queryKey: keys.water() });
      const prev = qc.getQueryData<{ total_ml: number; goal_ml: number; pct: number }>(keys.water());
      if (prev) {
        const total = prev.total_ml + ml;
        qc.setQueryData(keys.water(), {
          ...prev,
          total_ml: total,
          pct: Math.min(100, (total / prev.goal_ml) * 100),
          remaining_ml: Math.max(0, prev.goal_ml - total),
        });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(keys.water(), ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: keys.water() });
      qc.invalidateQueries({ queryKey: keys.dashboard() });
    },
  });
}

export function useUpdateHydrationSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<{
      daily_goal_ml: number; reminder_enabled: boolean; reminder_start: string;
      reminder_end: string; reminder_every_min: number; sync_apple_health: boolean;
    }>) => api.water.settings(patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.hydrationSettings }),
  });
}

export function useScanMeal() {
  const qc = useQueryClient();
  const onError = useApiErrorHandler('Could not analyse that');
  return useMutation({
    mutationFn: api.nutrition.scan,
    onError,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.dashboard() });
      qc.invalidateQueries({ queryKey: keys.meals() });
    },
  });
}

export function useLogWorkout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.fitness.logWorkout,
    onSuccess: () => {
      // A workout logged against a plan day sets that day's completed_at
      // server-side, so the plan has to refetch for its "Done" pill to show.
      qc.invalidateQueries({ queryKey: keys.plan });
      qc.invalidateQueries({ queryKey: keys.workouts });
      qc.invalidateQueries({ queryKey: keys.prs });
      qc.invalidateQueries({ queryKey: keys.dashboard() });
    },
  });
}

export function useStartFast() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ protocol, hours }: { protocol: string; hours?: number }) =>
      api.fasting.start(protocol, hours),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.fast }),
  });
}

export function useEndFast() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.fasting.end(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.fast });
      qc.invalidateQueries({ queryKey: keys.dashboard() });
    },
  });
}

export function useAdaptRecipe() {
  const onError = useApiErrorHandler('Could not adapt');
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: unknown }) => api.recipes.adapt(id, body),
    onError,
  });
}

export function useCreatePlan() {
  const qc = useQueryClient();
  const onError = useApiErrorHandler("Couldn't build your plan");
  return useMutation({
    mutationFn: api.fitness.createPlan,
    onError,
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.plan }),
  });
}

export function useToggleLike() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, liked }: { id: string; liked: boolean }) =>
      liked ? api.social.unlike(id) : api.social.like(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['feed'] }),
  });
}
