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
const tradeStatus = (status: string) => ({ PROPOSED: 'Awaiting response', COUNTERED: 'Counteroffer pending', PROCESSED: 'Trade completed', ACCEPTED: 'Accepted', REJECTED: 'Declined', CANCELLED: 'Cancelled', EXPIRED: 'Expired' }[status] || label(status));
const tradeAction = (event: string) => ({ TRADE_PROPOSED: 'proposed a trade', TRADE_COUNTERED: 'made a counteroffer', TRADE_ACCEPTED: 'accepted the trade', TRADE_REJECTED: 'declined the trade', TRADE_CANCELLED: 'cancelled the trade' }[event] || label(event));

export function TradeOfferCard({ offer, teams, selected, latest }: { offer: Offer; teams: Team[]; selected: boolean; latest: boolean }) {
  const teamName = (id: string) => teams.find(team => team.id === id)?.name || 'Unknown team';
  return <section className={`trade-offer${selected ? ' trade-offer-selected' : ''}`} aria-label={`Offer ${offer.sequence}`}>
    <div className="trade-offer-heading"><h4>{offer.sequence === 1 ? 'Original proposal' : `Counteroffer ${offer.sequence - 1}`}</h4><div className="trade-offer-labels">{selected && <span className="trade-event-marker">This event</span>}<span>{latest ? 'Latest offer' : 'Earlier offer'}</span></div></div>
    <p className="trade-offer-byline">Proposed by <b>{teamName(offer.proposer_team_id)}</b> to <b>{teamName(offer.recipient_team_id)}</b></p>
    <div className="trade-sides">{[offer.proposer_team_id, offer.recipient_team_id].map(teamId => {
      const destination = teamId === offer.proposer_team_id ? offer.recipient_team_id : offer.proposer_team_id;
      return <div key={teamId}>
        <span className="trade-side-label">{teamId === offer.proposer_team_id ? 'Offering' : 'In return'}</span>
        <h5>{teamName(teamId)} <span>sends to {teamName(destination)}</span></h5>
        <ul>{offer.assets.filter(asset => asset.from_team_id === teamId).map(asset => <li key={asset.id}><b>{asset.player?.full_name || 'Player unavailable'}</b>{asset.player && <span>{asset.player.position} · {asset.player.nfl_team || 'FA'}</span>}</li>)}</ul>
      </div>;
    })}</div>
    {offer.public_reasoning && <div className="trade-rationale"><h5>{teamName(offer.proposer_team_id)}’s rationale</h5><p>{offer.public_reasoning}</p></div>}
    {offer.message && offer.message !== offer.public_reasoning && <div className="trade-rationale"><h5>Message to {teamName(offer.recipient_team_id)}</h5><p>{offer.message}</p></div>}
    <small>{!latest ? 'Replaced by a later offer' : `Offer status: ${tradeStatus(offer.status)}`}</small>
  </section>;
}

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
  const offers = trade ? [...trade.offers].sort((a, b) => a.sequence - b.sequence) : [];
  const latest = offers.at(-1);
  return <details className="trade-details" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span className="trade-summary-copy"><b>{open ? 'Trade details' : 'View trade details'}</b><small>Players exchanged · offer history</small></span><span className="trade-disclosure" aria-hidden="true">{open ? '−' : '+'}</span></summary>
    {open && <div className="trade-content">
      {error && <p role="status">{trade ? 'Could not refresh. Showing the last update.' : 'Trade details could not be loaded.'} <button onClick={() => setAttempt(value => value + 1)}>Retry</button></p>}
      {!trade && !error && <p role="status">Loading trade details…</p>}
      {trade && <>
        <div className="trade-status"><div><span>Current trade status</span><b className="trade-status-badge" data-status={trade.status}>{tradeStatus(trade.status)}</b></div><span>{offers.length} {offers.length === 1 ? 'offer' : 'offers'}{trade.expires_at && <> · Expiry: {new Date(trade.expires_at).toLocaleString()}</>}</span></div>
        <p className="trade-history-note">{offerId && offers.some(offer => offer.id === offerId) ? 'The offer linked to this feed event is marked below. ' : ''}Offers are shown in order, from the original proposal to the latest offer.</p>
        {offers.map(offer => <TradeOfferCard key={offer.id} offer={offer} teams={teams} selected={offer.id === offerId} latest={offer.id === latest?.id} />)}
        {!trade.offers.length && <p>No offer details are available yet.</p>}
      </>}
    </div>}
  </details>;
}

export default function DecisionFeed({ events, teams, api }: { events: DecisionEvent[]; teams: Team[]; api: string }) {
  return <div className="lt-feed">{events.map(event => {
    const recipient = teams.find(team => team.id === event.data.to_team_id);
    const commentary = event.public_commentary || event.data.public_reasoning;
    const isTrade = event.kind === 'TRADE';
    return <article key={event.id} className={isTrade ? 'decision-trade' : undefined}>
      <div className="decision-meta">{isTrade && <span className="decision-category">Trade activity</span>}<time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString()}</time></div>
      <div>
        {isTrade ? <h3 className="decision-trade-title">{event.team?.name || 'A manager'} {tradeAction(event.event_type)}{recipient && <> with {recipient.name}</>}</h3> : <><strong className="decision-kind">{label(event.event_type)}</strong>{(event.team || event.player) && <p className="decision-participants">{event.team?.name}{event.player && `${event.team ? ' · ' : ''}${event.player.full_name}`}</p>}</>}
        {event.dropped_player && <p>Dropped: {event.dropped_player.full_name} · {event.dropped_player.position}</p>}
        {commentary && (isTrade ? <div className="decision-explanation"><span>{event.team?.name || 'Manager'}’s explanation</span><p>{commentary}</p></div> : <p>{commentary}</p>)}
        {event.aggregate_type === 'TRADE' && event.aggregate_id && <TradeDetails api={api} tradeId={event.aggregate_id} offerId={event.data.offer_id} teams={teams} />}
      </div>
    </article>;
  })}</div>;
}
