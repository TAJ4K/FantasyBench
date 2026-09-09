type RosterPlayer = {
  id: string;
  full_name: string;
  position: string;
  nfl_team: string | null;
  injury_status: string | null;
};

export type RosterAssignment = {
  id: string;
  position_slot: string | null;
  slot_type: string;
  player: RosterPlayer;
};

const startingSlots = ['QB', 'RB1', 'RB2', 'WR1', 'WR2', 'TE', 'FLEX', 'K', 'DST'];
const positionLabel = (slot: string) => slot.replace(/\d+$/, '');

function RosterRow({ slot, assignment }: { slot: string; assignment?: RosterAssignment }) {
  const player = assignment?.player;
  const position = positionLabel(slot);
  return <li className={`roster-row${player ? '' : ' roster-row-empty'}`}>
    <span className="roster-slot" data-position={position} aria-label={`${slot} slot`}>{position}</span>
    <div className="roster-player">
      <span className="roster-player-name">{player?.full_name || 'Empty slot'}</span>
      <span className="roster-player-meta">{player ? `${player.position} · ${player.nfl_team || 'FA'}` : 'Awaiting assignment'}</span>
    </div>
    <div className="roster-player-status">{player?.injury_status ? <span className="roster-injury">{player.injury_status}</span> : <span className="roster-no-status" aria-label="No injury status reported">—</span>}</div>
  </li>;
}

export default function TeamRoster({ roster }: { roster: RosterAssignment[] }) {
  const starters = roster.filter(row => row.slot_type === 'STARTER');
  const bench = roster.filter(row => row.slot_type === 'BENCH');
  const reserve = roster.filter(row => row.slot_type === 'IR');
  const extraStarters = starters.filter(row => !startingSlots.includes(row.position_slot || ''));
  return <div className="fantasy-roster">
    <section className="roster-group" aria-label="Starting lineup">
      <div className="roster-group-heading"><h4>Starters <span>{starters.length} / {startingSlots.length}</span></h4><span>Injury status</span></div>
      <ol className="roster-list">
        {startingSlots.map(slot => <RosterRow key={slot} slot={slot} assignment={starters.find(row => row.position_slot === slot)} />)}
        {extraStarters.map(row => <RosterRow key={row.id} slot={row.position_slot || row.player.position} assignment={row} />)}
      </ol>
    </section>
    <section className="roster-group" aria-label="Bench">
      <div className="roster-group-heading"><h4>Bench <span>{bench.length} players</span></h4></div>
      {bench.length ? <ol className="roster-list">{bench.map(row => <RosterRow key={row.id} slot="BN" assignment={row} />)}</ol> : <p className="roster-group-empty">No players on the bench.</p>}
    </section>
    {reserve.length > 0 && <section className="roster-group" aria-label="Injured reserve">
      <div className="roster-group-heading"><h4>Injured reserve <span>{reserve.length} players</span></h4></div>
      <ol className="roster-list">{reserve.map(row => <RosterRow key={row.id} slot="IR" assignment={row} />)}</ol>
    </section>}
  </div>;
}
