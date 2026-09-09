'use client';

import { useEffect, useState } from 'react';

export type WeeklyScore = { player_id: string; season: number; week: number; total: number };
export type RosterScore = { average: number; lastWeek: number | null };

export function summarizeRosterScores(rows: WeeklyScore[], season: number, currentWeek: number): Record<string, RosterScore> {
  const byPlayer = new Map<string, Map<number, number>>();
  for (const row of rows) {
    if (row.season !== season || row.week < 1 || row.week >= currentWeek || !Number.isFinite(row.total)) continue;
    const weeks = byPlayer.get(row.player_id) || new Map<number, number>();
    weeks.set(row.week, row.total);
    byPlayer.set(row.player_id, weeks);
  }
  return Object.fromEntries([...byPlayer].map(([id, weeks]) => [id, {
    average: [...weeks.values()].reduce((sum, points) => sum + points, 0) / weeks.size,
    lastWeek: weeks.get(currentWeek - 1) ?? null,
  }]));
}

export function useRosterScores(api: string, leagueId: string, season: number, currentWeek: number) {
  const key = `${api}/${leagueId}/${season}/${currentWeek}`;
  const [result, setResult] = useState<{ key: string; scores?: Record<string, RosterScore>; error: boolean }>();
  useEffect(() => {
    if (currentWeek <= 1) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController;
    async function refresh() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 12000);
      try {
        const weeks = await Promise.all(Array.from({ length: currentWeek - 1 }, async (_, index) => {
          const response = await fetch(`${api}/api/v1/weeks/${index + 1}/scores?league_id=${encodeURIComponent(leagueId)}`, { signal: controller.signal, cache: 'no-store' });
          if (!response.ok) throw new Error(String(response.status));
          return await response.json() as WeeklyScore[];
        }));
        if (!disposed) setResult({ key, scores: summarizeRosterScores(weeks.flat(), season, currentWeek), error: false });
      } catch {
        if (!disposed) setResult(previous => ({ key, scores: previous?.key === key ? previous.scores : undefined, error: true }));
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(refresh, 60000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); };
  }, [api, leagueId, season, currentWeek, key]);
  const current = result?.key === key ? result : undefined;
  return { scores: current?.scores || {}, loading: currentWeek > 1 && !current, error: current?.error || false };
}
