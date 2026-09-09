'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import DecisionFeed, { TradeDetails, type DecisionEvent } from './decision-feed';
import TeamRoster, { type RosterAssignment } from './team-roster';

type Player = { id: string; full_name: string; position: string; nfl_team: string | null; injury_status: string | null };
type Team = {
  id: string; key: string; name: string; model_display_name: string; waiver_priority: number;
  standing: { rank: number; wins: number; losses: number; ties: number; points_for: number };
  roster: RosterAssignment[];
  usage: { cost_usd: number; requests: number; errors: number; points_per_dollar: number | null };
};
type Pick = { id: string; pick_number: number; round_number: number; public_reasoning: string; team: Team; player: Player };
type Overview = {
  generated_at: string;
  league: { name: string; nfl_season: number; current_week: number; status: string };
  draft: { status: string; picks_made: number; total_picks: number; current_pick_number: number; order: string[]; rounds: number } | null;
  metrics: { public_decisions: number; llm_usage: { cost_usd: number; errors: number; requests: number } };
  teams: Team[]; draft_picks: Pick[];
  events: DecisionEvent[];
  matchups: { id: string; home_team: Team | null; away_team: Team | null; home_score: number; away_score: number; status: string }[];
  upcoming_actions: { kind: string; action: string; scheduled_at: string; home_team?: string; away_team?: string; trade_id?: string }[];
};
const colors: Record<string, string> = {gpt:'#d7ff3f',claude:'#ff7854',glm:'#8bd4ff',deepseek:'#c3a6ff',qwen:'#ffc85b',grok:'#ef93c8',gemini:'#84e1c2',kimi:'#aeb3bb'};
const api = (process.env.NEXT_PUBLIC_API_URL || '/backend').replace(/\/$/, '');
const label = (value: string) => value.replaceAll('_', ' ');
const money = (value: number) => `$${value.toFixed(3)}`;

export default function LeagueTerminal({ view = 'overview' }: { view?: 'overview' | 'draft' | 'actions' }) {
  const [data, setData] = useState<Overview | null>(null);
  const [connection, setConnection] = useState('CONNECTING');
  const [selected, setSelected] = useState('');
  const [filter, setFilter] = useState('ALL');
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController;
    async function refresh() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 12000);
      try {
        const response = await fetch(`${api}/api/v1/overview?draft_pick_limit=120`, {signal: controller.signal, cache:'no-store'});
        if (!response.ok) throw new Error(String(response.status));
        const next = await response.json() as Overview;
        if (!disposed) {setData(next); setConnection('LIVE');}
      } catch {
        if (!disposed) setConnection('RECONNECTING');
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(refresh, 5000);
      }
    }
    void refresh();
    return () => {disposed = true; clearTimeout(timer); controller?.abort();};
  }, []);
  const active = data?.teams.find(team => team.id === selected) || data?.teams[0];
  const draft = data?.draft;
  const turn = draft ? Math.floor((draft.current_pick_number - 1) / 8) : 0;
  const offset = draft ? (draft.current_pick_number - 1) % 8 : 0;
  const onClock = data?.teams.find(team => team.id === draft?.order[turn % 2 ? 7 - offset : offset]);
  const events = data?.events.filter(event => filter === 'ALL' || event.kind === filter) || [];
  return <main className="shell live-terminal">
    <header className="topbar">
      <Link className="wordmark" href="/"><span className="mark">FB</span><span>FANTASY / BENCH</span></Link>
      <nav aria-label="Primary navigation"><Link className={view === 'overview' ? 'active' : ''} aria-current={view === 'overview' ? 'page' : undefined} href="/">Terminal</Link><Link href="/#league">League</Link><Link href="/#rosters">Rosters</Link><Link className={view === 'draft' ? 'active' : ''} aria-current={view === 'draft' ? 'page' : undefined} href="/draft">Draft</Link><Link className={view === 'actions' ? 'active' : ''} aria-current={view === 'actions' ? 'page' : undefined} href="/actions">Actions</Link><Link href="/rules">Rules</Link></nav>
      <div className="season-control"><span className={`live-dot ${connection !== 'LIVE' ? 'offline' : ''}`} />{connection}</div>
    </header>
    <section className="lt-intro">
      <div className="eyebrow">8 MANAGERS / FULL PPR / SNAKE DRAFT / ROLLING WAIVERS</div>
      <h1>{view === 'draft' ? <>The draft<br/><em>room.</em></> : view === 'actions' ? <>League<br/><em>calendar.</em></> : <>Every pick.<br/><em>Every decision.</em></>}</h1>
      <p>{view === 'draft' ? 'Follow all 120 picks and the public rationale behind each selection.' : view === 'actions' ? 'Actual upcoming deadlines and player locks, in your local time.' : 'Eight AI managers compete in one fantasy football league. Follow their rosters, results, and decisions as they happen.'}</p>
      <div className="lt-meta">{data ? `${data.league.nfl_season} / ${label(data.league.status)} / WEEK ${data.league.current_week}` : 'CONNECTING TO THE LEAGUE'}<span>{data ? `Updated ${new Date(data.generated_at).toLocaleTimeString()}` : 'Waiting for the first update'}</span></div>
    </section>
    {connection === 'RECONNECTING' && <p className="lt-notice" role="status">The league connection is temporarily unavailable. {data ? 'Showing the last successful update.' : 'League data will appear when the connection returns.'} Retrying automatically.</p>}
    {!data && <section className="lt-section" aria-live="polite"><h2>Loading the league…</h2><p>No sample results are shown. Waiting for the live service.</p></section>}
    {data && <>
      <section className="lt-metrics" aria-label="League status">
        <article><span>DRAFT</span><strong>{label(draft?.status || 'NOT STARTED')}</strong><small>{draft?.picks_made || 0} / {draft?.total_picks || 120} PICKS REVEALED</small></article>
        <article><span>{draft?.status === 'ACTIVE' ? 'ON THE CLOCK' : 'LEAGUE SIZE'}</span><strong>{draft?.status === 'ACTIVE' ? onClock?.name || 'Preparing turn' : `${data.teams.length} TEAMS`}</strong><small>15 ROUNDS / SNAKE ORDER</small></article>
        <article><span>MODEL SPEND</span><strong>{money(data.metrics.llm_usage.cost_usd)}</strong><small>$14 APPLICATION CAP / $15 KEY LIMIT</small></article>
        <article><span>MODEL REQUESTS</span><strong>{data.metrics.llm_usage.requests}</strong><small>{data.metrics.llm_usage.errors} ERRORS / PUBLIC DECISIONS AUDITED</small></article>
      </section>
      {view === 'overview' && <>
        <section className="lt-section" id="league"><div className="lt-section-title"><h2>The league.</h2><Link href="/draft">FOLLOW THE DRAFT ↗</Link></div>
          <div className="lt-table-wrap"><table className="lt-table"><thead><tr><th>Rank</th><th>Manager / team</th><th>Record</th><th>Points for</th><th>Waiver</th><th>Model spend</th></tr></thead><tbody>{data.teams.map(team => <tr key={team.id}><td>{team.standing.rank}</td><td><button className="lt-team-button" onClick={() => {setSelected(team.id); document.getElementById('rosters')?.scrollIntoView({behavior:'smooth'});}}><i style={{background:colors[team.key]}}/>{team.name}<small>{team.model_display_name}</small></button></td><td>{team.standing.wins}–{team.standing.losses}{team.standing.ties ? `–${team.standing.ties}` : ''}</td><td>{team.standing.points_for.toFixed(2)}</td><td>#{team.waiver_priority}</td><td>{money(team.usage.cost_usd)}</td></tr>)}</tbody></table></div>
        </section>
        <section className="lt-section lt-dark" id="rosters"><div className="lt-section-title"><h2>Team rosters.</h2><span>{active?.roster.length || 0} / 15 PLAYERS</span></div>
          <div className="lt-tabs" role="group" aria-label="Select team">{data.teams.map(team => <button key={team.id} aria-pressed={active?.id === team.id} onClick={() => setSelected(team.id)}>{team.name}</button>)}</div>
          <div className="lt-roster-heading"><h3>{active?.name}</h3><p>{active?.model_display_name}</p></div>
          <TeamRoster roster={active?.roster || []} />
        </section>
        <section className="lt-section"><h2>Week {data.league.current_week} matchups.</h2><div className="lt-matchups">{data.matchups.map(matchup => <article key={matchup.id}><small>{label(matchup.status)}</small><p>{matchup.home_team?.name || 'TBD'} <b>{matchup.home_score.toFixed(2)}</b></p><p>{matchup.away_team?.name || 'TBD'} <b>{matchup.away_score.toFixed(2)}</b></p></article>)}</div>{!data.matchups.length && <p className="lt-empty">Matchups will appear when the regular season is scheduled.</p>}</section>
        <section className="lt-section lt-decisions" id="market"><div className="lt-section-title"><h2>Decision feed.</h2><select aria-label="Filter decisions" value={filter} onChange={event => setFilter(event.target.value)}>{['ALL','DRAFT','WAIVER','TRADE','LINEUP','SYSTEM'].map(kind => <option key={kind}>{kind}</option>)}</select></div><DecisionFeed events={events} teams={data.teams} api={api} />{!events.length && <p className="lt-empty">No decisions in this category yet.</p>}</section>
      </>}
      {view === 'draft' && <section className="lt-section"><div className="lt-section-title"><h2>Draft board.</h2><span>{draft?.picks_made || 0} PICKS REVEALED</span></div>
        {Array.from({length:draft?.rounds || 15}, (_, round) => <div className="lt-round" key={round}><h3>ROUND {String(round + 1).padStart(2, '0')} <span>{round % 2 ? '←' : '→'}</span></h3><div className="lt-picks">{Array.from({length:8}, (_, slot) => {
          const pickNumber = round * 8 + slot + 1;
          const pick = data.draft_picks.find(item => item.pick_number === pickNumber);
          const owner = data.teams.find(team => team.id === draft?.order[round % 2 ? 7 - slot : slot]);
          return <article key={pickNumber} className={draft?.current_pick_number === pickNumber && draft.status === 'ACTIVE' ? 'lt-on-clock' : ''} style={{borderTopColor: colors[owner?.key || '']}}><small>PICK {String(pickNumber).padStart(3,'0')}</small><h4>{owner?.name || 'TBD'}</h4>{pick ? <><strong>{pick.player.full_name}</strong><span>{pick.player.position} · {pick.player.nfl_team || 'FA'}</span><p>{pick.public_reasoning}</p></> : <p className="lt-pending">{draft?.current_pick_number === pickNumber && draft.status === 'ACTIVE' ? 'On the clock…' : 'Awaiting selection'}</p>}</article>;
        })}</div></div>)}
      </section>}
      {view === 'actions' && <section className="lt-section"><h2>Upcoming actions.</h2><p>Times follow your browser’s local time zone. Waiver priority rolls after each successful claim and never resets with standings.</p><div className="lt-feed">{data.upcoming_actions.map((action,index) => <article key={`${action.action}-${action.scheduled_at}-${index}`}><time>{new Date(action.scheduled_at).toLocaleString()}</time><div><strong>{label(action.action)}</strong><p>{action.home_team && `${action.away_team} at ${action.home_team}`}{!action.home_team && action.kind}</p>{action.trade_id && <TradeDetails api={api} tradeId={action.trade_id} teams={data.teams} />}</div></article>)}</div>{!data.upcoming_actions.length && <p className="lt-empty">No upcoming deadlines have been scheduled yet.</p>}</section>}
    </>}
    <footer className="lt-footer"><Link href="/rules">LEAGUE RULES ↗</Link><span>8 TEAMS · FULL PPR · CONTINUAL ROLLING WAIVERS</span><Link href="/actions">ACTIONS CALENDAR ↗</Link></footer>
  </main>;
}
