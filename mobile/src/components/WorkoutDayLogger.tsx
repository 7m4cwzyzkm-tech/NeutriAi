/**
 * Log what was actually done on one plan day, prefilled from what the plan
 * prescribed. Submits through the existing POST /workouts (useLogWorkout)
 * with plan_day_id, which is what marks the day completed server-side.
 *
 * Deliberately small: per exercise a done/skipped toggle and, per set, reps
 * (or seconds, for a timed hold) and weight. RPE, rest and warm-up exist on
 * WorkoutSetInput but were not asked for, so they go out as defaults.
 */
import React, { useState } from 'react';
import { Alert, Pressable, Text, TextInput, View } from 'react-native';
import { radius, space, type, useTheme } from '../theme';
import { Button, Row } from './Primitives';
import { api } from '../api/client';
import { useLogWorkout } from '../hooks/useApi';
import { useApp } from '../state/store';
import type { Celebration, PersonalRecord, PlanDay, WorkoutSetInput } from '../api/types';

type Block = PlanDay['blocks'][number];

interface SetDraft { amount: string; weight: string }
interface BlockDraft { done: boolean; timed: boolean; sets: SetDraft[] }

/**
 * The plan's `reps` is free text from the plan generator: "8", "8-10",
 * "AMRAP", "30s", "45 sec". A timed prescription logs seconds, not reps;
 * otherwise the FIRST number is the default (the low end of a range -- the
 * user edits it up if they did more). No number means no default.
 */
function parseReps(reps: string): { timed: boolean; amount: string } {
  const timed = /\d+\s*(s|sec|secs|seconds)\b/i.test(reps);
  const n = reps.match(/\d+/);
  return { timed, amount: n ? n[0] : '' };
}

/**
 * `load_hint` is also free text ("RPE 7", "bodyweight", "70% 1RM",
 * "20 kg"). Only an explicit kilogram figure is a weight we can prefill;
 * anything else is guidance, not a number, and stays blank.
 */
function parseKg(hint?: string): string {
  const m = hint?.match(/(\d+(?:\.\d+)?)\s*kg\b/i);
  return m ? m[1] : '';
}

function initialDraft(blocks: Block[]): BlockDraft[] {
  return blocks.map((b) => {
    const { timed, amount } = parseReps(String(b.reps ?? ''));
    const weight = parseKg(b.load_hint);
    const n = Math.max(1, Math.min(20, Math.round(Number(b.sets) || 1)));
    return { done: true, timed, sets: Array.from({ length: n }, () => ({ amount, weight })) };
  });
}

function toNumber(s: string): number | undefined {
  const v = parseFloat(s.replace(',', '.'));
  return Number.isFinite(v) && v >= 0 ? v : undefined;
}

export interface LoggedSummary { exercises: number; sets: number; prs: PersonalRecord[] }

/** The form itself; the Train screen decides when it is showing. */
export function WorkoutDayLogger({ day, onCancel, onDone }: {
  day: PlanDay;
  onCancel: () => void;
  onDone: (summary: LoggedSummary) => void;
}) {
  const c = useTheme();
  const logWorkout = useLogWorkout();
  const showCelebration = useApp((s) => s.showCelebration);
  const [draft, setDraft] = useState<BlockDraft[]>(() => initialDraft(day.blocks));

  function updateSet(bi: number, si: number, patch: Partial<SetDraft>) {
    setDraft((d) => d.map((b, i) => (i !== bi ? b : {
      ...b, sets: b.sets.map((s, j) => (j === si ? { ...s, ...patch } : s)),
    })));
  }

  function toggle(bi: number) {
    setDraft((d) => d.map((b, i) => (i === bi ? { ...b, done: !b.done } : b)));
  }

  function submit() {
    const sets: WorkoutSetInput[] = [];
    let exercises = 0;
    draft.forEach((b, bi) => {
      if (!b.done) return;
      exercises += 1;
      const block = day.blocks[bi];
      b.sets.forEach((s, si) => {
        const amount = toNumber(s.amount);
        sets.push({
          exercise_name: block.name,
          exercise_slug: block.slug || undefined,
          set_index: si + 1,
          reps: !b.timed && amount !== undefined ? Math.round(amount) : undefined,
          duration_s: b.timed && amount !== undefined ? Math.round(amount) : undefined,
          weight_kg: toNumber(s.weight),
          rest_s: block.rest_s || undefined,
          is_warmup: false,
        });
      });
    });
    if (!sets.length) {
      Alert.alert('Nothing to log', 'Mark at least one exercise as done.');
      return;
    }

    logWorkout.mutate(
      {
        title: day.title,
        kind: day.kind,
        plan_day_id: day.id,
        // No duration: the plan's est_minutes is a guess, not what happened.
        sets,
      },
      {
        onSuccess: async (workout) => {
          const prs = workout.new_prs ?? [];
          onDone({ exercises, sets: sets.length, prs });
          if (!prs.length) return;
          // The backend already recorded a "pr" celebration (celebrate() in
          // log_workout). Show THAT row through the app's one overlay, the
          // same way HomeScreen shows dashboard.celebration -- so it respects
          // the user's celebration setting (no row if they turned it off)
          // and is marked seen once, not shown again on Home.
          try {
            const unseen = (await api.social.celebrations()) as Celebration[];
            const pr = unseen.find((x) => x.kind === 'pr');
            if (pr) showCelebration(pr);
          } catch {
            // The PR is still listed on this screen; the animation is extra.
          }
        },
        onError: (e: any) => Alert.alert("Couldn't save the workout", e?.message ?? 'Try again.'),
      },
    );
  }

  const input = {
    backgroundColor: c.surfaceAlt, borderRadius: radius.sm, color: c.text,
    paddingVertical: 6, paddingHorizontal: space.sm, minWidth: 64, textAlign: 'center' as const,
    fontVariant: ['tabular-nums' as const],
  };

  return (
    <View style={{ gap: space.md }}>
      {day.blocks.map((block, bi) => {
        const b = draft[bi];
        return (
          <View key={bi} style={{ gap: space.xs }}>
            <Pressable onPress={() => toggle(bi)}>
              <Row style={{ justifyContent: 'space-between' }}>
                <Text style={[type.body, { color: b.done ? c.text : c.textFaint, flex: 1 }]}>
                  {block.name}
                </Text>
                <Text style={{ color: b.done ? c.accent : c.textFaint, fontSize: 13, fontWeight: '600' }}>
                  {b.done ? '✓ Done' : 'Skipped'}
                </Text>
              </Row>
            </Pressable>
            {b.done
              ? b.sets.map((s, si) => (
                  <Row key={si} gap={space.sm}>
                    <Text style={[type.caption, { color: c.textDim, width: 44 }]}>Set {si + 1}</Text>
                    <TextInput
                      value={s.amount}
                      onChangeText={(t) => updateSet(bi, si, { amount: t })}
                      keyboardType="number-pad"
                      placeholder={b.timed ? 'sec' : 'reps'}
                      placeholderTextColor={c.textFaint}
                      style={input}
                    />
                    <Text style={[type.caption, { color: c.textDim }]}>{b.timed ? 'sec' : 'reps'} ×</Text>
                    <TextInput
                      value={s.weight}
                      onChangeText={(t) => updateSet(bi, si, { weight: t })}
                      keyboardType="decimal-pad"
                      placeholder="—"
                      placeholderTextColor={c.textFaint}
                      style={input}
                    />
                    <Text style={[type.caption, { color: c.textDim }]}>kg</Text>
                  </Row>
                ))
              : null}
          </View>
        );
      })}
      <Row gap={space.md}>
        <Button
          title="Cancel"
          variant="ghost"
          disabled={logWorkout.isPending}
          style={{ flex: 1 }}
          onPress={onCancel}
        />
        <Button title="Save workout" loading={logWorkout.isPending} style={{ flex: 1 }} onPress={submit} />
      </Row>
    </View>
  );
}
