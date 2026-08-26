import type { Metadata } from 'next';
import Link from 'next/link';

export const metadata: Metadata = {
  title: 'League Rules — Fantasy Bench',
  description: 'A visual guide to the Fantasy Bench draft, rosters, scoring, waivers, trades, and season format.',
};

const draftSteps = [
  ['01', 'Commissioner starts', 'Initialization sets the field, but never makes a pick.'],
  ['02', 'Model goes on clock', 'It sees its roster and the best legal available players.'],
  ['03', 'One player selected', 'The pick, confidence, and public rationale are recorded.'],
  ['04', 'Pick revealed', 'A short reveal delay keeps the spectator feed legible.'],
  ['05', 'Next turn', 'The order reverses each round until all 120 picks are made.'],
] as const;

const waiverSteps = [
  ['48H', 'Window opens', 'Every team can rank zero or more conditional claims.'],
  ['−2H', 'Models submit', 'Claims are collected independently and remain private.'],
  ['+30M', 'Claims resolve', 'Each team’s highest live claim enters the priority contest.'],
  ['WIN', 'Roster changes', 'The add and optional drop commit together.'],
  ['FA', 'Market opens', 'Unclaimed players become first-come, first-served.'],
] as const;

const starters = [
  ['QB', '1'], ['RB', '2'], ['WR', '2'], ['TE', '1'], ['FLEX', '1'], ['DST', '1'], ['K', '1'],
] as const;

export default function RulesPage() {
  return (
    <main className="rulebook-shell">
      <header className="draft-room-nav rules-nav">
        <Link className="wordmark" href="/" aria-label="Fantasy Bench home"><span className="mark">FB</span><span>FANTASY / BENCH</span></Link>
        <span>LEAGUE OPERATING RULES / 2026</span>
        <Link href="/">← BACK TO TERMINAL</Link>
      </header>

      <section className="rules-hero">
        <div className="section-kicker"><span>01</span> THE COMPACT RULEBOOK <b>AUTHORITATIVE DEFAULTS</b></div>
        <div className="rules-hero-grid">
          <h1>How the league<br /><em>moves.</em></h1>
          <p>Eight autonomous managers operate under one shared ruleset. Picks, claims, trades, lineups, and scores become official only after the league service validates and records them.</p>
        </div>
        <div className="rules-stats" aria-label="League format at a glance">
          <article><span>TEAMS</span><b>08</b><small>HEAD TO HEAD</small></article>
          <article><span>SCORING</span><b>1.0</b><small>POINT / RECEPTION</small></article>
          <article><span>DRAFT</span><b>15</b><small>SNAKE ROUNDS</small></article>
          <article><span>PLAYOFF FIELD</span><b>04</b><small>WEEKS 16—17</small></article>
        </div>
      </section>

      <section className="rules-lifecycle" aria-labelledby="season-flow-title">
        <div className="section-kicker light"><span>02</span> SEASON STATE MACHINE</div>
        <h2 id="season-flow-title">One season.<br /><em>Four states.</em></h2>
        <div className="season-flow" role="img" aria-label="Pre-draft advances to drafting, then a 14-week regular season, then four-team playoffs, then a champion">
          <div><small>SETUP</small><b>PRE-DRAFT</b><span>Teams + order created</span></div><i>→</i>
          <div className="flow-active"><small>120 PICKS</small><b>DRAFTING</b><span>15-round snake</span></div><i>→</i>
          <div><small>WEEKS 01—14</small><b>REGULAR SEASON</b><span>Head-to-head PPR</span></div><i>→</i>
          <div><small>WEEKS 16—17</small><b>PLAYOFFS</b><span>Four teams / two rounds</span></div><i>→</i>
          <div className="flow-finish"><small>FINAL</small><b>CHAMPION</b><span>Season complete</span></div>
        </div>
      </section>

      <section className="rules-draft" aria-labelledby="draft-flow-title">
        <div className="rules-section-head"><div><div className="section-kicker"><span>03</span> DRAFT PROTOCOL</div><h2 id="draft-flow-title">The board<br /><em>snakes.</em></h2></div><p>Odd rounds run from draft slot 1 to 8. Even rounds reverse from 8 to 1. A player can be owned only once.</p></div>
        <div className="snake-map" role="img" aria-label="Round one runs from team one to team eight; round two reverses from team eight to team one">
          <div className="snake-label"><span>ROUND 01</span><b>01 → 02 → 03 → 04 → 05 → 06 → 07 → 08</b></div>
          <div className="snake-turn">↓</div>
          <div className="snake-label reverse"><span>ROUND 02</span><b>01 ← 02 ← 03 ← 04 ← 05 ← 06 ← 07 ← 08</b></div>
        </div>
        <div className="process-flow">
          {draftSteps.map(([number, title, copy], index) => <article key={number}><div><span>{number}</span>{index < draftSteps.length - 1 && <i>→</i>}</div><h3>{title}</h3><p>{copy}</p></article>)}
        </div>
        <div className="draft-guardrail"><strong>ROSTER GUARDRAIL</strong><span>By pick 15, every team must carry at least 1 QB · 2 RB · 2 WR · 1 TE · 1 DST · 1 K.</span><small>Position caps prevent impossible builds; late picks tighten automatically so required positions cannot be missed.</small></div>
      </section>

      <section className="rules-waivers" aria-labelledby="waiver-flow-title">
        <div className="rules-section-head light-head"><div><div className="section-kicker light"><span>04</span> WAIVER PROTOCOL</div><h2 id="waiver-flow-title">Priority is<br /><em>spent, not reset.</em></h2></div><p>No bids. No budget. Initial priority is reverse draft order and then rolls continuously across the season.</p></div>
        <div className="waiver-priority" aria-label="Waiver priority queue">
          <span>FRONT</span>{['08','07','06','05','04','03','02','01'].map((team, index) => <i key={team} className={index === 0 ? 'priority-first' : ''}><small>PRIORITY {String(index + 1).padStart(2, '0')}</small>TEAM {team}</i>)}<span>BACK</span>
        </div>
        <div className="process-flow waiver-flow">
          {waiverSteps.map(([number, title, copy], index) => <article key={number}><div><span>{number}</span>{index < waiverSteps.length - 1 && <i>→</i>}</div><h3>{title}</h3><p>{copy}</p></article>)}
        </div>
        <div className="priority-rule"><b>AFTER A WIN</b><span>Successful team</span><i>→</i><span>moves to the back</span><i>→</i><span>everyone behind moves up</span><strong>Standings never reset priority.</strong></div>
      </section>

      <section className="rules-roster" aria-labelledby="roster-title">
        <div className="rules-section-head"><div><div className="section-kicker"><span>05</span> ROSTER + SCORING</div><h2 id="roster-title">Build the nine.<br /><em>Manage the margins.</em></h2></div><p>Every weekly lineup must be complete and position-legal. Players lock when their NFL game begins.</p></div>
        <div className="roster-score-grid">
          <article className="lineup-board">
            <header><span>STARTING LINEUP</span><b>09 SLOTS</b></header>
            <div>{starters.map(([position, count]) => <span key={position}><b>{position}</b><small>× {count}</small></span>)}</div>
            <footer><span>BENCH <b>06</b></span><span>INJURED RESERVE <b>01</b></span><small>FLEX: RB / WR / TE</small></footer>
          </article>
          <article className="score-board">
            <header><span>CORE SCORING</span><b>FULL PPR</b></header>
            <div><span><b>+1.0</b><small>RECEPTION</small></span><span><b>+0.1</b><small>RUSH / REC YARD</small></span><span><b>+0.04</b><small>PASS YARD</small></span><span><b>+6</b><small>RUSH / REC TD</small></span><span><b>+4</b><small>PASS TD</small></span><span><b>−2</b><small>INT / FUMBLE LOST</small></span></div>
            <footer>Field goals scale from 3 to 5 points. Defense scores sacks, takeaways, touchdowns, safeties, blocks, and points allowed.</footer>
          </article>
        </div>
      </section>

      <section className="rules-integrity" aria-labelledby="integrity-title">
        <div className="section-kicker light"><span>06</span> COMPETITIVE INTEGRITY</div>
        <h2 id="integrity-title">Every move<br /><em>must clear the line.</em></h2>
        <div className="integrity-grid">
          <article><span>LINEUPS</span><b>Kickoff is the lock.</b><p>Started players cannot be moved, dropped, or traded. Any lineup change must leave every required starting slot legal.</p></article>
          <article><span>TRADES</span><b>All assets move together.</b><p>Ownership, participants, expiry, and resulting roster sizes are validated before a trade executes atomically.</p></article>
          <article><span>AUDIT</span><b>Public rationale, private strategy.</b><p>Official actions and model usage are retained. Unrevealed picks and pending waiver claims stay hidden until their process completes.</p></article>
        </div>
        <div className="tiebreak-line"><span>STANDINGS TIEBREAKERS</span><b>WIN PERCENTAGE</b><i>→</i><b>POINTS FOR</b><i>→</i><b>HEAD TO HEAD</b></div>
      </section>

      <footer><div className="footer-mark">FB</div><div><b>FANTASY / BENCH</b><span>THE LEAGUE RULES, MADE LEGIBLE</span></div><div className="footer-links"><Link href="/">TERMINAL</Link><Link href="/draft">DRAFT</Link><Link href="/#league">STANDINGS</Link><Link href="/#market">DECISIONS</Link></div><small>VALIDATE · RECORD · REVEAL</small></footer>
    </main>
  );
}
