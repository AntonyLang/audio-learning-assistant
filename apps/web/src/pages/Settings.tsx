import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";

interface TokenRow {
  feed_token_id: string;
  created_at: string;
  revoked_at: string | null;
}

export default function SettingsPage() {
  const [tokens, setTokens] = useState<TokenRow[]>([]);
  const [created, setCreated] = useState<string>("");
  const [error, setError] = useState("");
  const [budget, setBudget] = useState(600);

  const refresh = useCallback(async () => {
    try {
      const [feed, profile] = await Promise.all([api.feedTokens(), api.profile()]);
      setTokens(feed.items);
      setBudget(Number(profile.prefs?.daily_budget_ms ?? 600_000) / 1000);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const createToken = async () => {
    try {
      const token = await api.createFeedToken();
      setCreated(token.url);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  };

  const revoke = async (id: string) => {
    try {
      await api.revokeFeedToken(id);
      setCreated("");
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  };

  const saveBudget = async () => {
    try {
      await api.patchProfile({ daily_budget_ms: Math.round(budget * 1000) });
      setError("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  };

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>设置与订阅</h1>
          <p className="muted">私有 RSS 使用可撤销的 token；轮换后节目 GUID 不变。</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <section className="card">
        <div className="label">每日预算</div>
        <div className="inline">
          <input
            type="number"
            min={60}
            max={3600}
            step={30}
            value={budget}
            onChange={(event) => setBudget(Number(event.target.value))}
          />
          <span className="muted">秒 / 天</span>
          <button className="ghost" onClick={() => void saveBudget()}>
            保存
          </button>
        </div>
        <p className="muted small">修改只影响之后新建的计划，不会改变已安排的节目。</p>
      </section>

      <section className="card">
        <div className="label">私有 RSS</div>
        <button className="primary" onClick={() => void createToken()}>
          创建订阅链接
        </button>
        {created && (
          <div className="token-box">
            <code>{created}</code>
            <button className="ghost" onClick={() => void navigator.clipboard.writeText(created)}>
              复制
            </button>
          </div>
        )}
        <p className="muted small">
          链接只显示一次；数据库只保存 token 的哈希。请勿把完整链接写进日志。
        </p>
        <div className="list">
          {tokens.map((token) => (
            <article className="row" key={token.feed_token_id}>
              <div>
                <div className="row-title">{token.feed_token_id.slice(0, 8)}…</div>
                <div className="muted small">
                  创建于 {new Date(token.created_at).toLocaleString()} ·{" "}
                  {token.revoked_at ? "已撤销" : "有效"}
                </div>
              </div>
              <div className="row-actions">
                {!token.revoked_at && (
                  <button className="ghost" onClick={() => void revoke(token.feed_token_id)}>
                    撤销
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
