'use client';

import Image from 'next/image';
import { useState } from 'react';
import { useRosterScores, type RosterScore } from './roster-scores';

type RosterPlayer = {
  id: string;
  full_name: string;
  position: string;
  nfl_team: string | null;
  injury_status: string | null;
  sleeper_id?: string | null;
};

export type RosterAssignment = {
  id: string;
  position_slot: string | null;
  slot_type: string;
  player: RosterPlayer;
};

const startingSlots = ['QB', 'RB1', 'RB2', 'WR1', 'WR2', 'TE', 'FLEX', 'K', 'DST'];
const positionLabel = (slot: string) => slot.replace(/\d+$/, '');

function PlayerPortrait({ player }: { player?: RosterPlayer }) {
  const [failed, setFailed] = useState(false);
  const source = player?.position === 'DST' && player.nfl_team
    ? `https://sleepercdn.com/images/team_logos/nfl/${player.nfl_team.toLowerCase()}.png`
    : player?.sleeper_id && /^\d+$/.test(player.sleeper_id)
      ? `https://sleepercdn.com/content/nfl/players/${player.sleeper_id}.jpg`
      : null;
  const initials = player?.position === 'DST' ? player.nfl_team : player?.full_name.split(/\s+/).map(part => part[0]).slice(0, 2).join('');
  return <span className={`roster-portrait${player?.position === 'DST' ? ' roster-portrait-defense' : ''}`} aria-hidden="true">
    {source && !failed ? <Image src={source} alt="" width={48} height={48} unoptimized onError={() => setFailed(true)} /> : <span>{initials || '—'}</span>}
  </span>;
}

function RosterRow({ slot, assignment, score, loading }: { slot: string; assignment?: RosterAssignment; score?: RosterScore; loading: boolean }) {
  const player = assignment?.player;
  const position = positionLabel(slot);
  return <li className={`roster-row${player ? '' : ' roster-row-empty'}`}>
    <span className="roster-slot" data-position={position} aria-label={`${slot} slot`}>{position}</span>
    <PlayerPortrait key={player?.id || 'empty'} player={player} />
    <div className="roster-player">
      <span className="roster-player-name">{player?.full_name || 'Empty slot'}</span>
      <span className="roster-player-meta">{player ? `${player.position} · ${player.nfl_team || 'FA'}` : 'Awaiting assignment'}</span>
      {player?.injury_status && <span className="roster-injury">{player.injury_status}</span>}
    </div>
    <span className="roster-points" aria-label={`Average fantasy points: ${score?.average.toFixed(1) ?? (loading && player ? 'loading' : 'unavailable')}`}>{score?.average.toFixed(1) ?? (loading && player ? '…' : '—')}</span>
    <span className="roster-points" aria-label={`Last week fantasy points: ${score?.lastWeek?.toFixed(1) ?? (loading && player ? 'loading' : 'unavailable')}`}>{score?.lastWeek?.toFixed(1) ?? (loading && player ? '…' : '—')}</span>
  </li>;
}

export default function TeamRoster({ roster, api, leagueId, season, currentWeek }: { roster: RosterAssignment[]; api: string; leagueId: string; season: number; currentWeek: number }) {
  const { scores, loading, error } = useRosterScores(api, leagueId, season, currentWeek);
  const starters = roster.filter(row => row.slot_type === 'STARTER');
  const bench = roster.filter(row => row.slot_type === 'BENCH');
  const reserve = roster.filter(row => row.slot_type === 'IR');
  const extraStarters = starters.filter(row => !startingSlots.includes(row.position_slot || ''));
  const scoreHeadings = <><span className="roster-score-heading" title="Average fantasy points per recorded week this season, excluding the current week">AVG</span><span className="roster-score-heading" title={currentWeek > 1 ? `Fantasy points in week ${currentWeek - 1}` : 'No previous week this season'}>LAST WK</span></>;
  return <div className="fantasy-roster">
    <section className="roster-group" aria-label="Starting lineup">
      <div className="roster-group-heading"><h4>Starters <span>{starters.length} / {startingSlots.length}</span></h4>{scoreHeadings}</div>
      <ol className="roster-list">
        {startingSlots.map(slot => { const row = starters.find(row => row.position_slot === slot); return <RosterRow key={slot} slot={slot} assignment={row} score={row && scores[row.player.id]} loading={loading} />; })}
        {extraStarters.map(row => <RosterRow key={row.id} slot={row.position_slot || row.player.position} assignment={row} score={scores[row.player.id]} loading={loading} />)}
      </ol>
    </section>
    <section className="roster-group" aria-label="Bench">
      <div className="roster-group-heading"><h4>Bench <span>{bench.length} players</span></h4>{scoreHeadings}</div>
      {bench.length ? <ol className="roster-list">{bench.map(row => <RosterRow key={row.id} slot="BN" assignment={row} score={scores[row.player.id]} loading={loading} />)}</ol> : <p className="roster-group-empty">No players on the bench.</p>}
    </section>
    {reserve.length > 0 && <section className="roster-group" aria-label="Injured reserve">
      <div className="roster-group-heading"><h4>Injured reserve <span>{reserve.length} players</span></h4>{scoreHeadings}</div>
      <ol className="roster-list">{reserve.map(row => <RosterRow key={row.id} slot="IR" assignment={row} score={scores[row.player.id]} loading={loading} />)}</ol>
    </section>}
    <p className="roster-score-note">{error ? 'Scores temporarily unavailable; any displayed scores are from the last successful update. ' : ''}Average uses recorded weeks in {season}, excluding the current week. — means no recorded score.</p>
  </div>;
}
