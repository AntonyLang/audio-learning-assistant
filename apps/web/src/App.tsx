import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import Inbox from "./pages/Inbox";
import TodayPage from "./pages/Today";
import EpisodePage from "./pages/EpisodePage";
import SettingsPage from "./pages/Settings";
import Login from "./pages/Login";

export type Route =
  | { name: "inbox" }
  | { name: "today" }
  | { name: "settings" }
  | { name: "episode"; id: string };

interface Profile {
  email: string;
  timezone: string;
  prefs: Record<string, unknown>;
}

function routeFromHash(): Route {
  const match = location.hash.match(/^#\/episode\/([a-f0-9-]{36})$/i);
  if (match) return { name: "episode", id: match[1] };
  if (location.hash === "#/inbox") return { name: "inbox" };
  if (location.hash === "#/settings") return { name: "settings" };
  return { name: "today" };
}

function navigate(route: Route) {
  location.hash = route.name === "episode" ? `/episode/${route.id}` : `/${route.name}`;
}

export default function App() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [route, setRoute] = useState<Route>(routeFromHash);
  const [booting, setBooting] = useState(true);
  const [health, setHealth] = useState<{ provider_mode: string; inline_worker: boolean } | null>(
    null,
  );

  const loadProfile = useCallback(async () => {
    try {
      const me = await api.profile();
      setProfile({ email: me.email, timezone: me.timezone, prefs: me.prefs });
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        console.error(error);
      }
      setProfile(null);
    } finally {
      setBooting(false);
    }
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    void loadProfile();
    void api
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [loadProfile]);

  const onLogin = useCallback(() => {
    setBooting(true);
    void loadProfile();
  }, [loadProfile]);

  const onLogout = useCallback(async () => {
    await api.logout();
    setProfile(null);
  }, []);

  if (booting) {
    return <div className="boot">正在加载…</div>;
  }

  if (!profile) {
    return <Login onLogin={onLogin} />;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" />
          <div>
            <strong>音频学习助手</strong>
            <div className="brand-sub">有来源的中文短讲</div>
          </div>
        </div>
        <nav>
          <button
            className={route.name === "today" ? "active" : ""}
            onClick={() => navigate({ name: "today" })}
          >
            今日收听
          </button>
          <button
            className={route.name === "inbox" ? "active" : ""}
            onClick={() => navigate({ name: "inbox" })}
          >
            收件箱
          </button>
          <button
            className={route.name === "settings" ? "active" : ""}
            onClick={() => navigate({ name: "settings" })}
          >
            设置与订阅
          </button>
        </nav>
        <div className="sidebar-foot">
          <div className="muted small">{profile.email}</div>
          {health && (
            <div className="muted small">
              {health.provider_mode === "fake" ? "演示模式" : "个人学习空间"}
            </div>
          )}
          <button className="link" onClick={() => void onLogout()}>
            退出登录
          </button>
        </div>
      </aside>
      <main className="main">
        {route.name === "today" && (
          <TodayPage onOpenEpisode={(id) => navigate({ name: "episode", id })} />
        )}
        {route.name === "inbox" && (
          <Inbox onOpenEpisode={(id) => navigate({ name: "episode", id })} />
        )}
        {route.name === "settings" && <SettingsPage />}
        {route.name === "episode" && (
          <EpisodePage key={route.id} id={route.id} onBack={() => navigate({ name: "today" })} />
        )}
      </main>
    </div>
  );
}
