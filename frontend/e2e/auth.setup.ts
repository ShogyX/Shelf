// Setup project: brings the throwaway e2e backend into the state the app now requires — an admin
// account and a library that actually contains the seeded work — then saves the session cookie for
// every other project to reuse (storageState).
//
// Why via the API rather than backend/app/seed.py: seed.py is also the offline frontend-dev seeder
// and is run by hand; having it mint an admin with a fixed password is a footgun. The API is also
// the real contract, so this doubles as a smoke test of setup → shelf → membership.
//
// Note the library is per-user and shared Works only become visible through a LibraryItem; the only
// REST path that creates one for an already-existing work is placing it on one of your bookshelves.
import fs from "node:fs";
import path from "node:path";
import { expect, request, test as setup } from "@playwright/test";
import { ADMIN, API_URL, AUTH_FILE, SEED_TITLE, SHELF_NAME } from "./support";

setup("create admin and populate the library", async () => {
  // Straight at the API port: /auth/setup refuses non-local connections, and going through the Vite
  // proxy would put a hop in between. The session cookie is host-only ("127.0.0.1") and cookies
  // ignore ports, so the saved state authenticates the browser on the web port too.
  const api = await request.newContext({ baseURL: API_URL });

  const me = await (await api.get("/api/auth/me")).json();
  if (me.needs_setup) {
    const res = await api.post("/api/auth/setup", { data: ADMIN });
    expect(res.ok(), `POST /auth/setup failed: ${res.status()} ${await res.text()}`).toBeTruthy();
  } else {
    // The e2e DB is wiped per run, so this only happens when someone reuses one by hand.
    const res = await api.post("/api/auth/login", { data: ADMIN });
    expect(res.ok(), `POST /auth/login failed: ${res.status()} ${await res.text()}`).toBeTruthy();
  }

  // Locate the seeded work. Works aren't listable outside a library, but an admin can read any by
  // id, and the seeder makes exactly one — scan the low ids and match on its title.
  let workId: number | null = null;
  for (let id = 1; id <= 10 && workId === null; id++) {
    const res = await api.get(`/api/works/${id}`);
    if (!res.ok()) continue;
    const work = await res.json();
    if (String(work.title).startsWith(SEED_TITLE)) workId = work.id;
  }
  expect(workId, `seeded work "${SEED_TITLE}" not found — did \`python -m app.seed\` run?`).not.toBeNull();

  // Shelve it → that also adds the LibraryItem, which is what makes it show up in the UI.
  const shelves = await (await api.get("/api/bookshelves")).json();
  const shelf = shelves.find((s: { name: string }) => s.name === SHELF_NAME);
  const res = shelf
    ? await api.post(`/api/bookshelves/${shelf.id}/works/${workId}`)
    : await api.post("/api/bookshelves", { data: { name: SHELF_NAME, work_ids: [workId] } });
  expect(res.ok(), `shelving work ${workId} failed: ${res.status()} ${await res.text()}`).toBeTruthy();

  // No stale resume marker: the reading specs assert on progress they create themselves.
  await api.delete(`/api/works/${workId}/progress`);

  fs.mkdirSync(path.dirname(AUTH_FILE), { recursive: true });
  await api.storageState({ path: AUTH_FILE });
  await api.dispose();
});
