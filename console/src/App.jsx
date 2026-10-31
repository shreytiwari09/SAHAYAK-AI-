import { HashRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { usePoll } from "./hooks";
import Cases from "./screens/Cases";
import Team from "./screens/Team";
import Queue from "./screens/Queue";
import Chat from "./screens/Chat";
import Audit from "./screens/Audit";
import Metrics from "./screens/Metrics";
import Controls from "./screens/Controls";

function TopBar() {
  const approvals = usePoll(() => api.approvals(), [], 2500);
  const metrics = usePoll(() => api.metrics(), [], 10000);
  const waiting = approvals.data?.length || 0;
  const models = metrics.data?.models;
  const best = models && Object.entries(models).find(([, h]) => h.up)?.[0];

  return (
    <header className="topbar">
      <div className="topbar__inner">
        <NavLink to="/cases" className="wordmark" aria-label="Sahayak, all cases">Sahayak</NavLink>
        <nav className="tabs" aria-label="Sections">
          <NavLink to="/cases">Cases</NavLink>
          <NavLink to="/team">Team</NavLink>
          <NavLink to="/queue">
            Needs a person
            {waiting > 0 && <span className="count measured" aria-label={`${waiting} waiting`}>{waiting}</span>}
          </NavLink>
          <NavLink to="/chat">Chat</NavLink>
          <NavLink to="/audit">Audit</NavLink>
          <NavLink to="/metrics">Metrics</NavLink>
          <NavLink to="/controls">Controls</NavLink>
        </nav>
        <p className="topbar__model">
          {approvals.error ? (
            <span className="bad-text">Server not reachable</span>
          ) : models === null ? (
            "Offline stand-in model"
          ) : best ? (
            <>Model <span className="measured cool">{best}</span></>
          ) : models ? (
            <span className="bad-text">No model answering</span>
          ) : null}
        </p>
      </div>
    </header>
  );
}

function KillSwitchBanner() {
  const policy = usePoll(() => api.policy(), [], 5000);
  if (!policy.data?.kill_switch) return null;
  return (
    <div className="banner" role="status">
      <p>The kill switch is on: every action that changes a merchant’s systems waits for a person.</p>
      <NavLink to="/controls">Change it in controls</NavLink>
    </div>
  );
}

export default function App() {
  return (
    <HashRouter>
      <TopBar />
      <KillSwitchBanner />
      <main className="main" id="main">
        <Routes>
          <Route path="/" element={<Navigate to="/cases" replace />} />
          <Route path="/cases" element={<Cases />} />
          <Route path="/cases/:id" element={<Cases />} />
          <Route path="/team" element={<Team />} />
          <Route path="/queue" element={<Queue />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/metrics" element={<Metrics />} />
          <Route path="/controls" element={<Controls />} />
          <Route path="*" element={<Navigate to="/cases" replace />} />
        </Routes>
      </main>
    </HashRouter>
  );
}
