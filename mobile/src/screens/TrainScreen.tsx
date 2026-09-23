/** Workout logging, the AI plan, and equipment scanning. */
import React, { useState } from 'react';
import { Alert, Image, ScrollView, Text, View } from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, Divider, Empty, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { uploadImage } from '../api/supabase';
import { useCreatePlan, useCurrentPlan, usePRs } from '../hooks/useApi';
import type { EquipmentScan } from '../api/types';

const GOALS = [
  { id: 'build_muscle', label: 'Build muscle' },
  { id: 'lose_fat', label: 'Lose fat' },
  { id: 'get_stronger', label: 'Get stronger' },
  { id: 'general_fitness', label: 'General fitness' },
  { id: 'endurance', label: 'Endurance' },
];

// backend/app/models/fitness.py: EquipmentScanIn.image_paths is
// Field(min_length=1, max_length=4) -- this is the real ceiling, not a
// UI-chosen number, so it is named here rather than repeated as a literal.
const MAX_EQUIPMENT_SHOTS = 4;

export function TrainScreen() {
  const c = useTheme();
  const { data: plan, isLoading } = useCurrentPlan();
  const { data: prs } = usePRs();
  const createPlan = useCreatePlan();
  const [scanning, setScanning] = useState(false);
  const [equipment, setEquipment] = useState<EquipmentScan | null>(null);
  // Shots taken but not yet submitted -- mirrors ScanScreen.tsx's `shots`
  // array. There is no separate `reviewing` flag the way ScanScreen has one:
  // that flag exists there to toggle its own embedded live CameraView on and
  // off, and this screen has no live camera view to toggle -- it hands off to
  // the system camera app (launchCameraAsync) and gets control back only once
  // a photo exists or the user cancels. So `shots.length > 0` alone is enough
  // to know a review is in progress; the review UI below is keyed on exactly
  // that.
  const [shots, setShots] = useState<string[]>([]);
  const [goal, setGoal] = useState('build_muscle');
  const [days, setDays] = useState(4);
  const [week, setWeek] = useState(1);

  // "Live capture" for this screen: hand off to the system camera and, on a
  // real photo, add it to the shots collected so far. Used both for the
  // first photo and for "Add another angle" -- same action either way, the
  // only difference is whether `shots` already has something in it.
  async function captureAngle() {
    const res = await ImagePicker.launchCameraAsync({ quality: 0.8 });
    if (res.canceled || !res.assets[0]) return;
    setShots((s) => [...s, res.assets[0].uri].slice(0, MAX_EQUIPMENT_SHOTS));
  }

  async function runScan() {
    if (!shots.length) return;
    setScanning(true);
    try {
      const paths = await Promise.all(shots.map((uri) => uploadImage('equipment-photos', uri)));
      const detected = await api.fitness.scanEquipment(paths);
      setEquipment(detected);
      setShots([]);
      if (detected.fallback_to_calisthenics) {
        Alert.alert(
          'No equipment spotted',
          "That's fine — NeutriAI will build a bodyweight programme. You can add equipment later and it will rebuild around it.",
        );
      }
    } catch (e: any) {
      // Shots are kept on failure (not reset), the same as ScanScreen's
      // analyse() -- a failed upload or scan call should not force a retake
      // of photos that are still sitting right there.
      if (!e?.needsUpgrade) Alert.alert('Scan failed', e?.message ?? 'Try again.');
    } finally {
      setScanning(false);
    }
  }

  function generate() {
    createPlan.mutate({
      goal,
      days_per_week: days,
      weeks: 4,
      session_minutes: 45,
      equipment_scan_id: equipment?.id,
      equipment: equipment?.equipment,
      experience: 'intermediate',
      limitations: [],
    });
  }

  if (isLoading) return <Screen><Loading /></Screen>;

  const weekDays = (plan?.days ?? []).filter((d) => d.week_index === week);

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
          <View>
            <Label>Training</Label>
            <H1>{plan ? plan.name : 'Build a plan'}</H1>
          </View>

          {!plan ? (
            <>
              <Card style={{ gap: space.md }}>
                <H2>Start with what you have</H2>
                <Body dim>
                  Photograph your gym, garage or living room. NeutriAI identifies the equipment and
                  writes a programme around it. No equipment at all is a perfectly good answer —
                  you'll get a real calisthenics progression, not a consolation prize.
                </Body>
                {shots.length === 0 ? (
                  <Button
                    title={equipment ? 'Rescan equipment' : 'Scan my equipment'}
                    variant="secondary"
                    loading={scanning}
                    onPress={captureAngle}
                  />
                ) : (
                  <View style={{ gap: space.sm }}>
                    <Row gap={space.sm}>
                      {shots.map((uri) => (
                        <Image key={uri} source={{ uri }} style={{ width: 64, height: 64, borderRadius: radius.sm }} />
                      ))}
                    </Row>
                    <Body dim>
                      {shots.length < MAX_EQUIPMENT_SHOTS
                        ? `${shots.length} photo${shots.length > 1 ? 's' : ''} captured — add another angle or scan now`
                        : `${MAX_EQUIPMENT_SHOTS} photos captured, the most a scan can use`}
                    </Body>
                    <Row gap={space.md}>
                      {shots.length < MAX_EQUIPMENT_SHOTS ? (
                        <Button
                          title="More equipment"
                          variant="secondary"
                          disabled={scanning}
                          style={{ flex: 1 }}
                          onPress={captureAngle}
                        />
                      ) : null}
                      <Button
                        title={`Scan ${shots.length} photo${shots.length > 1 ? 's' : ''}`}
                        loading={scanning}
                        style={{ flex: 1 }}
                        onPress={runScan}
                      />
                    </Row>
                  </View>
                )}
                {equipment ? (
                  <View style={{ gap: space.sm }}>
                    <Divider />
                    <Label>Detected</Label>
                    <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                      {equipment.equipment.map((e) => (
                        <Chip key={e} label={e.replace(/_/g, ' ')} active />
                      ))}
                    </Row>
                    {equipment.detected.map((d, i) => (
                      <Text key={i} style={[type.caption, { color: c.textFaint }]}>
                        • {d.detail}
                      </Text>
                    ))}
                  </View>
                ) : null}
              </Card>

              <Card style={{ gap: space.md }}>
                <Label>Goal</Label>
                <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                  {GOALS.map((g) => (
                    <Chip key={g.id} label={g.label} active={goal === g.id} onPress={() => setGoal(g.id)} />
                  ))}
                </Row>
                <Label>Days per week</Label>
                <Row gap={space.sm}>
                  {[2, 3, 4, 5, 6].map((d) => (
                    <Chip key={d} label={String(d)} active={days === d} onPress={() => setDays(d)} />
                  ))}
                </Row>
              </Card>

              <Button
                title="Generate my 4-week plan"
                loading={createPlan.isPending}
                onPress={generate}
              />
              {createPlan.isPending ? (
                <Body dim>Writing four weeks of progressive training. This takes about 20 seconds.</Body>
              ) : null}
            </>
          ) : (
            <>
              {plan.is_calisthenics_fallback ? (
                <Card style={{ borderColor: c.accent }}>
                  <Label>Bodyweight programme</Label>
                  <Body dim>
                    Built entirely from movements that need no equipment, with a real progression
                    ladder from incline push-ups through to one-arm work.
                  </Body>
                </Card>
              ) : null}

              <Card style={{ gap: space.sm }}>
                <Label>Progression</Label>
                <Body>{String(plan.progression?.rule ?? 'Add reps, then load.')}</Body>
                {plan.safety_notes.length > 0 ? (
                  <View style={{ gap: 4, marginTop: space.sm }}>
                    {plan.safety_notes.map((n, i) => (
                      <Text key={i} style={[type.caption, { color: c.textFaint }]}>• {n}</Text>
                    ))}
                  </View>
                ) : null}
              </Card>

              <Row gap={space.sm}>
                {Array.from({ length: plan.weeks }).map((_, i) => (
                  <Chip key={i} label={`Week ${i + 1}`} active={week === i + 1} onPress={() => setWeek(i + 1)} />
                ))}
              </Row>

              {weekDays.map((d) => (
                <Card key={d.id} style={{ gap: space.md }}>
                  <Row style={{ justifyContent: 'space-between' }}>
                    <View>
                      <Text style={[type.h2, { color: c.text }]}>{d.title}</Text>
                      <Text style={[type.caption, { color: c.textDim }]}>
                        {d.kind} · {d.est_minutes} min
                      </Text>
                    </View>
                    {d.completed_at ? (
                      <View style={{ paddingHorizontal: 10, paddingVertical: 4, borderRadius: radius.pill, backgroundColor: c.accent + '22' }}>
                        <Text style={{ color: c.accent, fontSize: 12, fontWeight: '600' }}>Done</Text>
                      </View>
                    ) : null}
                  </Row>
                  {d.blocks.map((b, i) => (
                    <View key={i} style={{ gap: 2 }}>
                      <Row style={{ justifyContent: 'space-between' }}>
                        <Text style={[type.body, { color: c.text }]}>{b.name}</Text>
                        <Text style={[type.body, { color: c.textDim, fontVariant: ['tabular-nums'] }]}>
                          {b.sets} × {b.reps}
                        </Text>
                      </Row>
                      {b.load_hint ? (
                        <Text style={[type.caption, { color: c.textFaint }]}>{b.load_hint}</Text>
                      ) : null}
                    </View>
                  ))}
                </Card>
              ))}
            </>
          )}

          {prs && prs.length > 0 ? (
            <Card style={{ gap: space.md }}>
              <H2>Personal records</H2>
              {prs.slice(0, 8).map((pr, i) => (
                <Row key={i} style={{ justifyContent: 'space-between' }}>
                  <Text style={[type.body, { color: c.text, textTransform: 'capitalize' }]}>
                    {pr.exercise_slug.replace(/-/g, ' ')}
                  </Text>
                  <Text style={[type.body, { color: c.accent, fontWeight: '600' }]}>
                    {pr.value}{pr.unit} <Text style={{ color: c.textFaint }}>{pr.metric}</Text>
                  </Text>
                </Row>
              ))}
            </Card>
          ) : null}
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
