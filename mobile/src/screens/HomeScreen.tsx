/** Dashboard: calories, macros, water, fast, today's meals, motivation. */
import React, { useCallback, useEffect } from 'react';
import { RefreshControl, ScrollView, Text, View } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, Empty, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { CalorieRing, MacroRow, ProgressRing } from '../components/Rings';
import { useDashboard, useLogWater } from '../hooks/useApi';
import { useApp } from '../state/store';

const GLASS_ML = 250;
const BOTTLE_ML = 500;

function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return 'Still up';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

export function HomeScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const { data, isLoading, refetch, isRefetching } = useDashboard();
  const logWater = useLogWater();
  const showCelebration = useApp((s) => s.showCelebration);

  // The dashboard tells us when a celebration is pending; the overlay owns
  // showing it, so it works the same whether it arrived here or via push.
  useEffect(() => {
    if (data?.celebration) showCelebration(data.celebration);
  }, [data?.celebration?.id]);

  const onRefresh = useCallback(() => refetch(), [refetch]);

  if (isLoading) return <Screen><Loading label="Loading your day" /></Screen>;

  const summary = data?.summary;
  const targets = data?.targets;
  const streaks = data?.streaks ?? {};

  if (!targets) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1, padding: space.lg, justifyContent: 'center' }}>
          <Empty
            title="Let's set your targets"
            subtitle="A few details about you and NeutriAI can calculate exactly what to aim for."
          />
          <Button title="Finish setup" onPress={() => nav.navigate('Onboarding')} />
        </SafeAreaView>
      </Screen>
    );
  }

  const waterPct = ((summary?.water_ml ?? 0) / Math.max(targets.water_ml, 1)) * 100;

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <ScrollView
          contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}
          refreshControl={<RefreshControl refreshing={isRefetching} onRefresh={onRefresh} tintColor={c.accent} />}
        >
          <View>
            <Label>{greeting()}</Label>
            <H1>Today</H1>
          </View>

          {/* ---- calories + macros ---- */}
          <Card>
            <View style={{ alignItems: 'center', gap: space.lg }}>
              <CalorieRing
                consumed={summary?.kcal_in ?? 0}
                target={targets.target_kcal}
                burned={summary?.kcal_out ?? 0}
              />
              <MacroRow
                protein={summary?.protein_g ?? 0}
                carbs={summary?.carbs_g ?? 0}
                fat={summary?.fat_g ?? 0}
                targets={targets}
              />
              {(summary?.kcal_out ?? 0) > 0 ? (
                <Text style={[type.caption, { color: c.textFaint }]}>
                  {summary?.kcal_out} kcal burned — 60% credited toward your target
                </Text>
              ) : null}
            </View>
          </Card>

          {/* ---- scan CTA ---- */}
          <Button title="Scan a meal" onPress={() => nav.navigate('Scan')} />

          {/* ---- water + fast side by side ---- */}
          <Row gap={space.md} style={{ alignItems: 'stretch' }}>
            <Card style={{ flex: 1, alignItems: 'center', gap: space.md }}>
              <Label>Water</Label>
              <ProgressRing size={92} stroke={9} pct={waterPct} color={c.water}>
                <View style={{ alignItems: 'center' }}>
                  <Text style={[type.h2, { color: c.text }]}>{Math.round(waterPct)}%</Text>
                  <Text style={[type.caption, { color: c.textFaint }]}>
                    {(summary?.water_ml ?? 0)} ml
                  </Text>
                </View>
              </ProgressRing>
              <Row gap={space.sm}>
                <Chip label={`+${GLASS_ML}`} onPress={() => logWater.mutate({ ml: GLASS_ML, container: 'glass' })} />
                <Chip label={`+${BOTTLE_ML}`} onPress={() => logWater.mutate({ ml: BOTTLE_ML, container: 'bottle' })} />
              </Row>
            </Card>

            <Card style={{ flex: 1, gap: space.md, justifyContent: 'space-between' }}>
              <Label>Fasting</Label>
              {data?.active_fast ? (
                <>
                  <View style={{ alignItems: 'center' }}>
                    <ProgressRing size={92} stroke={9} pct={data.active_fast.pct} color={c.accent}>
                      <View style={{ alignItems: 'center' }}>
                        <Text style={[type.h2, { color: c.text }]}>
                          {Math.floor(data.active_fast.elapsed_minutes / 60)}h
                        </Text>
                        <Text style={[type.caption, { color: c.textFaint }]}>
                          {data.active_fast.elapsed_minutes % 60}m
                        </Text>
                      </View>
                    </ProgressRing>
                  </View>
                  <Text style={[type.caption, { color: c.textDim, textAlign: 'center' }]}>
                    {data.active_fast.phase}
                  </Text>
                </>
              ) : (
                <View style={{ flex: 1, justifyContent: 'center', gap: space.md }}>
                  <Body dim>No fast running.</Body>
                  <Button title="Start" variant="secondary" onPress={() => nav.navigate('Fasting')} />
                </View>
              )}
            </Card>
          </Row>

          {/* ---- streaks ---- */}
          {Object.keys(streaks).length > 0 ? (
            <Card>
              <Label>Streaks</Label>
              <Row gap={space.lg} style={{ marginTop: space.md, flexWrap: 'wrap' }}>
                {Object.entries(streaks).map(([kind, n]) => (
                  <View key={kind} style={{ alignItems: 'center', minWidth: 64 }}>
                    <Text style={[type.h2, { color: n > 0 ? c.accent : c.textFaint }]}>{n}</Text>
                    <Text style={[type.caption, { color: c.textDim, textTransform: 'capitalize' }]}>
                      {kind}
                    </Text>
                  </View>
                ))}
              </Row>
            </Card>
          ) : null}

          {/* ---- today's meals ---- */}
          <View style={{ gap: space.md }}>
            <Row style={{ justifyContent: 'space-between' }}>
              <H2>Meals</H2>
              <Text style={[type.caption, { color: c.textDim }]}>
                {data?.meals?.length ?? 0} logged
              </Text>
            </Row>
            {(data?.meals ?? []).length === 0 ? (
              <Card><Body dim>Nothing logged yet. Scan a photo or add one manually.</Body></Card>
            ) : (
              data!.meals.map((m) => (
                <Card key={m.id} onPress={() => nav.navigate('MealDetail', { mealId: m.id })}>
                  <Row style={{ justifyContent: 'space-between' }}>
                    <View style={{ flex: 1 }}>
                      <Text style={[type.body, { color: c.text, fontWeight: '600' }]}>{m.title}</Text>
                      <Text style={[type.caption, { color: c.textDim, marginTop: 2 }]}>
                        {m.meal_slot.replace('_', ' ')} ·{' '}
                        {new Date(m.eaten_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                        {m.confidence != null && m.confidence < 0.6 ? ' · needs review' : ''}
                      </Text>
                    </View>
                    <View style={{ alignItems: 'flex-end' }}>
                      <Text style={[type.h2, { color: c.text }]}>{Math.round(m.kcal)}</Text>
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        P{Math.round(m.protein_g)} C{Math.round(m.carbs_g)} F{Math.round(m.fat_g)}
                      </Text>
                    </View>
                  </Row>
                </Card>
              ))
            )}
          </View>
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
