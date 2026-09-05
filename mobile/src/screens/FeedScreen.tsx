/** Social feed with realtime updates. */
import React, { useEffect, useState } from 'react';
import { FlatList, Pressable, Text, View } from 'react-native';
import { useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Card, Chip, Empty, H1, Label, Loading, Row, Screen } from '../components/Primitives';
import { supabase } from '../api/supabase';
import { useFeed, useToggleLike } from '../hooks/useApi';
import type { Post } from '../api/types';

const SCOPES = [
  { id: 'following', label: 'Following' },
  { id: 'discover', label: 'Discover' },
  { id: 'mine', label: 'Mine' },
] as const;

function timeAgo(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function PostCard({ post }: { post: Post }) {
  const c = useTheme();
  const toggle = useToggleLike();
  const m = post.metrics ?? {};

  return (
    <Card style={{ gap: space.md }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Row gap={space.sm}>
          <View
            style={{
              width: 36, height: 36, borderRadius: 18, backgroundColor: c.surfaceAlt,
              alignItems: 'center', justifyContent: 'center',
            }}
          >
            <Text style={{ color: c.textDim, fontWeight: '700' }}>
              {(post.author.display_name || post.author.handle || '?')[0].toUpperCase()}
            </Text>
          </View>
          <View>
            <Text style={[type.body, { color: c.text, fontWeight: '600' }]}>
              {post.author.display_name || post.author.handle}
            </Text>
            <Text style={[type.caption, { color: c.textFaint }]}>
              @{post.author.handle} · {timeAgo(post.created_at)}
            </Text>
          </View>
        </Row>
        <View style={{ paddingHorizontal: 8, paddingVertical: 3, borderRadius: radius.pill, backgroundColor: c.surfaceAlt }}>
          <Text style={[type.caption, { color: c.textDim }]}>{post.kind}</Text>
        </View>
      </Row>

      {post.body ? <Body>{post.body}</Body> : null}

      {/* Attached meal/workout metrics render as a compact stat strip rather
          than a second card — the point is the number, not the container. */}
      {Object.keys(m).length > 0 ? (
        <Row gap={space.lg} style={{ backgroundColor: c.surfaceAlt, padding: space.md, borderRadius: radius.md }}>
          {m.kcal != null ? (
            <View>
              <Text style={[type.h2, { color: c.text }]}>{Math.round(Number(m.kcal))}</Text>
              <Text style={[type.caption, { color: c.textFaint }]}>kcal</Text>
            </View>
          ) : null}
          {m.protein_g != null ? (
            <View>
              <Text style={[type.h2, { color: c.protein }]}>{Math.round(Number(m.protein_g))}g</Text>
              <Text style={[type.caption, { color: c.textFaint }]}>protein</Text>
            </View>
          ) : null}
          {m.duration_s != null ? (
            <View>
              <Text style={[type.h2, { color: c.text }]}>{Math.round(Number(m.duration_s) / 60)}m</Text>
              <Text style={[type.caption, { color: c.textFaint }]}>duration</Text>
            </View>
          ) : null}
        </Row>
      ) : null}

      <Row gap={space.lg}>
        <Pressable onPress={() => toggle.mutate({ id: post.id, liked: post.liked_by_me })}>
          <Text style={{ color: post.liked_by_me ? c.danger : c.textDim, fontSize: 14 }}>
            {post.liked_by_me ? '♥' : '♡'} {post.like_count}
          </Text>
        </Pressable>
        <Text style={{ color: c.textDim, fontSize: 14 }}>💬 {post.comment_count}</Text>
      </Row>
    </Card>
  );
}

export function FeedScreen() {
  const c = useTheme();
  const [scope, setScope] = useState<(typeof SCOPES)[number]['id']>('following');
  const { data, isLoading, refetch, isRefetching } = useFeed(scope);
  const qc = useQueryClient();

  // Supabase realtime: a new public post refreshes the discover tab without a
  // pull-to-refresh. Cheap, because we invalidate rather than merge by hand.
  useEffect(() => {
    const channel = supabase
      .channel('feed-posts')
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'posts' }, () => {
        qc.invalidateQueries({ queryKey: ['feed'] });
      })
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, [qc]);

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <View style={{ padding: space.lg, gap: space.md }}>
          <View>
            <Label>Community</Label>
            <H1>Feed</H1>
          </View>
          <Row gap={space.sm}>
            {SCOPES.map((s) => (
              <Chip key={s.id} label={s.label} active={scope === s.id} onPress={() => setScope(s.id)} />
            ))}
          </Row>
        </View>

        {isLoading ? (
          <Loading />
        ) : (
          <FlatList
            data={data ?? []}
            keyExtractor={(p) => p.id}
            contentContainerStyle={{ padding: space.lg, paddingTop: 0, gap: space.md, paddingBottom: 120 }}
            refreshing={isRefetching}
            onRefresh={refetch}
            renderItem={({ item }) => <PostCard post={item} />}
            ListEmptyComponent={
              <Empty
                title={scope === 'following' ? 'Quiet in here' : 'Nothing yet'}
                subtitle={
                  scope === 'following'
                    ? 'Follow a few people and their meals, workouts and recipes show up here.'
                    : 'Be the first to post something today.'
                }
              />
            }
          />
        )}
      </SafeAreaView>
    </Screen>
  );
}
