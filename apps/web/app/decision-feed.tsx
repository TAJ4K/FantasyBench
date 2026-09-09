'use client';

import { useEffect, useState } from 'react';

type Player = { id: string; full_name: string; position: string; nfl_team: string | null };
type Team = { id: string; name: string };
export type DecisionEvent = {
  id: string;
  event_type: string;
  kind: string;
  occurred_at: string;
  aggregate_type?: string | null;
  aggregate_id?: string | null;
  team: Team | null;
  player: Player | null;
  dropped_player?: Player | null;
  public_commentary?: string | null;
  data: { public_reasoning?: string; to_team_id?: string; offer_id?: string };
};
type Offer = {
  id: string;
  sequence: number;
  status: string;
  proposer_team_id: string;
  recipient_team_id: string;
  message: string | null;
  public_reasoning: string | null;
  assets: { id: string; from_team_id: string; to_team_id: string; player: Player | null }[];
};
type Trade = { id: string; status: string; expires_at: string | null; offers: Offer[] };
const label = (value: string) => value.toLowerCase().replaceAll('_', ' ');

export function TradeDetails({ api, tradeId, offerId, teams }: { api: string; tradeId: string; offerId?: string; teams: Team[] }) {
  const [open, setOpen] = useState(false);
  const [trade, setTrade] = useState<Trade | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController;
    async function refresh() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 12000);
      try {
        const response = await fetch(`${api}/api/v1/trades/${encodeURIComponent(tradeId)}`, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error(String(response.status));
        const next = await response.json() as Trade;
        if (!disposed) { setTrade(next); setError(false); }
      } catch {
        if (!disposed) setError(true);
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(refresh, 15000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); };
  }, [api, tradeId, open, attempt]);
  const teamName = (id: string) => teams.find(team => team.id === id)?.name || 'Unknown team';
  return <details className="trade-details" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Inspect trade <span>Players, offers & rationale</span></summary>
    {open && <div className="trade-content">
      {error && <p role="status">{trade ? 'Could not refresh. Showing the last update.' : 'Trade details could not be loaded.'} <button onClick={() => setAttempt(value => value + 1)}>Retry</button></p>}
      {!trade && !error && <p role="status">Loading trade details…</p>}
      {trade && <>
        <div className="trade-status"><b>Current trade status: {label(trade.status)}</b>{trade.expires_at && <span>Expiry: {new Date(trade.expires_at).toLocaleString()}</span>}</div>
        {trade.offers.map(offer => <section className="trade-offer" key={offer.id} aria-label={`Offer ${offer.sequence}`}>
          <h4>Offer {offer.sequence} <span>{offer.id === offerId ? 'Offer in this event' : `Proposed by ${teamName(offer.proposer_team_id)}`}</span></h4>
          <div className="trade-sides">{[offer.proposer_team_id, offer.recipient_team_id].map(teamId => <div key={teamId}>
            <h5>{teamName(teamId)} <span>sends</span></h5>
            <ul>{offer.assets.filter(asset => asset.from_team_id === teamId).map(asset => <li key={asset.id}><b>{asset.player?.full_name || 'Player unavailable'}</b>{asset.player && <span>{asset.player.position} · {asset.player.nfl_team || 'FA'}</span>}</li>)}</ul>
          </div>)}</div>
          {offer.public_reasoning && <div className="trade-rationale"><h5>Why {teamName(offer.proposer_team_id)} proposed it</h5><p>{offer.public_reasoning}</p></div>}
          {offer.message && offer.message !== offer.public_reasoning && <div className="trade-rationale"><h5>Manager message</h5><p>{offer.message}</p></div>}
          <small>Current offer status: {label(offer.status)}</small>
        </section>)}
        {!trade.offers.length && <p>No offer details are available yet.</p>}
      </>}
    </div>}
  </details>;
}

export default function DecisionFeed({ events, teams, api }: { events: DecisionEvent[]; teams: Team[]; api: string }) {
  return <div className="lt-feed">{events.map(event => {
    const recipient = teams.find(team => team.id === event.data.to_team_id);
    const commentary = event.public_commentary || event.data.public_reasoning;
    return <article key={event.id}>
      <time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString()}</time>
      <div>
        <strong className="decision-kind">{label(event.event_type)}</strong>
        {(event.team || event.player) && <p className="decision-participants">{event.team?.name}{recipient && ` → ${recipient.name}`}{event.player && `${event.team ? ' · ' : ''}${event.player.full_name}`}</p>}
        {event.dropped_player && <p>Dropped: {event.dropped_player.full_name} · {event.dropped_player.position}</p>}
        {commentary && <p>{commentary}</p>}
        {event.aggregate_type === 'TRADE' && event.aggregate_id && <TradeDetails api={api} tradeId={event.aggregate_id} offerId={event.data.offer_id} teams={teams} />}
      </div>
    </article>;
  })}</div>;
}
