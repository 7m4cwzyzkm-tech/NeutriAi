/** Recipe browsing, posting, and one-tap AI personalization. */
import React, { useState } from 'react';
import { Alert, FlatList, Modal, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, Divider, Empty, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { useAdaptRecipe } from '../hooks/useApi';
import { useQuery } from '@tanstack/react-query';
import type { AdaptedRecipe, Recipe } from '../api/types';

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'saved', label: 'Saved' },
  { id: 'mine', label: 'Mine' },
] as const;

export function RecipesScreen() {
  const c = useTheme();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]['id']>('all');
  const [selected, setSelected] = useState<Recipe | null>(null);
  const [adapted, setAdapted] = useState<AdaptedRecipe | null>(null);
  const adapt = useAdaptRecipe();

  const { data, isLoading, refetch, isRefetching } = useQuery({
    queryKey: ['recipes', filter],
    queryFn: () =>
      api.recipes.browse({
        saved: filter === 'saved' || undefined,
        mine: filter === 'mine' || undefined,
      }),
  });

  async function personalize(recipe: Recipe) {
    try {
      const result = await adapt.mutateAsync({
        id: recipe.id,
        body: {
          honor_allergies: true,
          honor_diet_mode: true,
          consider_fasting_window: true,
          consider_workout_load: true,
          save_as_fork: true,
        },
      });
      setAdapted(result);
    } catch (e: any) {
      if (!e?.needsUpgrade) Alert.alert('Could not adapt', e?.message ?? 'Try again.');
    }
  }

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <View style={{ padding: space.lg, gap: space.md }}>
          <View>
            <Label>Recipes</Label>
            <H1>Cook something</H1>
          </View>
          <Row gap={space.sm}>
            {FILTERS.map((f) => (
              <Chip key={f.id} label={f.label} active={filter === f.id} onPress={() => setFilter(f.id)} />
            ))}
          </Row>
        </View>

        {isLoading ? (
          <Loading />
        ) : (
          <FlatList
            data={data ?? []}
            keyExtractor={(r) => r.id}
            refreshing={isRefetching}
            onRefresh={refetch}
            contentContainerStyle={{ padding: space.lg, paddingTop: 0, gap: space.md, paddingBottom: 120 }}
            renderItem={({ item }) => (
              <Card onPress={() => setSelected(item)} style={{ gap: space.sm }}>
                <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <View style={{ flex: 1 }}>
                    <Text style={[type.h2, { color: c.text }]}>{item.title}</Text>
                    <Text style={[type.caption, { color: c.textDim, marginTop: 2 }]}>
                      {item.prep_minutes + item.cook_minutes} min · serves {item.servings}
                      {item.cuisine ? ` · ${item.cuisine}` : ''}
                    </Text>
                  </View>
                  {item.is_ai_generated ? (
                    <View style={{ paddingHorizontal: 8, paddingVertical: 3, borderRadius: radius.pill, backgroundColor: c.accent + '22' }}>
                      <Text style={{ color: c.accent, fontSize: 11, fontWeight: '600' }}>adapted</Text>
                    </View>
                  ) : null}
                </Row>
                <Row gap={space.lg}>
                  <Text style={[type.caption, { color: c.text }]}>
                    {Math.round(item.per_serving.kcal)} kcal
                  </Text>
                  <Text style={[type.caption, { color: c.protein }]}>
                    P {Math.round(item.per_serving.protein_g)}g
                  </Text>
                  <Text style={[type.caption, { color: c.carbs }]}>
                    C {Math.round(item.per_serving.carbs_g)}g
                  </Text>
                  <Text style={[type.caption, { color: c.fat }]}>
                    F {Math.round(item.per_serving.fat_g)}g
                  </Text>
                </Row>
              </Card>
            )}
            ListEmptyComponent={
              <Empty
                title="No recipes yet"
                subtitle="Post one and NutriAI calculates the macros for you automatically."
              />
            }
          />
        )}

        {/* ---- recipe detail ---- */}
        <Modal visible={!!selected} animationType="slide" onRequestClose={() => setSelected(null)}>
          <Screen>
            <SafeAreaView style={{ flex: 1 }}>
              <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
                <H1>{selected?.title}</H1>
                {selected?.summary ? <Body dim>{selected.summary}</Body> : null}

                <Card>
                  <Row style={{ justifyContent: 'space-around' }}>
                    {[
                      ['kcal', Math.round(selected?.per_serving.kcal ?? 0), c.text],
                      ['protein', `${Math.round(selected?.per_serving.protein_g ?? 0)}g`, c.protein],
                      ['carbs', `${Math.round(selected?.per_serving.carbs_g ?? 0)}g`, c.carbs],
                      ['fat', `${Math.round(selected?.per_serving.fat_g ?? 0)}g`, c.fat],
                    ].map(([label, value, color]) => (
                      <View key={String(label)} style={{ alignItems: 'center' }}>
                        <Text style={[type.h2, { color: color as string }]}>{String(value)}</Text>
                        <Text style={[type.caption, { color: c.textFaint }]}>{String(label)}</Text>
                      </View>
                    ))}
                  </Row>
                  <Text style={[type.caption, { color: c.textFaint, textAlign: 'center', marginTop: space.sm }]}>
                    per serving
                  </Text>
                </Card>

                <Button
                  title="Personalise for me"
                  loading={adapt.isPending}
                  onPress={() => selected && personalize(selected)}
                />
                <Body dim>
                  Rewrites the recipe around your allergies, diet mode, macro targets, fasting
                  window and current training load — and recalculates the numbers.
                </Body>

                <View style={{ gap: space.sm }}>
                  <H2>Ingredients</H2>
                  {(selected?.ingredients ?? []).map((i, idx) => (
                    <Row key={idx} style={{ justifyContent: 'space-between' }}>
                      <Text style={[type.body, { color: c.text, flex: 1 }]}>{i.raw_text}</Text>
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        {Math.round(i.kcal)} kcal
                      </Text>
                    </Row>
                  ))}
                </View>

                <View style={{ gap: space.md }}>
                  <H2>Method</H2>
                  {(selected?.steps ?? []).map((s) => (
                    <Row key={s.n} gap={space.md} style={{ alignItems: 'flex-start' }}>
                      <Text style={[type.h2, { color: c.textFaint }]}>{s.n}</Text>
                      <Text style={[type.body, { color: c.text, flex: 1 }]}>{s.text}</Text>
                    </Row>
                  ))}
                </View>

                <Button title="Close" variant="secondary" onPress={() => setSelected(null)} />
              </ScrollView>
            </SafeAreaView>
          </Screen>
        </Modal>

        {/* ---- adaptation result ---- */}
        <Modal visible={!!adapted} animationType="slide" onRequestClose={() => setAdapted(null)}>
          <Screen>
            <SafeAreaView style={{ flex: 1 }}>
              <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
                <Label>Personalised for you</Label>
                <H1>{adapted?.title}</H1>
                <Body dim>{adapted?.adaptation_note}</Body>

                {adapted?.warnings?.length ? (
                  <Card style={{ borderColor: c.warn, gap: space.sm }}>
                    <Label>Check before cooking</Label>
                    {adapted.warnings.map((w, i) => (
                      <Text key={i} style={[type.body, { color: c.text }]}>• {w}</Text>
                    ))}
                  </Card>
                ) : null}

                {adapted?.substitutions?.length ? (
                  <Card style={{ gap: space.sm }}>
                    <Label>Swaps</Label>
                    {adapted.substitutions.map((s, i) => (
                      <View key={i}>
                        <Text style={[type.body, { color: c.text }]}>
                          {s.from} → <Text style={{ color: c.accent }}>{s.to}</Text>
                        </Text>
                        <Text style={[type.caption, { color: c.textFaint }]}>{s.reason}</Text>
                      </View>
                    ))}
                  </Card>
                ) : null}

                <Card>
                  <Row style={{ justifyContent: 'space-around' }}>
                    {[
                      ['kcal', Math.round(adapted?.per_serving.kcal ?? 0)],
                      ['protein', `${Math.round(adapted?.per_serving.protein_g ?? 0)}g`],
                      ['carbs', `${Math.round(adapted?.per_serving.carbs_g ?? 0)}g`],
                      ['fat', `${Math.round(adapted?.per_serving.fat_g ?? 0)}g`],
                    ].map(([l, v]) => (
                      <View key={String(l)} style={{ alignItems: 'center' }}>
                        <Text style={[type.h2, { color: c.text }]}>{String(v)}</Text>
                        <Text style={[type.caption, { color: c.textFaint }]}>{String(l)}</Text>
                      </View>
                    ))}
                  </Row>
                </Card>

                <Divider />
                <H2>Ingredients</H2>
                {(adapted?.ingredients ?? []).map((i: any, idx: number) => (
                  <Text key={idx} style={[type.body, { color: i.substituted_from ? c.accent : c.text }]}>
                    • {i.raw_text}
                  </Text>
                ))}

                <H2>Method</H2>
                {(adapted?.steps ?? []).map((s: any) => (
                  <Text key={s.n} style={[type.body, { color: c.text }]}>{s.n}. {s.text}</Text>
                ))}

                <Button title="Done" onPress={() => setAdapted(null)} />
              </ScrollView>
            </SafeAreaView>
          </Screen>
        </Modal>
      </SafeAreaView>
    </Screen>
  );
}
