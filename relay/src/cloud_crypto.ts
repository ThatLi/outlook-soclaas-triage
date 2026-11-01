import type { Env } from "./index";

function b64(bytes: Uint8Array): string {
  return btoa(Array.from(bytes, b => String.fromCharCode(b)).join(""));
}
function unb64(value: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(atob(value), c => c.charCodeAt(0));
}
async function encryptionKey(env: Env): Promise<CryptoKey> {
  const bytes = unb64(env.RELAY_ENCRYPTION_KEY.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - env.RELAY_ENCRYPTION_KEY.length % 4) % 4));
  return crypto.subtle.importKey("raw", bytes, "AES-GCM", false, ["encrypt", "decrypt"]);
}
export async function seal(env: Env, id: string, value: unknown): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(id) },
    await encryptionKey(env), new TextEncoder().encode(JSON.stringify(value)),
  );
  return JSON.stringify({ iv: b64(iv), ciphertext: b64(new Uint8Array(ciphertext)) });
}
export async function unseal<T = string[]>(env: Env, id: string, value: string): Promise<T> {
  const data = JSON.parse(value);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: unb64(data.iv), additionalData: new TextEncoder().encode(id) },
    await encryptionKey(env), unb64(data.ciphertext),
  );
  return JSON.parse(new TextDecoder().decode(plain));
}
