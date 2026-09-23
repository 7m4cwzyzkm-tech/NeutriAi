/** Intermittent fasting timer with live phase feedback. */
import React, { useEffect, useState } from 'react';
import { Alert, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { ProgressRing } from '../components/Rings';
import { useCurrentFast, useEndFast, useProfile, useStartFast } from '../hooks/useApi';
import { api } from '../api/client';
import type { Fast, FastingSettings, Goal } from '../api/types';
import {
  cancelRemindersOfKind, ensureNotificationPermission, scheduleLocalReminder,
} from '../native/localReminders';

// Tags the local notifications this screen schedules, so ending a fast early
// cancels exactly these and nothing else (water reminders, celebrations...).
const FAST_REMINDER_KIND = 'fast_reminder';

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

/**
 * Plain conditional copy keyed on the profile's goal and the chosen protocol.
 * No model call: this is the same four goals the targets math already uses.
 */
function adviceFor(goal: Goal, protocol: string): string {
  const shortWindow = protocol === '20:4' || protocol === 'omad';
  switch (goal) {
    case 'lose':
      return (
        "You're aiming to lose weight. A fasting window can make a calorie deficit easier to " +
        'stick to, but the deficit does the work, not the hours. Open your window with protein ' +
        'so what you lose is mostly fat, not muscle.'
      );
    case 'gain':
      return (
        "You're aiming to gain. Fasting works against that: fewer hours to eat means bigger " +
        'meals. ' +
        (shortWindow
          ? 'A window this short makes a surplus hard to reach — 16:8 is a gentler start.'
          : "If you keep missing your calories, fast less — that's the right call, not a failure.")
      );
    case 'recomp':
      return (
        "You're recomposing — building muscle while losing fat. Protein and training matter more " +
        'than fast length: put a protein-rich meal after training inside your window, and ease ' +
        'off the fast if lifting on an empty stomach hurts your sessions.'
      );
    case 'maintain':
    default:
      return (
        "You're maintaining. Here fasting is about routine, not a deficit — eat your usual " +
        "amount inside the window, and watch that a shorter window doesn't turn into eating less " +
        'than you mean to.'
      );
  }
}

function clock(d: Date): string {
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/**
 * The user said yes to a reminder for this fast. Records the preference
 * (notify_end, and notify_halfway if asked) -- only turning flags ON, never
 * off -- then schedules one-shot local notifications for this fast.
 *
 * Local, not server push: the server's fasting_notifications() job cannot
 * reach this device (no EAS projectId, so no push token). A one-shot DATE
 * notification is held by the OS and fires with the app closed; see
 * scheduleLocalReminder.
 */
async function enableFastReminders(fast: Fast, halfway: boolean): Promise<void> {
  try {
    const current: FastingSettings = await api.fasting.settings();
    const patch: Partial<FastingSettings> = {};
    if (current.notify_end !== true) patch.notify_end = true;
    if (halfway && current.notify_halfway !== true) patch.notify_halfway = true;
    if (Object.keys(patch).length) await api.fasting.updateSettings(patch);
  } catch {
    // The saved preference is a nice-to-have; the local reminder below is
    // what actually reaches the phone, so don't let a settings hiccup block it.
  }

  if (!(await ensureNotificationPermission())) {
    Alert.alert(
      'Notifications are off',
      'Turn on notifications for NeutriAI in your phone settings to get fasting reminders.',
    );
    return;
  }

  await cancelRemindersOfKind(FAST_REMINDER_KIND);
  const startedMs = new Date(fast.started_at).getTime();
  const targetMs = fast.target_minutes * 60_000;
  const data = { deep_link: 'neutriai://fasting', fast_id: fast.id };
  await scheduleLocalReminder({
    kind: FAST_REMINDER_KIND,
    title: 'Your eating window is open',
    body: `You hit your ${fast.protocol} target. Break the fast with something with protein.`,
    date: new Date(startedMs + targetMs),
    data,
  });
  if (halfway) {
    await scheduleLocalReminder({
      kind: FAST_REMINDER_KIND,
      title: 'Halfway there',
      body: `Your ${fast.protocol} fast is half done. Water helps.`,
      date: new Date(startedMs + targetMs / 2),
      data,
    });
  }
}

function askAboutReminder(fast: Fast) {
  const enable = (halfway: boolean) =>
    enableFastReminders(fast, halfway).catch(() =>
      Alert.alert("Couldn't set the reminder", 'Your fast is still running — only the reminder failed.'),
    );
  const opensAt = new Date(new Date(fast.started_at).getTime() + fast.target_minutes * 60_000);
  Alert.alert(
    'Remind you when you can eat?',
    `Your eating window opens at ${clock(opensAt)}. We can send a notification then.`,
    [
      { text: 'No thanks', style: 'cancel' },
      { text: 'Also at halfway', onPress: () => void enable(true) },
      { text: 'Remind me', onPress: () => void enable(false) },
    ],
  );
}

export function FastingScreen() {
  const c = useTheme();
  const { data: fast, isLoading } = useCurrentFast();
  const start = useStartFast();
  const end = useEndFast();
  const { data: profile } = useProfile();
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
  // Red while a fast is running, green when there is none (the eating window).
  // This follows `fast` presence only: nothing tracks the eating window's own
  // end, so green never counts down or flips to red by itself -- it turns red
  // when the user starts the next fast.
  const stateColor = fast ? c.danger : c.success;

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
          <View>
            <Label>Intermittent fasting</Label>
            <H1>{fast ? 'Fasting' : 'Ready when you are'}</H1>
            <Row gap={6} style={{ marginTop: 4 }}>
              <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: stateColor }} />
              <Text style={[type.caption, { color: stateColor }]}>
                {fast ? 'Fast in progress' : 'Not fasting — eating window'}
              </Text>
            </Row>
          </View>

          {fast ? (
            <>
              <Card style={{ alignItems: 'center', gap: space.lg }}>
                <ProgressRing size={220} stroke={18} pct={livePct} color={stateColor}>
                  <View style={{ alignItems: 'center' }}>
                    <Text style={[type.hero, { color: c.text }]}>{fmt(liveElapsed)}</Text>
                    <Text style={[type.caption, { color: c.textDim }]}>
                      {remaining > 0 ? `${fmt(remaining)} to go` : `${fmt(-remaining)} past target`}
                    </Text>
                  </View>
                </ProgressRing>

                <View style={{ alignItems: 'center', gap: 4 }}>
                  <Text style={[type.h2, { color: stateColor }]}>{fast.phase}</Text>
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
                      {clock(new Date(fast.started_at))}
                    </Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>started</Text>
                  </View>
                </Row>
              </Card>

              <Button
                title={remaining > 0 ? 'End fast early' : 'Complete fast'}
                variant={remaining > 0 ? 'secondary' : 'primary'}
                loading={end.isPending}
                onPress={() =>
                  end.mutate(fast.id, {
                    // A "your window is open" alert for a fast that already
                    // ended makes no sense -- drop any pending one.
                    onSuccess: () => void cancelRemindersOfKind(FAST_REMINDER_KIND),
                  })
                }
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

              <Card style={{ gap: space.xs, borderColor: c.warn, borderWidth: 1 }}>
                <Label style={{ color: c.warn }}>Before you start</Label>
                <Body>
                  Always check with your healthcare provider before starting a fast, especially if
                  you have a medical condition, take medication, are pregnant or nursing, or have a
                  history of disordered eating.
                </Body>
              </Card>

              <Button
                title="Start fasting now"
                loading={start.isPending}
                onPress={() =>
                  start.mutate(
                    { protocol, hours: protocol === 'custom' ? 14 : undefined },
                    { onSuccess: askAboutReminder },
                  )
                }
              />

              <Card style={{ gap: space.sm }}>
                <H2>What actually happens</H2>
                <Body dim>
                  Fasting shifts which fuel your body reaches for; it isn't magic and it doesn't
                  override total calories. If you feel faint, dizzy or unwell, eat. NeutriAI tracks
                  fasts because the schedule helps some people eat consistently — not because
                  longer is better.
                </Body>
                {profile ? (
                  <>
                    <Label style={{ marginTop: space.sm }}>For your goal</Label>
                    <Body dim>{adviceFor(profile.goal, protocol)}</Body>
                  </>
                ) : null}
              </Card>
            </>
          )}
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
