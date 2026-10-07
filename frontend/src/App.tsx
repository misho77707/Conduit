import { Navigate, Route, HashRouter, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
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
import { ComingSoon } from "./pages/ComingSoon";
import { Compare } from "./pages/Compare";

function Private({ children }: { children: JSX.Element }) {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

function Shell() {
  const { user } = useAuth();
  if (user) return <Navigate to="/connections" replace />;
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
                        <Route path="/" element={<Navigate to="/connections" replace />} />
                        <Route path="/connections" element={<Connections />} />
                        <Route path="/upload" element={<Wizard />} />
                        <Route path="/jobs" element={<Jobs />} />
                        <Route path="/jobs/:id" element={<Jobs />} />
                        <Route path="/monitor" element={<Monitor />} />
                        <Route path="/pipelines" element={<Pipelines />} />
                        <Route path="/pipelines/:id" element={<Builder />} />
                        <Route path="/compare" element={<Compare />} />
                        <Route path="/compare/:id" element={<Compare />} />
                        <Route path="/control" element={<ComingSoon title="Control & Setup" icon="tune" note="Environment setup, tool management and secrets. Coming later." />} />
                        <Route path="*" element={<Navigate to="/connections" replace />} />
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
