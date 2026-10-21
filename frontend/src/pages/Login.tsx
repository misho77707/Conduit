import { useState, type FormEvent } from "react";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { ApiError } from "../lib/api";
import { Icon } from "../ui/Icon";
import { IconButton } from "../ui/Button";
import { Field } from "../ui/Field";
import { Input } from "../ui/Input";
import { Button } from "../ui/Button";
import { Logo } from "../shell/Logo";

export function Login() {
  const { login } = useAuth();
  const { theme, toggle } = useTheme();
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!user.trim() || !pass) return setErr("Enter your username and password.");
    setErr("");
    setBusy(true);
    try {
      await login(user, pass);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 24, position: "relative", height: "100vh" }}>
      <IconButton icon={theme === "dark" ? "light_mode" : "dark_mode"} title="Toggle theme" onClick={toggle} size={32} style={{ position: "absolute", top: 16, right: 16, border: "1px solid var(--border-2)", background: "var(--surface)" }} />
      <form onSubmit={onSubmit} style={{ width: 360, maxWidth: "100%", display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Logo size={28} />
          <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: "-0.01em" }}>Conduit</span>
        </div>
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: 24, display: "flex", flexDirection: "column", gap: 16, boxShadow: "var(--shadow-1)" }}>
          <div>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Sign in</h1>
            <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>With your Conduit account. Ask a superadmin if you don't have one.</p>
          </div>
          <Field label="Username">
            <Input value={user} onChange={(e) => setUser(e.target.value)} autoFocus />
          </Field>
          <Field label="Password">
            <Input type="password" mono={false} value={pass} onChange={(e) => setPass(e.target.value)} />
          </Field>
          {err && (
            <div style={{ display: "flex", gap: 6, alignItems: "center", color: "var(--err)", fontSize: 12 }}>
              <Icon name="error" style={{ fontSize: 16 }} />
              {err}
            </div>
          )}
          <Button type="submit" variant="primary" height={36} disabled={busy} style={{ justifyContent: "center" }}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </div>
      </form>
    </div>
  );
}
