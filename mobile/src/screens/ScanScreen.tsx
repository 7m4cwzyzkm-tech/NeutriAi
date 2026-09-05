/**
 * The camera flow. Three states in one screen: capture, analysing, results.
 *
 * The results state is where the product earns trust, so it does two things
 * most calorie apps don't: it shows the gram *range* rather than a fake-precise
 * single number, and it colours each item by how confident the estimate is.
 * Everything is editable before it counts.
 */
import React, { useState } from 'react';
import { Alert, Image, ScrollView, Text, View } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import * as ImagePicker from 'expo-image-picker';
import { useNavigation } from '@react-navigation/native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { confidenceColor, radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { uploadImage } from '../api/supabase';
import { useScanMeal } from '../hooks/useApi';
import type { ScanResult } from '../api/types';

const SLOTS = ['breakfast', 'lunch', 'dinner', 'snack'] as const;

const METHOD_LABEL: Record<string, string> = {
  plate_reference: 'Plate reference',
  depth_model: 'Depth estimate',
  multi_image: 'Multi-angle',
  pixel_area: 'Pixel area',
  ai_prior: 'Typical serving',
  user_entered: 'You entered this',
};

export function ScanScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const [permission, requestPermission] = useCameraPermissions();
  const [shots, setShots] = useState<string[]>([]);
  const [slot, setSlot] = useState<(typeof SLOTS)[number] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<ScanResult | null>(null);
  const scan = useScanMeal();
  const cameraRef = React.useRef<CameraView>(null);

  async function capture() {
    const photo = await cameraRef.current?.takePictureAsync({ quality: 0.8 });
    if (photo?.uri) setShots((s) => [...s, photo.uri].slice(0, 3));
  }

  async function pickFromLibrary() {
    const res = await ImagePicker.launchImageLibraryAsync({ quality: 0.8, mediaTypes: ['images'] });
    if (!res.canceled && res.assets[0]) setShots((s) => [...s, res.assets[0].uri].slice(0, 3));
  }

  async function analyse() {
    if (!shots.length) return;
    setUploading(true);
    try {
      const paths = await Promise.all(shots.map((uri) => uploadImage('meal-photos', uri)));
      const res = await scan.mutateAsync({
        image_paths: paths,
        meal_slot: slot ?? undefined,
      });
      setResult(res);
    } catch (e: any) {
      // The paywall opens itself via the shared error handler; anything else
      // deserves a plain explanation rather than a silent failure.
      if (!e?.needsUpgrade) {
        Alert.alert('Could not analyse that', e?.message ?? 'Please try again.');
      }
    } finally {
      setUploading(false);
    }
  }

  // ---------------------------------------------------------------- results
  if (result) {
    const a = result.assessment;
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1 }}>
          <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
            <View>
              <Label>Scan complete</Label>
              <H1>{Math.round(result.totals.kcal)} kcal</H1>
              <Row gap={space.sm} style={{ marginTop: space.sm }}>
                <View
                  style={{
                    paddingVertical: 4, paddingHorizontal: 10, borderRadius: radius.pill,
                    backgroundColor: confidenceColor(c, result.overall_confidence) + '22',
                  }}
                >
                  <Text style={{ color: confidenceColor(c, result.overall_confidence), fontSize: 12, fontWeight: '600' }}>
                    {result.confidence_band} confidence
                  </Text>
                </View>
                {result.latency_ms ? (
                  <Text style={[type.caption, { color: c.textFaint }]}>
                    {(result.latency_ms / 1000).toFixed(1)}s
                  </Text>
                ) : null}
              </Row>
            </View>

            {result.needs_review ? (
              <Card style={{ borderColor: c.warn }}>
                <Body>
                  This one is worth a second look before it counts — the photo made a few items
                  hard to size accurately.
                </Body>
              </Card>
            ) : null}

            {result.items.map((item, i) => (
              <Card key={i}>
                <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <View style={{ flex: 1, gap: 4 }}>
                    <Text style={[type.body, { color: c.text, fontWeight: '600', textTransform: 'capitalize' }]}>
                      {item.name}
                    </Text>
                    <Text style={[type.caption, { color: c.textDim }]}>
                      {Math.round(item.grams)} g
                      {item.grams_low && item.grams_high
                        ? `  (${Math.round(item.grams_low)}–${Math.round(item.grams_high)})`
                        : ''}
                    </Text>
                    <Row gap={space.xs}>
                      <View
                        style={{
                          width: 6, height: 6, borderRadius: 3,
                          backgroundColor: confidenceColor(c, item.confidence),
                        }}
                      />
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        {METHOD_LABEL[item.estimation_method] ?? item.estimation_method}
                      </Text>
                    </Row>
                  </View>
                  <View style={{ alignItems: 'flex-end' }}>
                    <Text style={[type.h2, { color: c.text }]}>{Math.round(item.macros.kcal)}</Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>
                      P{Math.round(item.macros.protein_g)} C{Math.round(item.macros.carbs_g)} F{Math.round(item.macros.fat_g)}
                    </Text>
                  </View>
                </Row>
              </Card>
            ))}

            {a ? (
              <Card style={{ borderColor: a.severity === 'none' ? c.border : c.warn, gap: space.sm }}>
                <Label>{a.severity === 'none' ? 'On track' : 'Heads up'}</Label>
                <H2>{a.headline}</H2>
                <Body dim>{a.detail}</Body>
                {a.portion_advice.length > 0 ? (
                  <View style={{ gap: 6, marginTop: space.sm }}>
                    {a.portion_advice.map((p, i) => (
                      <Text key={i} style={[type.body, { color: c.textDim }]}>• {p}</Text>
                    ))}
                  </View>
                ) : null}
                {a.next_meal.suggestions.length > 0 ? (
                  <View style={{ marginTop: space.md, gap: 6 }}>
                    <Label>Next meal — {a.next_meal.target_kcal} kcal, {a.next_meal.emphasis}</Label>
                    {a.next_meal.suggestions.map((s, i) => (
                      <Text key={i} style={[type.body, { color: c.textDim }]}>• {s}</Text>
                    ))}
                  </View>
                ) : null}
              </Card>
            ) : null}

            {result.notes.length > 0 ? (
              <Card>
                <Label>How this was calculated</Label>
                <View style={{ gap: 6, marginTop: space.sm }}>
                  {result.notes.map((n, i) => (
                    <Text key={i} style={[type.caption, { color: c.textFaint }]}>• {n}</Text>
                  ))}
                </View>
              </Card>
            ) : null}

            <Row gap={space.md}>
              <Button
                title="Edit items"
                variant="secondary"
                style={{ flex: 1 }}
                onPress={() => nav.navigate('MealDetail', { mealId: result.meal_id, edit: true })}
              />
              <Button title="Looks right" style={{ flex: 1 }} onPress={() => nav.navigate('Home')} />
            </Row>
          </ScrollView>
        </SafeAreaView>
      </Screen>
    );
  }

  // -------------------------------------------------------------- analysing
  if (uploading || scan.isPending) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1, justifyContent: 'center' }}>
          <Loading label={uploading ? 'Uploading your photo…' : 'Identifying the food and sizing portions…'} />
          <Text style={[type.caption, { color: c.textFaint, textAlign: 'center', paddingHorizontal: space.xxl }]}>
            This usually takes about five seconds. Multi-angle shots take a little longer but are
            noticeably more accurate.
          </Text>
        </SafeAreaView>
      </Screen>
    );
  }

  // ---------------------------------------------------------------- capture
  if (!permission?.granted) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1, justifyContent: 'center', padding: space.xl, gap: space.lg }}>
          <H1>Camera access</H1>
          <Body dim>
            NutriAI needs the camera to identify what's on your plate. Photos are uploaded to your
            own private folder and are never public.
          </Body>
          <Button title="Allow camera" onPress={requestPermission} />
          <Button title="Choose from library instead" variant="ghost" onPress={pickFromLibrary} />
        </SafeAreaView>
      </Screen>
    );
  }

  return (
    <Screen>
      <View style={{ flex: 1 }}>
        <CameraView ref={cameraRef} style={{ flex: 1 }} facing="back" />

        {/* Framing guide: keeping the whole plate in frame is what makes the
            plate-reference estimate possible, so we ask for it visually. */}
        <View
          pointerEvents="none"
          style={{
            position: 'absolute', top: '18%', left: '8%', right: '8%', bottom: '32%',
            borderWidth: 2, borderColor: 'rgba(255,255,255,0.5)', borderRadius: 200,
          }}
        />
        <Text
          style={[
            type.caption,
            {
              position: 'absolute', top: '13%', width: '100%', textAlign: 'center',
              color: 'rgba(255,255,255,0.85)',
            },
          ]}
        >
          Fit the whole plate inside the circle
        </Text>

        <SafeAreaView edges={['bottom']} style={{ position: 'absolute', bottom: 0, width: '100%' }}>
          <View style={{ padding: space.lg, gap: space.md, backgroundColor: 'rgba(0,0,0,0.55)' }}>
            {shots.length > 0 ? (
              <Row gap={space.sm}>
                {shots.map((uri) => (
                  <Image key={uri} source={{ uri }} style={{ width: 52, height: 52, borderRadius: radius.sm }} />
                ))}
                <Text style={[type.caption, { color: '#fff', flex: 1 }]}>
                  {shots.length < 3 ? 'Add another angle for a better estimate' : 'Three angles — nice'}
                </Text>
              </Row>
            ) : null}

            <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
              {SLOTS.map((s) => (
                <Chip key={s} label={s} active={slot === s} onPress={() => setSlot(s)} />
              ))}
            </Row>

            <Row gap={space.md}>
              <Button title="Library" variant="secondary" style={{ flex: 1 }} onPress={pickFromLibrary} />
              <Button title={shots.length ? 'Another angle' : 'Capture'} style={{ flex: 1 }} onPress={capture} />
            </Row>
            {shots.length > 0 ? <Button title={`Analyse ${shots.length} photo${shots.length > 1 ? 's' : ''}`} onPress={analyse} /> : null}
          </View>
        </SafeAreaView>
      </View>
    </Screen>
  );
}
