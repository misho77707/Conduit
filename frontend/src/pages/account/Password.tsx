import { useState, type FormEvent } from "react";
import { useAuth } from "../../lib/auth";
import { useTheme } from "../../lib/theme";
import { useToast } from "../../lib/toast";
import { ApiError } from "../../lib/api";
import { Field } from "../../ui/Field";
import { Input } from "../../ui/Input";
import { Button, IconButton } from "../../ui/Button";
import { Icon } from "../../ui/Icon";
import { Modal } from "../../ui/Modal";
import { Logo } from "../../shell/Logo";

export const MIN_PASSWORD = 10; // backend/app/users.py

/** Why `next` can't be the new password (the same rules the API applies), or null. */
export function passwordProblem(username: string, next: string, current?: string): string | null {
  if (next.length < MIN_PASSWORD) return `At least ${MIN_PASSWORD} characters.`;
  if (next.trim().toLowerCase() === username.trim().toLowerCase()) return "It can't be the username.";
  if (current !== undefined && next === current) return "Choose a password different from the current one.";
  return null;
}

/** Current password + new password twice; changes it through the API (which ends your other sessions). */
function PasswordForm({ forced, onDone, footer }: {
  forced?: boolean; onDone: () => void; footer: (busy: boolean) => JSX.Element;
}) {
  const { user, changePassword } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = !current ? `Enter your ${forced ? "temporary" : "current"} password.` : passwordProblem(user?.username || "", next, current)
      || (next !== again ? "The two new passwords differ." : null);
    if (problem) return setErr(problem);
    setErr("");
    setBusy(true);
    try {
      await changePassword(current, next);
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Field label={forced ? "Temporary password" : "Current password"}>
        <Input type="password" mono={false} autoFocus value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
      </Field>
      <Field label="New password" hint={`At least ${MIN_PASSWORD} characters, not your username.`}>
        <Input type="password" mono={false} value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
      </Field>
      <Field label="New password again">
        <Input type="password" mono={false} value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="new-password" />
      </Field>
      {err && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", color: "var(--err)", fontSize: 12 }}>
          <Icon name="error" style={{ fontSize: 16 }} />{err}
        </div>
      )}
      {footer(busy)}
    </form>
  );
}

/** Shown right after signing in with a temporary password: nothing else is reachable until it is replaced. */
export function SetPasswordScreen() {
  const { user, logout } = useAuth();
  const { theme, toggle } = useTheme();
  const toast = useToast();
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 24, position: "relative", height: "100vh", background: "var(--bg)", color: "var(--text)" }}>
      <IconButton icon={theme === "dark" ? "light_mode" : "dark_mode"} title="Toggle theme" onClick={toggle} size={32} style={{ position: "absolute", top: 16, right: 16, border: "1px solid var(--border-2)", background: "var(--surface)" }} />
      <div style={{ width: 380, maxWidth: "100%", display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Logo size={28} />
          <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: "-0.01em" }}>Conduit</span>
        </div>
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: 24, display: "flex", flexDirection: "column", gap: 16, boxShadow: "var(--shadow-1)" }}>
          <div>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Set a new password</h1>
            <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>
              <b style={{ fontWeight: 500, color: "var(--text)" }}>{user?.username}</b> has a temporary password. Choose your own to continue.
            </p>
          </div>
          <PasswordForm forced onDone={() => toast("ok", "Password set")} footer={(busy) => (
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Button type="button" variant="ghost" onClick={logout}>Sign out</Button>
              <div style={{ flex: 1 }} />
              <Button type="submit" variant="primary" disabled={busy}>{busy ? "Saving…" : "Set password"}</Button>
            </div>
          )} />
        </div>
      </div>
    </div>
  );
}

export function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  return (
    <Modal width={440} title="Change password" sub="Your other sessions end; this one stays signed in." onClose={onClose}>
      <PasswordForm onDone={() => { toast("ok", "Password changed"); onClose(); }} footer={(busy) => (
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>{busy ? "Saving…" : "Change password"}</Button>
        </div>
      )} />
    </Modal>
  );
}
