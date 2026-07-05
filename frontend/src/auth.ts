import { create } from "zustand";
import { api, Me, Permission, User } from "./api/client";
import { setLocale } from "./i18n";

interface AuthState {
  loaded: boolean;
  me: Me | null;
  refresh: () => Promise<void>;
}

export const useAuth = create<AuthState>((set) => ({
  loaded: false,
  me: null,
  refresh: async () => {
    try {
      const me = await api.me();
      // Adopt the signed-in user's saved UI language (and persist it, so the next cold start —
      // before /auth/me resolves — already paints in the right language). Falls through to the
      // localStorage/browser default when the user hasn't chosen one.
      if (me.user?.locale) setLocale(me.user.locale);
      set({ me, loaded: true });
    } catch {
      set({
        me: {
          authenticated: false, needs_setup: false, user: null, allowed_categories: [],
          permissions: [], adult_allowed_categories: [], adult_categories: [],
        },
        loaded: true,
      });
    }
  },
}));

export const useCurrentUser = (): User | null => useAuth((s) => s.me?.user ?? null);
export const useIsAdmin = (): boolean => useAuth((s) => s.me?.user?.role === "admin");

/** True if the current user holds `perm` (admins resolve to all permissions server-side, so this
 *  is simply membership in the resolved set). */
export const useHasPermission = (perm: Permission): boolean =>
  useAuth((s) => (s.me?.permissions ?? []).includes(perm));
