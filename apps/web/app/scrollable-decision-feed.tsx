'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import DecisionFeed, { type DecisionEvent } from './decision-feed';

type Cursor = { before: string; before_id: string };
type Props = { api: string; leagueId: string; filter: string; teams: { id: string; name: string }[]; liveEvents: DecisionEvent[] };

function DecisionStream({ api, leagueId, filter, teams, liveEvents, refresh }: Props & { refresh: () => void }) {
  const [events, setEvents] = useState<DecisionEvent[]>([]);
  const [cursor, setCursor] = useState<Cursor | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const busy = useRef(false);
  const request = useRef<AbortController | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);

  const loadPage = useCallback(async (next: Cursor | null) => {
    if (busy.current) return;
    busy.current = true;
    const controller = new AbortController();
    request.current = controller;
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const query = new URLSearchParams({ league_id: leagueId, kind: filter, limit: '25', ...next });
      const response = await fetch(`${api}/api/v1/league/decisions?${query}`, { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error(String(response.status));
      const page = await response.json() as { items: DecisionEvent[]; next_cursor: Cursor | null };
      if (request.current !== controller) return;
      setError(false);
      setEvents(previous => {
        const seen = new Set(previous.map(event => event.id));
        return [...previous, ...page.items.filter(event => !seen.has(event.id))];
      });
      setCursor(page.next_cursor);
      setHasMore(page.next_cursor !== null);
    } catch {
      if (request.current === controller) setError(true);
    } finally {
      clearTimeout(timeout);
      if (request.current === controller) { busy.current = false; setLoading(false); }
    }
  }, [api, leagueId, filter]);

  const loadMore = useCallback(() => {
    if (busy.current) return;
    setLoading(true);
    setError(false);
    void loadPage(cursor);
  }, [cursor, loadPage]);

  useEffect(() => {
    const initialRequest = setTimeout(() => void loadPage(null), 0);
    return () => { clearTimeout(initialRequest); request.current?.abort(); request.current = null; busy.current = false; };
  }, [loadPage]);

  useEffect(() => {
    if (loading || error || !hasMore || !sentinel.current || !viewport.current || !('IntersectionObserver' in window)) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) loadMore();
    }, { root: viewport.current, rootMargin: '160px' });
    observer.observe(sentinel.current);
    return () => observer.disconnect();
  }, [error, hasMore, loading, loadMore]);

  const newest = liveEvents.find(event => filter === 'ALL' || event.kind === filter);
  const hasNew = newest && events.length > 0 && new Date(newest.occurred_at) > new Date(events[0].occurred_at);
  return <div className="decision-stream">
    <div className="decision-stream-toolbar"><span>Newest first · scroll for older decisions</span><button onClick={refresh}>{hasNew ? 'New decisions available · refresh' : 'Refresh latest'}</button></div>
    <div className="decision-stream-viewport" ref={viewport} tabIndex={0} role="region" aria-label={`${filter === 'ALL' ? 'All' : filter} decisions`}>
      <DecisionFeed events={events} teams={teams} api={api} />
      <div className="decision-stream-end" ref={sentinel}>
        {loading ? <p role="status">Loading decisions…</p> : error ? <><p role="status">Could not load decisions. Your place is saved.</p><button onClick={loadMore}>Retry</button></> : hasMore ? <button onClick={loadMore}>Load older decisions</button> : <p>{events.length ? 'You’ve reached the beginning of this feed.' : 'No decisions in this category yet.'}</p>}
      </div>
    </div>
  </div>;
}

export default function ScrollableDecisionFeed(props: Props) {
  const [revision, setRevision] = useState(0);
  return <DecisionStream key={`${props.leagueId}-${props.filter}-${revision}`} {...props} refresh={() => setRevision(value => value + 1)} />;
}
