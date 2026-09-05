/** Intermittent fasting timer with live phase feedback. */
import React, { useEffect, useState } from 'react';
import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { ProgressRing } from '../components/Rings';
import { useCurrentFast, useEndFast, useStartFast } from '../hooks/useApi';

const PROTOCOLS = [
  { id: '16:8', label: '16:8', note: 'The default. 16 hours fasting, 8 eating.' },
  { id: '18:6', label: '18:6', note: 'A step up once 16:8 feels routine.' },
  { id: '20:4', label: '20:4', note: 'Warrior-style. One large meal plus a snack.' },
  { id: 'omad', label: 'OMAD', note: 'One meal a day. Hard to hit protein — plan it.' },
  { id: 'custom', label: 'Custom', note: 'Set your own window.' },
];

function fmt(mins: number): string {
  const h = Math.floor(Math.abs(mins) / 60);
  const m = Math.abs(mins) % 60;
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

export function FastingScreen() {
  const c = useTheme();
  const { data: fast, isLoading } = useCurrentFast();
  const start = useStartFast();
  const end = useEndFast();
  const [protocol, setProtocol] = useState('16:8');
  const [, forceTick] = useState(0);

  // The server sends elapsed minutes; ticking locally keeps the ring honest
  // between refetches without polling the API every second.
  useEffect(() => {
    if (!fast) return;
    const t = setInterval(() => forceTick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, [fast?.id]);

  if (isLoading) return <Screen><Loading /></Screen>;

  const liveElapsed = fast
    ? Math.floor((Date.now() - new Date(fast.started_at).getTime()) / 60000)
    : 0;
  const livePct = fast ? Math.min(100, (liveElapsed / fast.target_minutes) * 100) : 0;
  const remaining = fast ? fast.target_minutes - liveElapsed : 0;

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
          <View>
            <Label>Intermittent fasting</Label>
            <H1>{fast ? 'Fasting' : 'Ready when you are'}</H1>
          </View>

          {fast ? (
            <>
              <Card style={{ alignItems: 'center', gap: space.lg }}>
                <ProgressRing size={220} stroke={18} pct={livePct} color={c.accent}>
                  <View style={{ alignItems: 'center' }}>
                    <Text style={[type.hero, { color: c.text }]}>{fmt(liveElapsed)}</Text>
                    <Text style={[type.caption, { color: c.textDim }]}>
                      {remaining > 0 ? `${fmt(remaining)} to go` : `${fmt(-remaining)} past target`}
                    </Text>
                  </View>
                </ProgressRing>

                <View style={{ alignItems: 'center', gap: 4 }}>
                  <Text style={[type.h2, { color: c.accent }]}>{fast.phase}</Text>
                  <Text style={[type.body, { color: c.textDim, textAlign: 'center' }]}>
                    {fast.phase_note}
                  </Text>
                </View>

                <Row gap={space.xl}>
                  <View style={{ alignItems: 'center' }}>
                    <Text style={[type.h2, { color: c.text }]}>{fast.protocol}</Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>protocol</Text>
                  </View>
                  <View style={{ alignItems: 'center' }}>
                    <Text style={[type.h2, { color: c.text }]}>{fast.streak}</Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>streak</Text>
                  </View>
                  <View style={{ alignItems: 'center' }}>
                    <Text style={[type.h2, { color: c.text }]}>
                      {new Date(fast.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                    </Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>started</Text>
                  </View>
                </Row>
              </Card>

              <Button
                title={remaining > 0 ? 'End fast early' : 'Complete fast'}
                variant={remaining > 0 ? 'secondary' : 'primary'}
                loading={end.isPending}
                onPress={() => end.mutate(fast.id)}
              />
              {remaining > 0 ? (
                <Body dim>
                  Ending before the target still logs the fast — it just won't count toward your
                  streak. There's no penalty for listening to your body.
                </Body>
              ) : null}
            </>
          ) : (
            <>
              <Card style={{ gap: space.md }}>
                <Label>Choose a protocol</Label>
                <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                  {PROTOCOLS.map((p) => (
                    <Chip key={p.id} label={p.label} active={protocol === p.id} onPress={() => setProtocol(p.id)} />
                  ))}
                </Row>
                <Body dim>{PROTOCOLS.find((p) => p.id === protocol)?.note}</Body>
              </Card>

              <Button
                title="Start fasting now"
                loading={start.isPending}
                onPress={() => start.mutate({ protocol, hours: protocol === 'custom' ? 14 : undefined })}
              />

              <Card style={{ gap: space.sm }}>
                <H2>What actually happens</H2>
                <Body dim>
                  Fasting shifts which fuel your body reaches for; it isn't magic and it doesn't
                  override total calories. If you feel faint, dizzy or unwell, eat. NutriAI tracks
                  fasts because the schedule helps some people eat consistently — not because
                  longer is better.
                </Body>
              </Card>
            </>
          )}
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
