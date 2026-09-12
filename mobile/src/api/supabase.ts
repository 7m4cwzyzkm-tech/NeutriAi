/**
 * Supabase client. Handles auth and direct Storage uploads.
 *
 * Photos go straight from the phone to Storage rather than through our API:
 * the API never has to buffer a 4 MB image, and the upload can resume
 * independently of the analysis request.
 */
import 'react-native-url-polyfill/auto';
import { Image } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient } from '@supabase/supabase-js';
import Constants from 'expo-constants';
import { File } from 'expo-file-system';
import * as ImageManipulator from 'expo-image-manipulator';

const url = Constants.expoConfig?.extra?.supabaseUrl as string | undefined;
const anonKey = Constants.expoConfig?.extra?.supabaseAnonKey as string | undefined;

/**
 * Fail with a sentence someone can act on.
 *
 * Unset config used to surface as a supabase-js internal error at import time,
 * before any screen mounted — a white screen and a stack trace pointing at
 * node_modules, which says nothing about the actual cause (an empty .env).
 */
if (!url || !anonKey) {
  throw new Error(
    'NeutriAI is not configured. Copy mobile/.env.example to mobile/.env, fill in ' +
    'EXPO_PUBLIC_SUPABASE_URL and EXPO_PUBLIC_SUPABASE_ANON_KEY, then restart ' +
    'with: npx expo start -c',
  );
}

export const supabase = createClient(url, anonKey, {
  auth: {
    storage: AsyncStorage,
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: false,
  },
});

// ---------------------------------------------------------------------------
// Image preparation
// ---------------------------------------------------------------------------

/**
 * Long edge, in pixels, that we upload at.
 *
 * Both vision models downscale anything larger than roughly this before they
 * look at it, so a 4032 px photo is resized twice and the extra pixels reach
 * the model in neither case. Sending them costs the user upload time on mobile
 * data and costs us storage, for zero accuracy.
 *
 * Safe for the portion estimator specifically: every geometric input it uses
 * is a *fraction* of the frame (bbox area ratio, plate diameter as a share of
 * frame width), and the depth rung works from lens FOV and distance, not from
 * pixel counts. Uniform scaling leaves all of those unchanged. What must not
 * change is the aspect ratio — hence resizing by one dimension only.
 */
const MAX_EDGE_PX = 1568;
const JPEG_QUALITY = 0.82;

/**
 * Image.getSize takes callbacks and, on some URIs, calls neither. An unbounded
 * promise there hangs the entire scan with no error — the user just watches a
 * spinner until the request times out and blames their connection. Bounded, a
 * silent failure costs us the downscale and nothing else.
 */
function measure(uri: string, timeoutMs = 3000): Promise<{ width: number; height: number } | null> {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v: { width: number; height: number } | null) => {
      if (settled) return;
      settled = true;
      resolve(v);
    };
    setTimeout(() => done(null), timeoutMs);
    Image.getSize(uri, (width: number, height: number) => done({ width, height }), () => done(null));
  });
}

/**
 * Downscale if the photo is bigger than the model will use.
 * Returns the original URI unchanged on any failure — a scan that uploads a
 * large photo is far better than a scan that does not happen.
 */
async function prepareImage(localUri: string): Promise<{ uri: string; jpeg: boolean }> {
  try {
    const size = await measure(localUri);
    if (!size) return { uri: localUri, jpeg: false };
    const longEdge = Math.max(size.width, size.height);
    if (longEdge <= MAX_EDGE_PX) return { uri: localUri, jpeg: false };

    const action = size.width >= size.height
      ? { resize: { width: MAX_EDGE_PX } }
      : { resize: { height: MAX_EDGE_PX } };

    const out = await ImageManipulator.manipulateAsync(localUri, [action], {
      compress: JPEG_QUALITY,
      format: ImageManipulator.SaveFormat.JPEG,
    });
    return { uri: out.uri, jpeg: true };
  } catch {
    return { uri: localUri, jpeg: false };
  }
}

// ---------------------------------------------------------------------------
// Local file -> bytes
// ---------------------------------------------------------------------------

/**
 * Read a local file as raw bytes.
 *
 * Two wrong approaches were tried here, and both are worth recording because
 * each looked correct:
 *
 * 1. `fetch(fileUri).arrayBuffer()` — React Native's Blob is a native handle,
 *    not memory, so reading a file:// URI through it yields an empty buffer.
 *    Supabase then stores a zero-byte object and the failure surfaces later as
 *    a confusing vision error rather than an upload error.
 *
 * 2. `FileSystem.readAsStringAsync(uri, { encoding: Base64 })` — correct for
 *    SDK 52, gone by SDK 57. expo-file-system replaced its function API with
 *    File/Directory/Paths classes. The old names are still EXPORTED, as stubs
 *    that throw and point at 'expo-file-system/legacy', which is why this
 *    typechecked cleanly and failed at runtime.
 *
 * The current API reads bytes directly, so the hand-rolled base64 decoder this
 * replaced is gone too — the platform does it, and does it faster.
 */
async function readBytes(uri: string): Promise<ArrayBuffer> {
  const buffer = await new File(uri).arrayBuffer();
  if (!buffer || buffer.byteLength === 0) {
    throw new Error('That photo could not be read from your device.');
  }
  return buffer;
}

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

  const prepared = await prepareImage(localUri);
  const rawExt = prepared.uri.split('.').pop()?.split('?')[0]?.toLowerCase() ?? 'jpg';
  const ext = prepared.jpeg ? 'jpg' : rawExt === 'png' ? 'png' : 'jpg';
  const path = `${uid}/${Date.now()}-${Math.random().toString(36).slice(2, 8)}.${ext}`;

  const bytes = await readBytes(prepared.uri);

  const { error } = await supabase.storage.from(bucket).upload(path, bytes, {
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
