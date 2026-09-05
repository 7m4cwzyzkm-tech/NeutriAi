/**
 * Meal detail and correction — the other route that had no screen.
 *
 * This is the most valuable screen in the app and it does not look like it.
 * Every correction here is a labelled training example: photo, our estimate,
 * the user's actual answer. `docs/OPTIMIZATION.md` has the query that turns
 * these into tuned priors for `portion.py`. So the editing has to be low
 * friction, or the data never arrives.
 *
 * Design consequences:
 * - Gram values are edited in place, not behind a modal.
 * - The original AI estimate stays visible next to the edited value, so the
 *   user can see what they changed and we can see it too.
 * - Deleting a mis-detected item is one tap.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Alert, ScrollView, Text, TextInput, View } from 'react-native';
import { useNavigation, useRoute } from '@react-navigation/native';
import { useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { confidenceColor, radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, Divider, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { keys } from '../hooks/useApi';
import type { Meal, MealSlot } from '../api/types';

const SLOTS: MealSlot[] = ['breakfast', 'lunch', 'dinner', 'snack', 'pre_workout', 'post_workout'];

const METHOD_LABEL: Record<string, string> = {
  plate_reference: 'Plate reference', depth_model: 'Depth estimate',
  multi_image: 'Multi-angle', pixel_area: 'Pixel area',
  ai_prior: 'Typical serving', user_entered: 'You entered this',
  barcode: 'Barcode',
};

interface EditableItem {
  id?: string;
  name: string;
  grams: number;
  originalGrams: number;
  kcalPerGram: number;
  proteinPerGram: number;
  carbsPerGram: number;
  fatPerGram: number;
  confidence: number;
  method: string;
  removed: boolean;
}

export function MealDetailScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const route = useRoute<any>();
  const qc = useQueryClient();
  const mealId: string | undefined = route.params?.mealId;

  const [meal, setMeal] = useState<Meal | null>(null);
  const [items, setItems] = useState<EditableItem[]>([]);
  const [slot, setSlot] = useState<MealSlot>('snack');
  const [title, setTitle] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!mealId) { setLoading(false); return; }
    (async () => {
      try {
        const meals = await api.nutrition.meals();
        const found = meals.find((m) => m.id === mealId) ?? null;
        setMeal(found);
        setSlot((found?.meal_slot as MealSlot) ?? 'snack');
        setTitle(found?.title ?? '');
        setItems(
          ((found?.items ?? []) as any[]).map((i) => {
            const g = Number(i.grams) || 1;
            return {
              id: i.id,
              name: i.name,
              grams: g,
              originalGrams: g,
              // Store per-gram rates so edits rescale macros without another
              // round trip to the nutrition database.
              kcalPerGram: Number(i.kcal || 0) / g,
              proteinPerGram: Number(i.protein_g || 0) / g,
              carbsPerGram: Number(i.carbs_g || 0) / g,
              fatPerGram: Number(i.fat_g || 0) / g,
              confidence: Number(i.confidence ?? 0.5),
              method: i.estimation_method ?? 'user_entered',
              removed: false,
            };
          }),
        );
      } catch (e: any) {
        Alert.alert('Could not load meal', e?.message ?? '');
      } finally {
        setLoading(false);
      }
    })();
  }, [mealId]);

  const totals = useMemo(
    () =>
      items.filter((i) => !i.removed).reduce(
        (acc, i) => ({
          kcal: acc.kcal + i.kcalPerGram * i.grams,
          protein: acc.protein + i.proteinPerGram * i.grams,
          carbs: acc.carbs + i.carbsPerGram * i.grams,
          fat: acc.fat + i.fatPerGram * i.grams,
        }),
        { kcal: 0, protein: 0, carbs: 0, fat: 0 },
      ),
    [items],
  );

  const dirty = useMemo(
    () =>
      items.some((i) => i.removed || Math.abs(i.grams - i.originalGrams) > 0.5) ||
      slot !== meal?.meal_slot ||
      title !== meal?.title,
    [items, slot, title, meal],
  );

  function setGrams(index: number, raw: string) {
    const value = Math.max(0, Math.min(5000, Number(raw.replace(/[^0-9.]/g, '')) || 0));
    setItems((prev) => prev.map((it, i) => (i === index ? { ...it, grams: value } : it)));
  }

  async function save() {
    if (!mealId) return;
    setSaving(true);
    try {
      await api.nutrition.correctMeal(mealId, {
        title: title || meal?.title || 'Meal',
        meal_slot: slot,
        items: items
          .filter((i) => !i.removed && i.grams > 0)
          .map((i) => ({
            name: i.name,
            grams: i.grams,
            macros: {
              kcal: i.kcalPerGram * i.grams,
              protein_g: i.proteinPerGram * i.grams,
              carbs_g: i.carbsPerGram * i.grams,
              fat_g: i.fatPerGram * i.grams,
              fiber_g: 0, sugar_g: 0, sodium_mg: 0,
            },
          })),
      });
      qc.invalidateQueries({ queryKey: keys.dashboard() });
      qc.invalidateQueries({ queryKey: keys.meals() });
      nav.goBack();
    } catch (e: any) {
      Alert.alert('Could not save', e?.message ?? 'Please try again.');
    } finally {
      setSaving(false);
    }
  }

  function confirmDelete() {
    Alert.alert('Delete this meal?', 'It will be removed from today’s totals.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete', style: 'destructive',
        onPress: async () => {
          await api.nutrition.deleteMeal(mealId!);
          qc.invalidateQueries({ queryKey: keys.dashboard() });
          qc.invalidateQueries({ queryKey: keys.meals() });
          nav.goBack();
        },
      },
    ]);
  }

  if (loading) return <Screen><Loading label="Loading meal" /></Screen>;
  if (!meal) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1, justifyContent: 'center', padding: space.xl, gap: space.lg }}>
          <H1>Meal not found</H1>
          <Body dim>It may have been deleted.</Body>
          <Button title="Go back" onPress={() => nav.goBack()} />
        </SafeAreaView>
      </Screen>
    );
  }

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 140 }}>
          <View>
            <Label>{new Date(meal.eaten_at).toLocaleString([], {
              weekday: 'short', hour: 'numeric', minute: '2-digit',
            })}</Label>
            <TextInput
              value={title} onChangeText={setTitle}
              placeholder="Meal name" placeholderTextColor={c.textFaint}
              style={[type.h1, { color: c.text, paddingVertical: 4 }]}
            />
          </View>

          <Card>
            <Row style={{ justifyContent: 'space-around' }}>
              {[
                ['kcal', Math.round(totals.kcal), c.text],
                ['protein', `${Math.round(totals.protein)}g`, c.protein],
                ['carbs', `${Math.round(totals.carbs)}g`, c.carbs],
                ['fat', `${Math.round(totals.fat)}g`, c.fat],
              ].map(([l, v, col]) => (
                <View key={String(l)} style={{ alignItems: 'center' }}>
                  <Text style={[type.h1, { color: col as string }]}>{String(v)}</Text>
                  <Text style={[type.caption, { color: c.textFaint }]}>{String(l)}</Text>
                </View>
              ))}
            </Row>
            {dirty ? (
              <Text style={[type.caption, { color: c.warn, textAlign: 'center', marginTop: space.sm }]}>
                Unsaved changes
              </Text>
            ) : null}
          </Card>

          <View style={{ gap: space.sm }}>
            <Label>Meal</Label>
            <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
              {SLOTS.map((s) => (
                <Chip key={s} label={s.replace('_', ' ')} active={slot === s} onPress={() => setSlot(s)} />
              ))}
            </Row>
          </View>

          <View style={{ gap: space.md }}>
            <Row style={{ justifyContent: 'space-between' }}>
              <H2>Items</H2>
              <Text style={[type.caption, { color: c.textDim }]}>
                {items.filter((i) => !i.removed).length} of {items.length}
              </Text>
            </Row>
            <Body dim>
              Correcting a portion makes today’s numbers right — and it’s how the estimator
              learns. It’s the single most useful thing you can do here.
            </Body>

            {items.map((item, i) => (
              <Card
                key={i}
                style={{
                  gap: space.md,
                  opacity: item.removed ? 0.4 : 1,
                  borderColor: item.removed ? c.danger : c.border,
                }}
              >
                <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <View style={{ flex: 1 }}>
                    <Text style={[type.body, { color: c.text, fontWeight: '600', textTransform: 'capitalize' }]}>
                      {item.name}
                    </Text>
                    <Row gap={space.xs} style={{ marginTop: 4 }}>
                      <View style={{
                        width: 6, height: 6, borderRadius: 3,
                        backgroundColor: confidenceColor(c, item.confidence),
                      }} />
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        {METHOD_LABEL[item.method] ?? item.method}
                        {item.grams !== item.originalGrams
                          ? `  ·  AI said ${Math.round(item.originalGrams)} g`
                          : ''}
                      </Text>
                    </Row>
                  </View>
                  <Text style={[type.h2, { color: c.text }]}>
                    {Math.round(item.kcalPerGram * item.grams)}
                  </Text>
                </Row>

                <Row gap={space.md}>
                  <View style={{ flex: 1 }}>
                    <Label>Grams</Label>
                    <TextInput
                      value={String(Math.round(item.grams))}
                      onChangeText={(t) => setGrams(i, t)}
                      keyboardType="number-pad"
                      editable={!item.removed}
                      style={{
                        backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                        paddingHorizontal: space.md, paddingVertical: space.sm,
                        color: c.text, fontSize: 17, marginTop: 4,
                        fontVariant: ['tabular-nums'],
                      }}
                    />
                  </View>
                  <View style={{ flex: 2, gap: 4 }}>
                    <Label>Quick adjust</Label>
                    <Row gap={space.xs}>
                      {[0.5, 0.75, 1.25, 1.5, 2].map((mult) => (
                        <Chip
                          key={mult}
                          label={`${mult}×`}
                          onPress={() =>
                            setItems((prev) =>
                              prev.map((it, idx) =>
                                idx === i
                                  ? { ...it, grams: Math.round(it.originalGrams * mult) }
                                  : it,
                              ),
                            )
                          }
                        />
                      ))}
                    </Row>
                  </View>
                </Row>

                <Row gap={space.lg}>
                  <Text style={[type.caption, { color: c.protein }]}>
                    P {Math.round(item.proteinPerGram * item.grams)}g
                  </Text>
                  <Text style={[type.caption, { color: c.carbs }]}>
                    C {Math.round(item.carbsPerGram * item.grams)}g
                  </Text>
                  <Text style={[type.caption, { color: c.fat }]}>
                    F {Math.round(item.fatPerGram * item.grams)}g
                  </Text>
                  <View style={{ flex: 1 }} />
                  <Text
                    onPress={() =>
                      setItems((prev) =>
                        prev.map((it, idx) => (idx === i ? { ...it, removed: !it.removed } : it)),
                      )
                    }
                    style={[type.caption, { color: item.removed ? c.accent : c.danger, fontWeight: '600' }]}
                  >
                    {item.removed ? 'Undo' : 'Not on my plate'}
                  </Text>
                </Row>
              </Card>
            ))}
          </View>

          <Divider />
          <Button title="Delete this meal" variant="ghost" onPress={confirmDelete} />
        </ScrollView>

        {dirty ? (
          <View style={{
            position: 'absolute', bottom: 0, left: 0, right: 0,
            padding: space.lg, backgroundColor: c.surface,
            borderTopWidth: 1, borderTopColor: c.border,
          }}>
            <SafeAreaView edges={['bottom']}>
              <Button title="Save corrections" loading={saving} onPress={save} />
            </SafeAreaView>
          </View>
        ) : null}
      </SafeAreaView>
    </Screen>
  );
}
