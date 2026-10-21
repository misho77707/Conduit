import { useEffect, useState, type FormEvent } from "react";
import { usersApi, ApiError, type Role, type UserAdminOut } from "../lib/api";
import { ROLE_LABEL, useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { relativeTime } from "../lib/format";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { Icon } from "../ui/Icon";
import { Input, Select } from "../ui/Input";
import { Modal } from "../ui/Modal";
import { RowMenu } from "../ui/RowMenu";
import { MIN_PASSWORD, passwordProblem } from "./account/Password";

const ROLE_HELP: Record<Role, string> = {
  superadmin: "Everything, including managing users.",
  admin: "Everything except users: connections, loads, pipelines, schedules, Compare, Monitor.",
  viewer: "Read-only: Monitor, Jobs, Pipelines and finished comparisons. No data rows, no changes.",
};

/** A temporary password that passes the rules: 14 letters and digits without look-alikes (0/O, 1/l/I). */
function generatePassword(): string {
  const abc = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint32Array(14));
  return Array.from(bytes, (b) => abc[b % abc.length]).join("");
}

export function Users() {
  const { user: me } = useAuth();
  const toast = useToast();
  const [users, setUsers] = useState<UserAdminOut[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [roleOf, setRoleOf] = useState<UserAdminOut | null>(null);
  const [resetOf, setResetOf] = useState<UserAdminOut | null>(null);

  const load = () => usersApi.list().then(setUsers).catch((e) => toast("err", "Could not load users", e instanceof Error ? e.message : undefined));
  useEffect(() => { load(); }, []);

  if (!users) return null;
  const activeSupers = users.filter((u) => u.role === "superadmin" && u.is_active);
  const isLast = (u: UserAdminOut) => activeSupers.length === 1 && activeSupers[0].id === u.id;
  const lastHint = (u: UserAdminOut) => `${u.username} is the last active superadmin: make another user superadmin first`;

  async function setActive(u: UserAdminOut, is_active: boolean) {
    try { await usersApi.update(u.id, { is_active }); toast("ok", is_active ? `${u.username} can sign in again` : `${u.username} is deactivated`, is_active ? undefined : "Their sessions ended."); load(); }
    catch (e) { toast("err", "Could not change the user", e instanceof Error ? e.message : undefined); }
  }
  async function remove(u: UserAdminOut) {
    const self = u.id === me?.id;
    if (!confirm(`Delete ${u.username}?${self ? " That's you: you'll be signed out." : ""}\n\nTheir connections, pipelines and jobs stay.`)) return;
    try { await usersApi.remove(u.id); toast("ok", `Deleted ${u.username}`); if (!self) load(); }
    catch (e) { toast("err", "Could not delete the user", e instanceof Error ? e.message : undefined); }
  }

  return (
    <div style={{ padding: "24px 28px 48px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1200 }}>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Users</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>Who can sign in, and what each role can do.</p>
        </div>
        <Button variant="primary" icon="person_add" onClick={() => setAdding(true)}>Add user</Button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))", gap: 1, background: "var(--border)", border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
        {(Object.keys(ROLE_HELP) as Role[]).map((r) => (
          <div key={r} style={{ background: "var(--surface)", padding: "10px 14px" }}>
            <div style={{ fontWeight: 500 }}>{ROLE_LABEL[r]} <span style={{ fontSize: 12, color: "var(--text-3)", fontWeight: 400 }}>· {users.filter((u) => u.role === r).length}</span></div>
            <div style={{ fontSize: 12, color: "var(--text-2)", marginTop: 2 }}>{ROLE_HELP[r]}</div>
          </div>
        ))}
      </div>

      <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}>
        <div style={{ minWidth: 860 }}>
          <div style={{ display: "grid", gridTemplateColumns: GRID, gap: 12, padding: "0 16px", height: 36, alignItems: "center", borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 500, color: "var(--text-3)" }}>
            <span>Username</span><span>Role</span><span>Status</span><span>Created by</span><span>Last sign-in</span><span />
          </div>
          {users.map((u) => {
            const self = u.id === me?.id, last = isLast(u);
            return (
              <div key={u.id} className="hover-surface2" style={{ display: "grid", gridTemplateColumns: GRID, gap: 12, padding: "0 16px", minHeight: 48, alignItems: "center", borderBottom: "1px solid var(--border)" }}>
                <span style={{ display: "flex", gap: 8, alignItems: "center", minWidth: 0 }}>
                  <span style={{ fontFamily: "'Geist Mono',monospace", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis" }}>{u.username}</span>
                  {self && <Chip text="you" />}
                </span>
                <span style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  {ROLE_LABEL[u.role]}
                  {last && <span title={lastHint(u)}><Chip text="last superadmin" tone="var(--accent)" /></span>}
                </span>
                <span style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5, color: u.is_active ? "var(--ok)" : "var(--text-3)" }}>
                    <span style={{ width: 7, height: 7, borderRadius: "50%", background: u.is_active ? "var(--ok)" : "var(--idle)" }} />{u.is_active ? "Active" : "Deactivated"}
                  </span>
                  {u.must_change_password && u.is_active && <Chip text="temporary password" tone="var(--warn)" />}
                </span>
                <span style={{ fontSize: 12, color: "var(--text-2)" }}>
                  {u.created_by || "—"}<span style={{ color: "var(--text-3)" }}> · {relativeTime(u.created_at)}</span>
                </span>
                <span style={{ fontSize: 12, color: u.last_login_at ? "var(--text-2)" : "var(--text-3)" }}>{u.last_login_at ? relativeTime(u.last_login_at) : "never"}</span>
                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  <RowMenu width={200} items={[
                    { icon: "badge", label: "Change role", onClick: () => setRoleOf(u), disabled: last, hint: last ? lastHint(u) : undefined },
                    ...(self ? [] : [{ icon: "key", label: "Reset password", onClick: () => setResetOf(u) }]),
                    u.is_active
                      ? { icon: "block", label: "Deactivate", onClick: () => setActive(u, false), disabled: last, hint: last ? lastHint(u) : "They can't sign in; their sessions end" }
                      : { icon: "check_circle", label: "Reactivate", onClick: () => setActive(u, true) },
                    { icon: "delete", label: "Delete", tone: "var(--err)", onClick: () => remove(u), disabled: last, hint: last ? lastHint(u) : "Their connections, pipelines and jobs stay" },
                  ]} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-3)" }}>
        There must always be one active superadmin. Changing someone's role, resetting their password or deactivating them ends their sessions.
        Your own password: account menu, top right.
      </div>

      {adding && <AddUserModal onClose={() => setAdding(false)} onCreated={load} />}
      {roleOf && <RoleModal user={roleOf} self={roleOf.id === me?.id} onClose={() => setRoleOf(null)} onSaved={() => { setRoleOf(null); load(); }} />}
      {resetOf && <ResetModal user={resetOf} onClose={() => setResetOf(null)} onDone={load} />}
    </div>
  );
}

const GRID = "minmax(180px,1.3fr) minmax(150px,1fr) minmax(170px,1.1fr) minmax(150px,1fr) 120px 40px";

function Chip({ text, tone = "var(--text-3)" }: { text: string; tone?: string }) {
  return <span style={{ fontSize: 10, fontWeight: 600, padding: "1px 6px", borderRadius: 4, border: `1px solid ${tone}`, color: tone, whiteSpace: "nowrap" }}>{text}</span>;
}

/** A temporary password field with a Generate button. */
function TempPassword({ username, value, onChange }: { username: string; value: string; onChange: (v: string) => void }) {
  const problem = value ? passwordProblem(username, value) : null;
  return (
    <Field label="Temporary password" hint={problem || `At least ${MIN_PASSWORD} characters. They replace it at their first sign-in.`}>
      <div style={{ display: "flex", gap: 8 }}>
        <Input mono value={value} onChange={(e) => onChange(e.target.value)} style={{ flex: 1 }} autoComplete="off" />
        <Button type="button" icon="casino" onClick={() => onChange(generatePassword())}>Generate</Button>
      </div>
    </Field>
  );
}

/** Shown once after a password was set: the only time it can be copied. */
function ShowOnce({ username, password }: { username: string; password: string }) {
  const toast = useToast();
  const copy = () => navigator.clipboard?.writeText(password).then(() => toast("ok", "Copied")).catch(() => {});
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", color: "var(--ok)" }}><Icon name="check_circle" />Temporary password for <b>{username}</b></div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <code style={{ flex: 1, padding: "8px 10px", borderRadius: 6, background: "var(--code-bg)", border: "1px solid var(--border)", fontFamily: "'Geist Mono',monospace", fontSize: 14, letterSpacing: "0.04em" }}>{password}</code>
        <Button icon="content_copy" onClick={copy}>Copy</Button>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-2)" }}>Give it to {username} over a channel you trust. It isn't shown again; they set their own password at their first sign-in.</div>
    </div>
  );
}

function AddUserModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [password, setPassword] = useState(generatePassword);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<UserAdminOut | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = !username.trim() ? "Enter a username." : passwordProblem(username, password);
    if (problem) return setErr(problem);
    setErr("");
    setBusy(true);
    try { setDone(await usersApi.create({ username: username.trim(), role, temporary_password: password })); onCreated(); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "Could not add the user."); }
    finally { setBusy(false); }
  }

  if (done) {
    return (
      <Modal width={480} title="User added" onClose={onClose} footer={<><div style={{ flex: 1 }} /><Button variant="primary" onClick={onClose}>Done</Button></>}>
        <ShowOnce username={done.username} password={password} />
      </Modal>
    );
  }
  return (
    <Modal width={480} title="Add user" sub="They sign in with a temporary password and must set their own first." onClose={onClose}>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label="Username">
          <Input mono autoFocus value={username} onChange={(e) => setUsername(e.target.value)} placeholder="nino" autoComplete="off" />
        </Field>
        <RolePicker value={role} onChange={setRole} />
        <TempPassword username={username} value={password} onChange={setPassword} />
        {err && <div style={{ display: "flex", gap: 6, alignItems: "center", color: "var(--err)", fontSize: 12 }}><Icon name="error" style={{ fontSize: 16 }} />{err}</div>}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>{busy ? "Adding…" : "Add user"}</Button>
        </div>
      </form>
    </Modal>
  );
}

function RolePicker({ value, onChange }: { value: Role; onChange: (r: Role) => void }) {
  return (
    <Field label="Role" hint={ROLE_HELP[value]}>
      <Select value={value} onChange={(e) => onChange(e.target.value as Role)}>
        {(Object.keys(ROLE_HELP) as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
      </Select>
    </Field>
  );
}

function RoleModal({ user, self, onClose, onSaved }: { user: UserAdminOut; self: boolean; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [role, setRole] = useState<Role>(user.role);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    if (role === user.role) return onClose();
    setBusy(true);
    try { await usersApi.update(user.id, { role }); toast("ok", `${user.username} is now ${ROLE_LABEL[role].toLowerCase()}`); onSaved(); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "Could not change the role."); }
    finally { setBusy(false); }
  }

  return (
    <Modal width={440} title={`Role of ${user.username}`} onClose={onClose}
      footer={<><div style={{ flex: 1 }} /><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</Button></>}>
      <RolePicker value={role} onChange={setRole} />
      <div style={{ fontSize: 12, color: "var(--text-2)" }}>
        {self ? "You'll be signed out and sign in again with the new role." : `${user.username}'s sessions end; they sign in again with the new role.`}
      </div>
      {err && <div style={{ color: "var(--err)", fontSize: 12 }}>{err}</div>}
    </Modal>
  );
}

function ResetModal({ user, onClose, onDone }: { user: UserAdminOut; onClose: () => void; onDone: () => void }) {
  const [password, setPassword] = useState(generatePassword);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function reset() {
    const problem = passwordProblem(user.username, password);
    if (problem) return setErr(problem);
    setBusy(true);
    try { await usersApi.resetPassword(user.id, password); setDone(true); onDone(); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "Could not reset the password."); }
    finally { setBusy(false); }
  }

  return (
    <Modal width={480} title={done ? "Password reset" : `Reset the password of ${user.username}`} onClose={onClose}
      sub={done ? undefined : "Their sessions end, and they must set their own password at their next sign-in."}
      footer={<><div style={{ flex: 1 }} />{done ? <Button variant="primary" onClick={onClose}>Done</Button>
        : <><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" disabled={busy} onClick={reset}>{busy ? "Resetting…" : "Reset password"}</Button></>}</>}>
      {done ? <ShowOnce username={user.username} password={password} /> : (
        <>
          <TempPassword username={user.username} value={password} onChange={setPassword} />
          {err && <div style={{ color: "var(--err)", fontSize: 12 }}>{err}</div>}
        </>
      )}
    </Modal>
  );
}
