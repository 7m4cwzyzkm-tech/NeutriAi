/**
 * Typed API client.
 *
 * One place that knows about auth headers, error shape and retries, so screens
 * never touch fetch directly. Auth tokens come from the Supabase session — the
 * backend verifies them locally, so there is no separate login round trip.
 */
import Constants from 'expo-constants';
import { supabase } from './supabase';
import type {
  AdaptedRecipe, Dashboard, EquipmentScan, Fast, Integration, Meal, Plan,
  PersonalRecord, Post, PricingPlan, Profile, Recipe, ScanResult, Subscription,
  Targets, WaterDay, Workout, WorkoutSetInput,
} from './types';
import { ApiError } from './types';

const BASE = (Constants.expoConfig?.extra?.apiUrl as string) ?? 'http://localhost:8000/v1';

type Options = {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** Vision endpoints legitimately take several seconds. */
  timeoutMs?: number;
  retries?: number;
};

async function authHeader(): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function buildUrl(path: string, query?: Options['query']): string {
  const url = new URL(BASE + path);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
  }
  return url.toString();
}

export async function request<T>(path: string, opts: Options = {}): Promise<T> {
  const { method = 'GET', body, query, timeoutMs = 20000, retries = 1 } = opts;

  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(buildUrl(path, query), {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...(await authHeader()),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      clearTimeout(timer);

      if (res.status === 204) return undefined as T;

      const text = await res.text();
      const json = text ? JSON.parse(text) : null;

      if (!res.ok) {
        const err = json?.error ?? {};
        // 5xx is worth one retry; 4xx never is — the request itself is wrong.
        if (res.status >= 500 && attempt < retries) {
          lastError = new ApiError(res.status, err.code ?? 'server_error', err.message ?? 'Server error');
          await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
          continue;
        }
        throw new ApiError(
          res.status,
          err.code ?? `http_${res.status}`,
          err.message ?? 'Something went wrong.',
          err.detail,
        );
      }
      return json as T;
    } catch (e) {
      clearTimeout(timer);
      if (e instanceof ApiError) throw e;
      lastError = e;
      if (attempt < retries) {
        await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
        continue;
      }
    }
  }
  const message =
    lastError instanceof Error && lastError.name === 'AbortError'
      ? 'That took too long. Check your connection and try again.'
      : 'Could not reach NutriAI.';
  throw new ApiError(0, 'network_error', message);
}

/** Every endpoint, grouped the way the screens use them. */
export const api = {
  profile: {
    get: () => request<Profile>('/me'),
    update: (patch: Partial<Profile>) =>
      request<Profile>('/me', { method: 'PATCH', body: patch }),
    targets: (recompute = false) =>
      request<Targets>('/me/targets', { query: { recompute } }),
    dashboard: (day?: string) => request<Dashboard>('/me/dashboard', { query: { day } }),
    streaks: () => request<Record<string, { current: number; best: number }>>('/me/streaks'),
    history: (days = 30) => request<unknown[]>('/me/history', { query: { days } }),
    restrictions: {
      list: () => request<unknown[]>('/me/restrictions'),
      add: (kind: string, label: string, severity = 'moderate') =>
        request('/me/restrictions', { method: 'POST', body: { kind, label, severity } }),
      remove: (id: string) => request(`/me/restrictions/${id}`, { method: 'DELETE' }),
    },
    logWeight: (weight_kg: number, body_fat_pct?: number) =>
      request('/me/body-metrics', { method: 'POST', body: { weight_kg, body_fat_pct } }),
  },

  nutrition: {
    scan: (payload: {
      image_paths: string[]; meal_slot?: string; calibration_id?: string;
      plate_diameter_mm?: number;
    }) => request<ScanResult>('/scans', { method: 'POST', body: payload, timeoutMs: 60000, retries: 0 }),
    getScan: (id: string) => request<unknown>(`/scans/${id}`),
    meals: (day?: string) => request<Meal[]>('/meals', { query: { day } }),
    createMeal: (body: unknown) => request<Meal>('/meals', { method: 'POST', body }),
    correctMeal: (id: string, body: unknown) =>
      request<Meal>(`/meals/${id}`, { method: 'PATCH', body }),
    deleteMeal: (id: string) => request(`/meals/${id}`, { method: 'DELETE' }),
    assessments: (day?: string) => request<unknown[]>('/assessments', { query: { day } }),
    searchFoods: (q: string) => request<unknown[]>('/foods/search', { query: { q } }),
    calibrations: {
      list: () => request<unknown[]>('/calibrations'),
      add: (body: unknown) => request('/calibrations', { method: 'POST', body }),
    },
  },

  water: {
    day: (day?: string) => request<WaterDay>('/water', { query: { day } }),
    log: (amount_ml: number, container?: string) =>
      request('/water', { method: 'POST', body: { amount_ml, container } }),
    remove: (id: string) => request(`/water/${id}`, { method: 'DELETE' }),
    settings: (patch: unknown) => request('/water/settings', { method: 'PATCH', body: patch }),
  },

  fasting: {
    current: () => request<Fast | null>('/fasts/current'),
    start: (protocol: string, custom_hours?: number) =>
      request<Fast>('/fasts', { method: 'POST', body: { protocol, custom_hours } }),
    end: (id: string, breakMealId?: string) =>
      request<Fast>(`/fasts/${id}/end`, { method: 'POST', query: { break_meal_id: breakMealId } }),
    list: () => request<Fast[]>('/fasts'),
    settings: () => request<unknown>('/fasts/settings'),
    updateSettings: (patch: unknown) =>
      request('/fasts/settings', { method: 'PATCH', body: patch }),
  },

  fitness: {
    logWorkout: (body: { title: string; kind: string; sets: WorkoutSetInput[] } & Record<string, unknown>) =>
      request<Workout>('/workouts', { method: 'POST', body }),
    workouts: (limit = 30) => request<Workout[]>('/workouts', { query: { limit } }),
    deleteWorkout: (id: string) => request(`/workouts/${id}`, { method: 'DELETE' }),
    prs: () => request<PersonalRecord[]>('/personal-records'),
    exercises: (equipment?: string[]) =>
      request<unknown[]>('/exercises', { query: { equipment: equipment?.join(',') } }),
    scanEquipment: (image_paths: string[], space_note?: string) =>
      request<EquipmentScan>('/equipment/scan', {
        method: 'POST', body: { image_paths, space_note }, timeoutMs: 45000, retries: 0,
      }),
    createPlan: (body: unknown) =>
      request<Plan>('/plans', { method: 'POST', body, timeoutMs: 90000, retries: 0 }),
    currentPlan: () => request<Plan | null>('/plans/current'),
    integrations: () => request<Integration[]>('/integrations'),
    connect: (provider: string) =>
      request<{ flow: string; authorize_url?: string; message?: string }>(
        `/integrations/${provider}/connect`,
      ),
    sync: (provider: string) =>
      request(`/integrations/${provider}/sync`, { method: 'POST', timeoutMs: 60000 }),
    disconnect: (provider: string) => request(`/integrations/${provider}`, { method: 'DELETE' }),
    pushHealth: (days: unknown[]) => request('/health/push', { method: 'POST', body: days }),
    healthDays: (days = 14) => request<unknown[]>('/health', { query: { days } }),
  },

  recipes: {
    create: (body: unknown) => request<Recipe>('/recipes', { method: 'POST', body }),
    browse: (params: Record<string, string | number | boolean | undefined>) =>
      request<Recipe[]>('/recipes', { query: params }),
    get: (id: string) => request<Recipe>(`/recipes/${id}`),
    remove: (id: string) => request(`/recipes/${id}`, { method: 'DELETE' }),
    save: (id: string) => request(`/recipes/${id}/save`, { method: 'POST' }),
    unsave: (id: string) => request(`/recipes/${id}/save`, { method: 'DELETE' }),
    adapt: (id: string, body: unknown) =>
      request<AdaptedRecipe>(`/recipes/${id}/adapt`, {
        method: 'POST', body, timeoutMs: 90000, retries: 0,
      }),
    shoppingList: (id: string, servings?: number) =>
      request<unknown>(`/recipes/${id}/shopping-list`, { query: { servings } }),
  },

  social: {
    feed: (scope: 'following' | 'discover' | 'mine' = 'following', offset = 0) =>
      request<Post[]>('/feed', { query: { scope, offset } }),
    post: (body: unknown) => request<Post>('/posts', { method: 'POST', body }),
    deletePost: (id: string) => request(`/posts/${id}`, { method: 'DELETE' }),
    like: (id: string) => request(`/posts/${id}/like`, { method: 'POST' }),
    unlike: (id: string) => request(`/posts/${id}/like`, { method: 'DELETE' }),
    comments: (id: string) => request<unknown[]>(`/posts/${id}/comments`),
    comment: (id: string, body: string) =>
      request(`/posts/${id}/comments`, { method: 'POST', body: { body } }),
    follow: (handle: string) => request(`/users/${handle}/follow`, { method: 'POST' }),
    unfollow: (handle: string) => request(`/users/${handle}/follow`, { method: 'DELETE' }),
    searchUsers: (q: string) => request<unknown[]>('/users/search', { query: { q } }),
    notifications: (unreadOnly = false) =>
      request<unknown[]>('/notifications', { query: { unread_only: unreadOnly } }),
    markRead: (ids?: string[]) =>
      request('/notifications/read', { method: 'POST', body: ids ?? undefined }),
    celebrations: () => request<unknown[]>('/celebrations'),
    markCelebrationSeen: (id: string) =>
      request(`/celebrations/${id}/seen`, { method: 'POST' }),
    motivation: () => request<unknown[]>('/motivation'),
  },

  billing: {
    plans: () => request<PricingPlan[]>('/billing/plans'),
    subscription: () => request<Subscription>('/billing/subscription'),
    checkout: (plan: 'monthly' | 'annual', promo_code?: string) =>
      request<{ url: string; session_id: string; trial_days: number }>('/billing/checkout', {
        method: 'POST', body: { plan, promo_code },
      }),
    portal: () => request<{ url: string }>('/billing/portal', { method: 'POST' }),
  },
};
