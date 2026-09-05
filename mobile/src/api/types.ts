/** Shapes mirrored from the FastAPI Pydantic models. */

export type MealSlot = 'breakfast' | 'lunch' | 'dinner' | 'snack' | 'pre_workout' | 'post_workout';
export type Goal = 'lose' | 'maintain' | 'gain' | 'recomp';
export type Tier = 'free' | 'trial' | 'pro' | 'pro_annual' | 'comped';

export interface Macros {
  kcal: number; protein_g: number; carbs_g: number;
  fat_g: number; fiber_g: number; sugar_g: number; sodium_mg: number;
}

export interface Profile {
  id: string; handle: string; display_name: string; avatar_url?: string | null;
  bio: string; sex?: 'male' | 'female' | 'other' | null; birth_date?: string | null;
  height_cm?: number | null; weight_kg?: number | null; target_weight_kg?: number | null;
  activity_level: string; goal: Goal; diet_mode: string;
  unit_system: 'metric' | 'imperial'; timezone: string;
  is_private: boolean; onboarded_at?: string | null;
}

export interface Targets {
  bmr_kcal: number; tdee_kcal: number; target_kcal: number;
  protein_g: number; carbs_g: number; fat_g: number;
  fiber_g: number; sugar_g_max: number; water_ml: number;
  rationale: Record<string, unknown>;
}

export interface DetectedItem {
  name: string; cuisine?: string | null;
  grams: number; grams_low?: number | null; grams_high?: number | null;
  estimation_method: string; confidence: number;
  bbox?: { x: number; y: number; w: number; h: number } | null;
  macros: Macros; food_fact_id?: string | null;
}

export interface Assessment {
  severity: 'none' | 'mild' | 'moderate' | 'severe';
  kcal_over: number; pct_of_target: number; carb_load_flag: boolean;
  meal_frequency: number; headline: string; detail: string;
  portion_advice: string[];
  macro_corrections: Record<string, number>;
  next_meal: { target_kcal: number; emphasis: string; suggestions: string[] };
}

export interface ScanResult {
  scan_id: string; meal_id?: string | null; status: string;
  items: DetectedItem[]; totals: Macros;
  overall_confidence: number; confidence_band: 'low' | 'medium' | 'high';
  needs_review: boolean; notes: string[]; latency_ms?: number | null;
  assessment?: Assessment | null;
}

export interface Meal {
  id: string; day: string; meal_slot: MealSlot; title: string; eaten_at: string;
  kcal: number; protein_g: number; carbs_g: number; fat_g: number;
  fiber_g: number; sugar_g: number;
  confidence?: number | null; is_verified: boolean;
  photo_path?: string | null; items: unknown[];
}

export interface DailySummary {
  day: string; kcal_in: number; protein_g: number; carbs_g: number; fat_g: number;
  fiber_g: number; sugar_g: number; kcal_out: number; steps: number;
  water_ml: number; workouts: number; fast_minutes: number; goals_met: string[];
  celebrated_at?: string | null;
}

export interface Dashboard {
  day: string;
  summary: DailySummary | null;
  targets: Targets | null;
  streaks: Record<string, number>;
  active_fast: Fast | null;
  meals: Meal[];
  celebration: Celebration | null;
  unread_notifications: number;
}

export interface Fast {
  id: string; protocol: string; status: string;
  started_at: string; ends_at?: string | null; ended_at?: string | null;
  target_minutes: number; elapsed_minutes: number; remaining_minutes: number;
  pct: number; phase: string; phase_note: string; streak: number;
}

export interface WaterDay {
  day: string; goal_ml: number; total_ml: number; pct: number;
  remaining_ml: number; logs: { id: string; amount_ml: number; logged_at: string }[];
  streak: number; on_pace: boolean;
}

export interface Celebration {
  id: string; kind: string; title: string; subtitle: string;
  animation: 'confetti' | 'fireworks' | 'rings' | 'flame' | 'trophy' | 'wave';
  payload: Record<string, unknown>; seen_at?: string | null;
}

export interface WorkoutSetInput {
  exercise_name: string; exercise_slug?: string; set_index: number;
  reps?: number; weight_kg?: number; duration_s?: number;
  rpe?: number; rest_s?: number; is_warmup?: boolean;
}

export interface Workout {
  id: string; title: string; kind: string; started_at: string;
  ended_at?: string | null; duration_s?: number | null; kcal?: number | null;
  hr_zones: Record<string, number>; sets: unknown[]; new_prs: PersonalRecord[];
}

export interface PersonalRecord {
  exercise_slug: string; metric: string; value: number; unit: string;
  achieved_at?: string;
}

export interface PlanDay {
  id: string; week_index: number; day_index: number; title: string;
  kind: string; est_minutes: number;
  blocks: { slug: string; name: string; sets: number; reps: string;
            rest_s: number; tempo?: string; load_hint?: string; notes?: string }[];
  completed_at?: string | null;
}

export interface Plan {
  id: string; name: string; goal: string; days_per_week: number; weeks: number;
  equipment: string[]; is_calisthenics_fallback: boolean;
  safety_notes: string[]; progression: Record<string, unknown>; days: PlanDay[];
}

export interface EquipmentScan {
  id: string; equipment: string[];
  detected: { equipment: string; detail: string; confidence: number }[];
  confidence: number; fallback_to_calisthenics: boolean;
}

export interface Recipe {
  id: string; author_id: string; title: string; summary: string;
  photo_paths: string[]; cuisine?: string | null; servings: number;
  prep_minutes: number; cook_minutes: number; difficulty: number;
  tags: string[]; steps: { n: number; text: string; minutes?: number }[];
  ingredients: { name: string; raw_text: string; grams: number; kcal: number }[];
  per_serving: Macros; is_ai_generated: boolean;
  adaptation_note?: string | null; like_count: number; save_count: number;
}

export interface AdaptedRecipe {
  recipe_id?: string | null; title: string; adaptation_note: string;
  substitutions: { from: string; to: string; reason: string }[];
  ingredients: Recipe['ingredients']; steps: Recipe['steps'];
  per_serving: Macros; servings: number;
  shopping_list: { name: string; qty: string; aisle: string }[];
  warnings: string[];
}

export interface Post {
  id: string; author: { id: string; handle: string; display_name: string; avatar_url?: string };
  kind: string; body: string; media_paths: string[];
  metrics: Record<string, unknown>;
  like_count: number; comment_count: number; liked_by_me: boolean;
  created_at: string; attached?: Record<string, unknown> | null;
}

export interface Subscription {
  tier: Tier; is_active: boolean; status?: string | null;
  plan_interval?: string | null; amount_cents?: number | null; currency: string;
  trial_end?: string | null; current_period_end?: string | null;
  cancel_at_period_end: boolean; promo_code?: string | null;
  ai_scans_used_today: number; ai_scans_quota: number;
}

export interface PricingPlan {
  id: string; name: string; interval: string; amount_cents: number;
  currency: string; trial_days: number; savings_note?: string | null;
  features: string[];
}

export interface Integration {
  provider: string; supports_pull: boolean; supports_push: boolean;
  oauth_version?: string | null; connected: boolean;
  status?: string; last_sync_at?: string | null; error?: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
  /** True when the fix is "subscribe", so the UI can open the paywall. */
  get needsUpgrade() {
    return this.status === 402 || this.code === 'quota_exceeded';
  }
}
