import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { auth as authApi, getToken, onSessionEvent, setToken, type Role, type UserOut } from "./api";
import { useToast } from "./toast";

/** What the signed-in role may use. The backend enforces all of it (app/deps.py); this only hides what a role
 * can't use. */
export interface Can {
  edit: boolean; // admin, superadmin: connections, loads, pipelines, Compare runs, Monitor checks
  manageUsers: boolean; // superadmin
  viewer: boolean; // read-only
}

export function canOf(role: Role | undefined): Can {
  return { edit: role === "admin" || role === "superadmin", manageUsers: role === "superadmin", viewer: role === "viewer" };
}

/** Where a role lands after signing in, and where a route it can't use sends it. */
export function homeOf(user: UserOut | null): string {
  return user?.role === "viewer" ? "/monitor" : "/connections";
}

export const ROLE_LABEL: Record<Role, string> = { superadmin: "Superadmin", admin: "Admin", viewer: "Viewer" };

interface AuthState {
  user: UserOut | null;
  loading: boolean;
  can: Can;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  /** Change your own password; the API ends your other sessions and this one continues on a fresh token. */
  changePassword: (current: string, next: string) => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [user, setUser] = useState<UserOut | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getToken()) {
      setLoading(false);
      return;
    }
    authApi
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  const signedIn = useRef(false);
  signedIn.current = !!user;

  useEffect(() => onSessionEvent((e) => {
    if (e === "signed_out") {
      if (signedIn.current) toast("warn", "Your session ended", "Sign in again (your password or role may have changed).");
      setUser(null);
    } else {
      authApi.me().then(setUser).catch(() => {});  // must_change_password: App shows the set-a-new-password screen
    }
  }), [toast]);

  const login = useCallback(async (username: string, password: string) => {
    const tok = await authApi.login(username, password);
    setToken(tok.access_token);
    setUser(await authApi.me());
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  const changePassword = useCallback(async (current: string, next: string) => {
    const tok = await authApi.changePassword(current, next);
    setToken(tok.access_token);
    setUser(await authApi.me());
  }, []);

  const can = useMemo(() => canOf(user?.role), [user?.role]);

  return <AuthContext.Provider value={{ user, loading, can, login, logout, changePassword }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

/** Shorthand for components that only hide / show things. */
export function useCan(): Can {
  return useAuth().can;
}
