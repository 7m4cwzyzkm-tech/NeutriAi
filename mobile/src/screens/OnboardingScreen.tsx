/**
 * Onboarding — the screen `HomeScreen` has been trying to navigate to.
 *
 * Five steps, one decision each. Two things drive the design:
 *
 * 1. **Every field is load-bearing.** Targets cannot be computed without sex,
 *    birth date, height and weight, so nothing here is optional padding. The
 *    step count is short because the maths needs exactly this much.
 * 2. **Show the working at the end.** The final step displays the computed
 *    targets *with* the reasoning — BMR method, activity multiplier, goal
 *    delta. A number a user can interrogate is a number they trust.
 *
 * Progress is saved on every step rather than at the end, so a user who drops
 * out at step 4 does not start over.
 */
import React, { useState } from 'react';
import { Alert, KeyboardAvoidingView, Platform, ScrollView, Text, TextInput, View } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import { useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { keys } from '../hooks/useApi';
import type { Targets } from '../api/types';

const ACTIVITY = [
  { id: 'sedentary', label: 'Sedentary', note: 'Desk job, little deliberate exercise' },
  { id: 'light', label: 'Light', note: '1–3 sessions a week' },
  { id: 'moderate', label: 'Moderate', note: '3–5 sessions a week' },
  { id: 'active', label: 'Active', note: '6–7 sessions a week' },
  { id: 'very_active', label: 'Very active', note: 'Physical job plus training' },
  { id: 'athlete', label: 'Athlete', note: 'Two sessions most days' },
] as const;

const GOALS = [
  { id: 'lose', label: 'Lose fat', note: 'A 20% deficit — the point where adherence and lean mass both hold up' },
  { id: 'maintain', label: 'Maintain', note: 'Eat at maintenance, train for performance' },
  { id: 'gain', label: 'Build muscle', note: 'A 12% surplus, enough to grow without excess fat gain' },
  { id: 'recomp', label: 'Recomposition', note: 'Slight deficit, high protein, gain muscle while losing fat' },
] as const;

const DIETS = [
  { id: 'balanced', label: 'Balanced' }, { id: 'high_protein', label: 'High protein' },
  { id: 'low_carb', label: 'Low carb' }, { id: 'keto', label: 'Keto' },
  { id: 'mediterranean', label: 'Mediterranean' }, { id: 'vegetarian', label: 'Vegetarian' },
  { id: 'vegan', label: 'Vegan' }, { id: 'pescatarian', label: 'Pescatarian' },
  { id: 'paleo', label: 'Paleo' }, { id: 'athlete', label: 'Athlete' },
] as const;

const lbToKg = (lb: number) => lb * 0.45359237;
const inToCm = (inches: number) => inches * 2.54;

export function OnboardingScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const qc = useQueryClient();

  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [imperial, setImperial] = useState(true);

  const [sex, setSex] = useState<'male' | 'female' | 'other' | null>(null);
  const [birth, setBirth] = useState('');            // YYYY-MM-DD
  const [heightFt, setHeightFt] = useState('');
  const [heightIn, setHeightIn] = useState('');
  const [heightCm, setHeightCm] = useState('');
  const [weight, setWeight] = useState('');
  const [activity, setActivity] = useState<string>('moderate');
  const [goal, setGoal] = useState<string>('maintain');
  const [diet, setDiet] = useState<string>('balanced');
  const [targets, setTargets] = useState<Targets | null>(null);

  const heightValue = imperial
    ? inToCm(Number(heightFt || 0) * 12 + Number(heightIn || 0))
    : Number(heightCm || 0);
  const weightValue = imperial ? lbToKg(Number(weight || 0)) : Number(weight || 0);

  const stepValid = [
    Boolean(sex) && /^\d{4}-\d{2}-\d{2}$/.test(birth),
    heightValue >= 60 && heightValue <= 260 && weightValue >= 20 && weightValue <= 400,
    Boolean(activity),
    Boolean(goal),
    true,
  ][step];

  async function saveAndAdvance() {
    setSaving(true);
    try {
      const patch: Record<string, unknown> =
        step === 0 ? { sex, birth_date: birth }
        : step === 1 ? {
            height_cm: Math.round(heightValue * 10) / 10,
            weight_kg: Math.round(weightValue * 10) / 10,
            unit_system: imperial ? 'imperial' : 'metric',
          }
        : step === 2 ? { activity_level: activity }
        : step === 3 ? { goal }
        : { diet_mode: diet };

      await api.profile.update(patch as never);

      if (step === 4) {
        // The backend recomputes targets the moment the profile is complete.
        const t = await api.profile.targets(true);
        setTargets(t);
        qc.invalidateQueries({ queryKey: keys.profile });
        qc.invalidateQueries({ queryKey: keys.targets });
        qc.invalidateQueries({ queryKey: keys.dashboard() });
        setStep(5);
      } else {
        setStep(step + 1);
      }
    } catch (e: any) {
      Alert.alert('Could not save', e?.message ?? 'Please try again.');
    } finally {
      setSaving(false);
    }
  }

  const r = (targets?.rationale ?? {}) as Record<string, unknown>;

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }}>
        <KeyboardAvoidingView
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
          style={{ flex: 1 }}
        >
          {/* progress */}
          <View style={{ flexDirection: 'row', gap: 4, padding: space.lg }}>
            {[0, 1, 2, 3, 4].map((i) => (
              <View
                key={i}
                style={{
                  flex: 1, height: 3, borderRadius: 2,
                  backgroundColor: i <= step ? c.accent : c.surfaceAlt,
                }}
              />
            ))}
          </View>

          <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 40 }}>
            {step === 0 ? (
              <>
                <View>
                  <Label>Step 1 of 5</Label>
                  <H1>About you</H1>
                  <Body dim>
                    Energy needs differ by sex and age. This is the only place NeutriAI asks,
                    and it is used for one thing: the BMR equation.
                  </Body>
                </View>
                <Card style={{ gap: space.md }}>
                  <Label>Sex</Label>
                  <Row gap={space.sm}>
                    {(['male', 'female', 'other'] as const).map((s) => (
                      <Chip key={s} label={s === 'other' ? 'Prefer not to say' : s}
                            active={sex === s} onPress={() => setSex(s)} />
                    ))}
                  </Row>
                  <Label>Date of birth</Label>
                  <TextInput
                    value={birth} onChangeText={setBirth}
                    placeholder="YYYY-MM-DD" placeholderTextColor={c.textFaint}
                    keyboardType="numbers-and-punctuation"
                    style={{
                      backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                      padding: space.lg, color: c.text, fontSize: 16,
                    }}
                  />
                </Card>
              </>
            ) : null}

            {step === 1 ? (
              <>
                <View>
                  <Label>Step 2 of 5</Label>
                  <H1>Height and weight</H1>
                  <Body dim>You can change these any time — targets recompute automatically.</Body>
                </View>
                <Row gap={space.sm}>
                  <Chip label="Imperial" active={imperial} onPress={() => setImperial(true)} />
                  <Chip label="Metric" active={!imperial} onPress={() => setImperial(false)} />
                </Row>
                <Card style={{ gap: space.md }}>
                  <Label>Height</Label>
                  {imperial ? (
                    <Row gap={space.md}>
                      {[
                        { v: heightFt, set: setHeightFt, unit: 'ft' },
                        { v: heightIn, set: setHeightIn, unit: 'in' },
                      ].map((f) => (
                        <View key={f.unit} style={{ flex: 1 }}>
                          <TextInput
                            value={f.v} onChangeText={f.set} keyboardType="number-pad"
                            placeholder={f.unit} placeholderTextColor={c.textFaint}
                            style={{
                              backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                              padding: space.lg, color: c.text, fontSize: 16,
                            }}
                          />
                        </View>
                      ))}
                    </Row>
                  ) : (
                    <TextInput
                      value={heightCm} onChangeText={setHeightCm} keyboardType="number-pad"
                      placeholder="cm" placeholderTextColor={c.textFaint}
                      style={{
                        backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                        padding: space.lg, color: c.text, fontSize: 16,
                      }}
                    />
                  )}
                  <Label>Weight ({imperial ? 'lb' : 'kg'})</Label>
                  <TextInput
                    value={weight} onChangeText={setWeight} keyboardType="decimal-pad"
                    placeholder={imperial ? 'lb' : 'kg'} placeholderTextColor={c.textFaint}
                    style={{
                      backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                      padding: space.lg, color: c.text, fontSize: 16,
                    }}
                  />
                </Card>
              </>
            ) : null}

            {step === 2 ? (
              <>
                <View>
                  <Label>Step 3 of 5</Label>
                  <H1>How active are you?</H1>
                  <Body dim>
                    Be honest rather than aspirational — overstating this is the most common
                    reason calorie targets come out too high.
                  </Body>
                </View>
                {ACTIVITY.map((a) => (
                  <Card key={a.id} onPress={() => setActivity(a.id)}
                        style={{ borderColor: activity === a.id ? c.accent : c.border,
                                 borderWidth: activity === a.id ? 2 : 1 }}>
                    <H2>{a.label}</H2>
                    <Body dim>{a.note}</Body>
                  </Card>
                ))}
              </>
            ) : null}

            {step === 3 ? (
              <>
                <View>
                  <Label>Step 4 of 5</Label>
                  <H1>What are you working toward?</H1>
                </View>
                {GOALS.map((g) => (
                  <Card key={g.id} onPress={() => setGoal(g.id)}
                        style={{ borderColor: goal === g.id ? c.accent : c.border,
                                 borderWidth: goal === g.id ? 2 : 1 }}>
                    <H2>{g.label}</H2>
                    <Body dim>{g.note}</Body>
                  </Card>
                ))}
              </>
            ) : null}

            {step === 4 ? (
              <>
                <View>
                  <Label>Step 5 of 5</Label>
                  <H1>How do you eat?</H1>
                  <Body dim>
                    This shapes the macro split, and the recipe AI uses it when adapting
                    dishes for you.
                  </Body>
                </View>
                <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                  {DIETS.map((d) => (
                    <Chip key={d.id} label={d.label} active={diet === d.id}
                          onPress={() => setDiet(d.id)} />
                  ))}
                </Row>
              </>
            ) : null}

            {step === 5 && targets ? (
              <>
                <View>
                  <Label>You're set up</Label>
                  <H1>Your daily targets</H1>
                </View>
                <Card>
                  <Row style={{ justifyContent: 'space-around' }}>
                    {[
                      ['kcal', targets.target_kcal, c.text],
                      ['protein', `${targets.protein_g}g`, c.protein],
                      ['carbs', `${targets.carbs_g}g`, c.carbs],
                      ['fat', `${targets.fat_g}g`, c.fat],
                    ].map(([l, v, col]) => (
                      <View key={String(l)} style={{ alignItems: 'center' }}>
                        <Text style={[type.h1, { color: col as string }]}>{String(v)}</Text>
                        <Text style={[type.caption, { color: c.textFaint }]}>{String(l)}</Text>
                      </View>
                    ))}
                  </Row>
                </Card>
                <Card style={{ gap: 6 }}>
                  <Label>How we got there</Label>
                  <Text style={[type.body, { color: c.textDim }]}>
                    Your BMR is {targets.bmr_kcal} kcal, calculated with{' '}
                    {String(r.bmr_method ?? '').replace(/_/g, '-')}. Multiplied by{' '}
                    {String(r.activity_multiplier)} for your activity level, that's{' '}
                    {targets.tdee_kcal} kcal to maintain.
                  </Text>
                  <Text style={[type.body, { color: c.textDim }]}>
                    {Number(r.goal_delta_pct ?? 0) === 0
                      ? 'No adjustment — you’re eating at maintenance.'
                      : `${Number(r.goal_delta_pct) > 0 ? 'Adding' : 'Subtracting'} ${Math.abs(Number(r.goal_delta_pct))}% for your goal gives ${targets.target_kcal} kcal.`}
                  </Text>
                  <Text style={[type.body, { color: c.textDim }]}>
                    Protein is set at {String(r.protein_g_per_kg)} g per kg of bodyweight,
                    fat at {String(r.fat_pct_of_kcal)}% of calories, and carbs take the rest.
                  </Text>
                  <Text style={[type.body, { color: c.textDim }]}>
                    Water goal: {(targets.water_ml / 1000).toFixed(1)} L. Fibre: {targets.fiber_g} g.
                  </Text>
                </Card>
                <Button title="Start tracking" onPress={() => nav.navigate('Main', { screen: 'Home' })} />
              </>
            ) : null}
          </ScrollView>

          {step < 5 ? (
            <View style={{ padding: space.lg, gap: space.sm }}>
              <Button
                title={step === 4 ? 'Calculate my targets' : 'Continue'}
                loading={saving}
                disabled={!stepValid}
                onPress={saveAndAdvance}
              />
              {step > 0 ? (
                <Button title="Back" variant="ghost" onPress={() => setStep(step - 1)} />
              ) : null}
            </View>
          ) : null}
        </KeyboardAvoidingView>
      </SafeAreaView>
    </Screen>
  );
}
