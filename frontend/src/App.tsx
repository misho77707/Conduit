import { lazy, Suspense } from "react";
import { Navigate, Route, HashRouter, Routes } from "react-router-dom";
import { AuthProvider, homeOf, useAuth, type Can } from "./lib/auth";
import { ThemeProvider } from "./lib/theme";
import { ToastProvider } from "./lib/toast";
import { AppShell } from "./shell/AppShell";
import { Login } from "./pages/Login";
import { Connections } from "./pages/Connections";
import { Wizard } from "./pages/upload/Wizard";
import { Jobs } from "./pages/Jobs";
import { Monitor } from "./pages/Monitor";
import { Pipelines } from "./pages/Pipelines";
import { Builder } from "./pages/pipelines/Builder";
import { Compare } from "./pages/Compare";
import { SetPasswordScreen } from "./pages/account/Password";
// Control & Setup is for admins only: its code loads when first opened.
const ControlSetup = lazy(() => import("./pages/control/ControlSetup").then((m) => ({ default: m.ControlSetup })));

function Private({ children }: { children: JSX.Element }) {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user) return <Navigate to="/login" replace />;
  if (user.must_change_password) return <SetPasswordScreen />; // nothing else until a temporary password is replaced
  return children;
}

/** A screen a role can't use sends it home (the API refuses its calls anyway). */
function Only({ can: need, children }: { can: keyof Can; children: JSX.Element }) {
  const { user, can } = useAuth();
  return can[need] ? children : <Navigate to={homeOf(user)} replace />;
}

function Home() {
  const { user } = useAuth();
  return <Navigate to={homeOf(user)} replace />;
}

function Shell() {
  const { user } = useAuth();
  if (user) return <Navigate to={homeOf(user)} replace />;
  return <Login />;
}

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <AuthProvider>
          <HashRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <Routes>
              <Route path="/login" element={<Shell />} />
              <Route
                path="/*"
                element={
                  <Private>
                    <AppShell>
                      <Routes>
                        <Route path="/" element={<Home />} />
                        <Route path="/connections" element={<Only can="edit"><Connections /></Only>} />
                        <Route path="/upload" element={<Only can="edit"><Wizard /></Only>} />
                        <Route path="/jobs" element={<Jobs />} />
                        <Route path="/jobs/:id" element={<Jobs />} />
                        <Route path="/monitor" element={<Monitor />} />
                        <Route path="/pipelines" element={<Pipelines />} />
                        <Route path="/pipelines/:id" element={<Only can="edit"><Builder /></Only>} />
                        <Route path="/compare" element={<Compare />} />
                        <Route path="/compare/:id" element={<Compare />} />
                        <Route path="/users" element={<Navigate to="/control/users" replace />} />{/* moved into Control & Setup */}
                        <Route path="/control" element={<Only can="edit"><Suspense fallback={null}><ControlSetup /></Suspense></Only>} />
                        <Route path="/control/:tab" element={<Only can="edit"><Suspense fallback={null}><ControlSetup /></Suspense></Only>} />
                        <Route path="*" element={<Home />} />
                      </Routes>
                    </AppShell>
                  </Private>
                }
              />
            </Routes>
          </HashRouter>
        </AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
