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
 * - A food the scan missed is added inline too: name, grams, done.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Alert, ScrollView, Text, TextInput, View } from 'react-native';
import { useNavigation, useRoute } from '@react-navigation/native';
import { useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { confidenceColor, radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, Divider, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { keys, useProfile } from '../hooks/useApi';
import type { Meal, MealSlot } from '../api/types';
import { methodLabel } from '../lib/method';

const SLOTS: MealSlot[] = ['breakfast', 'lunch', 'dinner', 'snack', 'pre_workout', 'post_workout'];

/** Typed grams, clamped to what the backend accepts (0 < grams <= 5000). */
function parseGrams(raw: string): number {
  return Math.max(0, Math.min(5000, Number(raw.replace(/[^0-9.]/g, '')) || 0));
}

interface EditableItem {
  id?: string;
  name: string;
  grams: number;
  originalGrams: number;
  kcalPerGram: number;
  proteinPerGram: number;
  carbsPerGram: number;
  fatPerGram: number;
  // Carried even though the screen never displays them.
  //
  // Correcting one gram value used to send fiber, sugar and sodium as zero,
  // and the backend takes a supplied macro block over its own lookup -- so
  // editing a meal silently dropped the day's fibre and made the next scan's
  // "fibre is N g short" advice wrong. Nothing on screen said it happened,
  // because the header shows calories and those were right.
  fiberPerGram: number;
  sugarPerGram: number;
  sodiumPerGram: number;
  // Where this sat in the scan, fixed at load time. NOT its position in the
  // edited list: removing a row shifts everything below it, and a correction
  // paired against the wrong detection teaches the wrong food's height.
  //
  // Undefined for a food the user ADDED ("Add a food"): it is an edit of no
  // detection. The backend's `source_index` is `ge=0`, so a sentinel like -1
  // would be refused, and any real index would pair the new food with a
  // detection it has nothing to do with. Undefined drops the key from the
  // request, which is exactly "an edit of nothing".
  sourceIndex?: number;
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
  const [verifying, setVerifying] = useState(false);
  // Same cached profile query the rest of the app uses; is_tester is set by
  // hand in Supabase for invited weighed-verification testers.
  const { data: profile } = useProfile();
  const isTester = !!profile?.is_tester;

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
          ((found?.items ?? []) as any[]).map((i, index) => {
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
              fiberPerGram: Number(i.fiber_g || 0) / g,
              sugarPerGram: Number(i.sugar_g || 0) / g,
              sodiumPerGram: Number(i.sodium_mg || 0) / g,
              sourceIndex: index,
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

  // An added row starts with grams === originalGrams, so the gram check
  // alone would leave it unsaved and hide the Save button.
  const dirty = useMemo(
    () =>
      items.some((i) => i.removed || i.sourceIndex === undefined || Math.abs(i.grams - i.originalGrams) > 0.5) ||
      slot !== meal?.meal_slot ||
      title !== meal?.title,
    [items, slot, title, meal],
  );

  function setGrams(index: number, raw: string) {
    setItems((prev) => prev.map((it, i) => (i === index ? { ...it, grams: parseGrams(raw) } : it)));
  }

  // "Add a food": the scan missed something entirely. Inline, like every
  // other edit on this screen -- two fields and a button, no modal.
  const [newName, setNewName] = useState('');
  const [newGrams, setNewGrams] = useState('');
  const canAdd = newName.trim().length > 0 && parseGrams(newGrams) > 0;

  function addItem() {
    if (!canAdd) return;
    const g = parseGrams(newGrams);
    setItems((prev) => [
      ...prev,
      {
        name: newName.trim(),
        grams: g,
        // Equal, so it never reads "AI said X g" -- there was no AI estimate.
        originalGrams: g,
        // Unknown, not zero: this screen does not look foods up. The backend
        // resolves the name on save (see save()), and those are the numbers
        // stored. Until then this row shows no macros.
        kcalPerGram: 0,
        proteinPerGram: 0,
        carbsPerGram: 0,
        fatPerGram: 0,
        fiberPerGram: 0,
        sugarPerGram: 0,
        sodiumPerGram: 0,
        sourceIndex: undefined,
        confidence: 1,
        method: 'user_entered',
        removed: false,
      },
    ]);
    setNewName('');
    setNewGrams('');
  }

  async function save() {
    if (!mealId) return;
    setSaving(true);
    try {
      await api.nutrition.correctMeal(mealId, {
        title: title || meal?.title || 'Meal',
        meal_slot: slot,
        // No `notes` key here, deliberately -- this screen has nothing to
        // send. `Meal` (api/types.ts) has no `notes` field because
        // MealOut (backend/app/models/nutrition.py) never returns one, even
        // though MealIn accepts it and the meals table stores it: a GET
        // never round-trips it to this screen, so there is no existing
        // value to preserve here, only one to avoid inventing.
        // Omitting the key is also the CORRECT fix, not a gap: the backend's
        // own `notes_update()` (routers/scans.py:27-39) uses
        // `model_fields_set` to update notes only when the client's JSON
        // body actually contains the key -- a body that never mentions it
        // leaves the stored value untouched. Sending `notes: null` here
        // would have cleared it on every correction instead.
        items: items
          .filter((i) => !i.removed && i.grams > 0)
          .map((i) =>
            i.sourceIndex === undefined
              // Added by the user: name and grams only. No source_index (an
              // edit of no detection) and NO macros block -- correct_meal
              // looks nutrition up only when `macros` is absent and stores any
              // block it is sent as-is, so this row's unknown zeros would have
              // been saved as a 0 kcal food.
              ? { name: i.name, grams: i.grams }
              : {
                  name: i.name,
                  grams: i.grams,
                  // Which detected item this edits. Without it a RENAME cannot be
                  // matched back to the scan -- the name is the thing that changed --
                  // so the app learned nothing from the single most useful
                  // correction a person can make.
                  source_index: i.sourceIndex,
                  macros: {
                    kcal: i.kcalPerGram * i.grams,
                    protein_g: i.proteinPerGram * i.grams,
                    carbs_g: i.carbsPerGram * i.grams,
                    fat_g: i.fatPerGram * i.grams,
                    fiber_g: i.fiberPerGram * i.grams,
                    sugar_g: i.sugarPerGram * i.grams,
                    sodium_mg: i.sodiumPerGram * i.grams,
                  },
                },
          ),
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

  // "Matches what I weighed": the tester weighed each food before plating it
  // and the scan already equals their scale, so there is nothing to type.
  // Records the match for the bias figure and marks the meal verified; it
  // changes no item. The server refuses this for non-testers too.
  async function verify() {
    if (!mealId) return;
    setVerifying(true);
    try {
      await api.nutrition.verifyMeal(mealId);
      qc.invalidateQueries({ queryKey: keys.dashboard() });
      qc.invalidateQueries({ queryKey: keys.meals() });
      nav.goBack();
    } catch (e: any) {
      Alert.alert('Could not confirm', e?.message ?? 'Please try again.');
    } finally {
      setVerifying(false);
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

            {items.map((item, i) => {
              const added = item.sourceIndex === undefined;
              return (
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
                          {methodLabel(item.method)}
                          {!added && item.grams !== item.originalGrams
                            ? `  ·  AI said ${Math.round(item.originalGrams)} g`
                            : ''}
                        </Text>
                      </Row>
                    </View>
                    {/* An added food's nutrition is unknown until the backend
                        looks it up on save; a dash, not a made-up 0. */}
                    <Text style={[type.h2, { color: added ? c.textFaint : c.text }]}>
                      {added ? '—' : Math.round(item.kcalPerGram * item.grams)}
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
                    {added ? (
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        Nutrition is looked up when you save
                      </Text>
                    ) : (
                      <>
                        <Text style={[type.caption, { color: c.protein }]}>
                          P {Math.round(item.proteinPerGram * item.grams)}g
                        </Text>
                        <Text style={[type.caption, { color: c.carbs }]}>
                          C {Math.round(item.carbsPerGram * item.grams)}g
                        </Text>
                        <Text style={[type.caption, { color: c.fat }]}>
                          F {Math.round(item.fatPerGram * item.grams)}g
                        </Text>
                      </>
                    )}
                    <View style={{ flex: 1 }} />
                    {/* A detection is struck through, with Undo, so the
                        correction still says the scan saw it. A food the user
                        added has nothing to undo against: it just goes. */}
                    <Text
                      onPress={() =>
                        setItems((prev) =>
                          added
                            ? prev.filter((_, idx) => idx !== i)
                            : prev.map((it, idx) => (idx === i ? { ...it, removed: !it.removed } : it)),
                        )
                      }
                      style={[type.caption, { color: item.removed ? c.accent : c.danger, fontWeight: '600' }]}
                    >
                      {added ? 'Remove' : item.removed ? 'Undo' : 'Not on my plate'}
                    </Text>
                  </Row>
                </Card>
              );
            })}

            {/* The scan can only be corrected on what it found. This is the
                way to log what it missed. */}
            <Card style={{ gap: space.md }}>
              <Label>Add a food the scan missed</Label>
              <Row gap={space.md}>
                <TextInput
                  value={newName}
                  onChangeText={setNewName}
                  placeholder="e.g. garlic bread"
                  placeholderTextColor={c.textFaint}
                  maxLength={120}
                  returnKeyType="done"
                  style={{
                    flex: 2, backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                    paddingHorizontal: space.md, paddingVertical: space.sm,
                    color: c.text, fontSize: 17,
                  }}
                />
                <TextInput
                  value={newGrams}
                  onChangeText={setNewGrams}
                  placeholder="grams"
                  placeholderTextColor={c.textFaint}
                  keyboardType="number-pad"
                  maxLength={4}
                  style={{
                    flex: 1, backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                    paddingHorizontal: space.md, paddingVertical: space.sm,
                    color: c.text, fontSize: 17, fontVariant: ['tabular-nums'],
                  }}
                />
              </Row>
              <Button title="Add a food" variant="secondary" disabled={!canAdd} onPress={addItem} />
            </Card>
          </View>

          <Divider />
          <Button title="Delete this meal" variant="ghost" onPress={confirmDelete} />
        </ScrollView>

        {/* One bar, two tester outcomes: edited -> save the scale readings as
            corrections; untouched -> confirm the scan matched the scale.
            Non-testers only ever see Save corrections, as before. */}
        {dirty || (isTester && meal && !meal.is_verified) ? (
          <View style={{
            position: 'absolute', bottom: 0, left: 0, right: 0,
            padding: space.lg, backgroundColor: c.surface,
            borderTopWidth: 1, borderTopColor: c.border,
          }}>
            <SafeAreaView edges={['bottom']}>
              {dirty ? (
                <Button title="Save corrections" loading={saving} onPress={save} />
              ) : (
                <Button title="Matches what I weighed" loading={verifying} onPress={verify} />
              )}
            </SafeAreaView>
          </View>
        ) : null}
      </SafeAreaView>
    </Screen>
  );
}
