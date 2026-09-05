/**
 * Daily celebration overlay.
 *
 * Particles are plain Animated views rather than a physics engine: 40 of them
 * for 2.5 seconds is imperceptibly different from a real simulation and costs
 * nothing on a three-year-old Android phone.
 */
import React, { useEffect, useMemo, useRef } from 'react';
import { Animated, Dimensions, Easing, Modal, Pressable, Text, View } from 'react-native';
import * as Haptics from 'expo-haptics';
import { space, type, useTheme } from '../theme';
import { useApp } from '../state/store';
import { api } from '../api/client';

const { width, height } = Dimensions.get('window');
const PARTICLE_COUNT = 40;

const PALETTES: Record<string, string[]> = {
  confetti: ['#5EE39A', '#5B9CFF', '#F2A93B', '#E8618C', '#3EC8E8'],
  fireworks: ['#FFD166', '#EF476F', '#06D6A0', '#118AB2'],
  flame: ['#FF6B35', '#F7C548', '#FF9F1C'],
  trophy: ['#FFD700', '#FFECB3', '#E6C200'],
  rings: ['#5B9CFF', '#5EE39A', '#E8618C'],
  wave: ['#3EC8E8', '#5B9CFF', '#A5E3F5'],
};

function Particle({ index, colors }: { index: number; colors: string[] }) {
  const progress = useRef(new Animated.Value(0)).current;
  const startX = useMemo(() => Math.random() * width, []);
  const drift = useMemo(() => (Math.random() - 0.5) * 220, []);
  const size = useMemo(() => 6 + Math.random() * 8, []);
  const color = colors[index % colors.length];
  const delay = useMemo(() => Math.random() * 400, []);
  const spin = useMemo(() => (Math.random() - 0.5) * 720, []);

  useEffect(() => {
    Animated.timing(progress, {
      toValue: 1,
      duration: 2200 + Math.random() * 800,
      delay,
      easing: Easing.out(Easing.quad),
      useNativeDriver: true,
    }).start();
  }, []);

  return (
    <Animated.View
      style={{
        position: 'absolute',
        left: startX,
        top: -20,
        width: size,
        height: size * 1.6,
        borderRadius: 2,
        backgroundColor: color,
        opacity: progress.interpolate({ inputRange: [0, 0.75, 1], outputRange: [1, 1, 0] }),
        transform: [
          { translateY: progress.interpolate({ inputRange: [0, 1], outputRange: [0, height + 60] }) },
          { translateX: progress.interpolate({ inputRange: [0, 1], outputRange: [0, drift] }) },
          {
            rotate: progress.interpolate({
              inputRange: [0, 1],
              outputRange: ['0deg', `${spin}deg`],
            }),
          },
        ],
      }}
    />
  );
}

export function CelebrationOverlay() {
  const c = useTheme();
  const celebration = useApp((s) => s.celebration);
  const dismiss = useApp((s) => s.dismissCelebration);
  const scale = useRef(new Animated.Value(0.85)).current;

  useEffect(() => {
    if (!celebration) return;
    Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    Animated.spring(scale, { toValue: 1, friction: 6, tension: 80, useNativeDriver: true }).start();
    // Auto-dismiss so a celebration never blocks the app.
    const t = setTimeout(handleDismiss, 4200);
    return () => clearTimeout(t);
  }, [celebration?.id]);

  function handleDismiss() {
    if (celebration?.id) api.social.markCelebrationSeen(celebration.id).catch(() => {});
    dismiss();
  }

  if (!celebration) return null;
  const colors = PALETTES[celebration.animation] ?? PALETTES.confetti;

  return (
    <Modal transparent animationType="fade" visible onRequestClose={handleDismiss}>
      <Pressable style={{ flex: 1, backgroundColor: c.overlay }} onPress={handleDismiss}>
        {Array.from({ length: PARTICLE_COUNT }).map((_, i) => (
          <Particle key={i} index={i} colors={colors} />
        ))}
        <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl }}>
          <Animated.View
            style={{
              transform: [{ scale }],
              backgroundColor: c.surface,
              borderRadius: 28,
              paddingVertical: space.xxl,
              paddingHorizontal: space.xl,
              alignItems: 'center',
              gap: space.sm,
              maxWidth: 340,
            }}
          >
            <Text style={{ fontSize: 52 }}>
              {celebration.animation === 'trophy' ? '🏆'
                : celebration.animation === 'flame' ? '🔥'
                : celebration.animation === 'wave' ? '💧' : '🎉'}
            </Text>
            <Text style={[type.h1, { color: c.text, textAlign: 'center' }]}>
              {celebration.title}
            </Text>
            {celebration.subtitle ? (
              <Text style={[type.body, { color: c.textDim, textAlign: 'center' }]}>
                {celebration.subtitle}
              </Text>
            ) : null}
            <Text style={[type.caption, { color: c.textFaint, marginTop: space.md }]}>
              Tap anywhere to dismiss
            </Text>
          </Animated.View>
        </View>
      </Pressable>
    </Modal>
  );
}
