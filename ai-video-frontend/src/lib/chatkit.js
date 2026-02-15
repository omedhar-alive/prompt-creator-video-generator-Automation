// src/lib/chatkit.js

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

/**
 * Create a ChatKit session via your backend.
 * Backend route: POST /chatkit/session
 * Body: { user_id }
 * Returns: { session_id, client_secret }
 */
export async function createChatkitSession(userId) {
  const res = await fetch(`${API_BASE}/chatkit/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });

  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`chatkit/session failed (${res.status}): ${txt}`);
  }

  return await res.json();
}
