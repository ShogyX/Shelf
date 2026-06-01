import { create } from "zustand";
import { api, Me, User } from "./api/client";

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
      set({ me: await api.me(), loaded: true });
    } catch {
      set({ me: { authenticated: false, needs_setup: false, user: null }, loaded: true });
    }
  },
}));

export const useCurrentUser = (): User | null => useAuth((s) => s.me?.user ?? null);
export const useIsAdmin = (): boolean => useAuth((s) => s.me?.user?.role === "admin");
