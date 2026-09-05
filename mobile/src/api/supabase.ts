/**
 * Supabase client. Handles auth and direct Storage uploads.
 *
 * Photos go straight from the phone to Storage rather than through our API:
 * the API never has to buffer a 4 MB image, and the upload can resume
 * independently of the analysis request.
 */
import 'react-native-url-polyfill/auto';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient } from '@supabase/supabase-js';
import Constants from 'expo-constants';

const url = Constants.expoConfig?.extra?.supabaseUrl as string;
const anonKey = Constants.expoConfig?.extra?.supabaseAnonKey as string;

export const supabase = createClient(url, anonKey, {
  auth: {
    storage: AsyncStorage,
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: false,
  },
});

/**
 * Upload a local file URI to a bucket under the user's own folder.
 * The storage RLS policy requires the first path segment to be the user id,
 * which is what stops one user reading another's meal photos.
 */
export async function uploadImage(
  bucket: 'meal-photos' | 'equipment-photos' | 'recipe-photos' | 'avatars' | 'post-media',
  localUri: string,
): Promise<string> {
  const { data: sessionData } = await supabase.auth.getUser();
  const uid = sessionData.user?.id;
  if (!uid) throw new Error('Not signed in.');

  const ext = localUri.split('.').pop()?.split('?')[0] ?? 'jpg';
  const path = `${uid}/${Date.now()}-${Math.random().toString(36).slice(2, 8)}.${ext}`;

  const response = await fetch(localUri);
  const blob = await response.arrayBuffer();

  const { error } = await supabase.storage.from(bucket).upload(path, blob, {
    contentType: ext === 'png' ? 'image/png' : 'image/jpeg',
    upsert: false,
  });
  if (error) throw new Error(`Upload failed: ${error.message}`);
  return path;
}

export async function signedUrl(bucket: string, path: string, seconds = 3600) {
  const { data } = await supabase.storage.from(bucket).createSignedUrl(path, seconds);
  return data?.signedUrl ?? null;
}
