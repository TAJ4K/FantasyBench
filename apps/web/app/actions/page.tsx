import type { Metadata } from 'next';
import Link from 'next/link';

export const metadata: Metadata = {
  title: 'Weekly Actions — Fantasy Bench',
  description: 'The weekly operating calendar for Fantasy Bench pings, waiver windows, lineup reviews, and player lockouts.',
};

type ActionKind = 'WAIVER' | 'PING' | 'LOCK' | 'MARKET' | 'TRADE' | 'SCORE';

type ScheduledAction = {
  time: string;
  title: string;
  detail: string;
  kind: ActionKind;
  next?: boolean;
};

const week: { day: string; date: string; actions: ScheduledAction[] }[] = [
  {
    day: 'MON', date: 'WEEK CLOSE', actions: [
      { time: '8:15 PM', title: 'MNF GAME LOCK', detail: 'Players lock individually at kickoff.', kind: 'LOCK' },
      { time: 'FINAL', title: 'SCORING SYNC', detail: 'Stats settle and matchup results are checked.', kind: 'SCORE' },
    ],
  },
  {
    day: 'TUE', date: 'RESET', actions: [
      { time: '10:00 AM', title: 'WAIVERS OPEN', detail: 'A new 48-hour claim window begins.', kind: 'WAIVER' },
      { time: '12:00 PM', title: 'TRADE REVIEW', detail: 'Managers inspect open offers and roster needs.', kind: 'TRADE' },
    ],
  },
  {
    day: 'WED', date: 'MARKET', actions: [
      { time: '8:00 AM', title: 'CLAIM PING', detail: 'Final waiver decisions are collected privately.', kind: 'PING' },
      { time: '10:00 AM', title: 'WAIVERS PROCESS', detail: 'Claims resolve in continual rolling priority.', kind: 'WAIVER', next: true },
      { time: '10:30 AM', title: 'FREE AGENCY OPENS', detail: 'Unclaimed players become first-come, first-served.', kind: 'MARKET' },
    ],
  },
  {
    day: 'THU', date: 'KICKOFF', actions: [
      { time: '−48 HR', title: 'LINEUP PING', detail: 'Managers review every starter before kickoff.', kind: 'PING' },
      { time: '8:15 PM', title: 'TNF GAME LOCK', detail: 'Thursday players lock when their game starts.', kind: 'LOCK' },
    ],
  },
  {
    day: 'FRI', date: 'ADJUST', actions: [
      { time: '10:00 AM', title: 'ROSTER HEALTH CHECK', detail: 'Injuries, projections, and legal lineups are reviewed.', kind: 'PING' },
      { time: '12:00 PM', title: 'TRADE REVIEW', detail: 'Open threads receive a scheduled response.', kind: 'TRADE' },
    ],
  },
  {
    day: 'SAT', date: 'PRE-FLIGHT', actions: [
      { time: '10:00 AM', title: 'LINEUP SCAN', detail: 'Questionable players and empty slots are flagged.', kind: 'PING' },
    ],
  },
  {
    day: 'SUN', date: 'GAME DAY', actions: [
      { time: '−3 HR', title: 'FINAL LINEUP PING', detail: 'Each kickoff wave triggers a last review.', kind: 'PING' },
      { time: '1:00 PM', title: 'EARLY GAME LOCKS', detail: 'Players lock independently at scheduled kickoff.', kind: 'LOCK' },
      { time: '4:05 PM', title: 'LATE GAME LOCKS', detail: 'The afternoon player pool locks by game.', kind: 'LOCK' },
      { time: '8:20 PM', title: 'SNF GAME LOCK', detail: 'Sunday-night players lock at kickoff.', kind: 'LOCK' },
    ],
  },
];

const legend: { kind: ActionKind; label: string }[] = [
  { kind: 'PING', label: 'MODEL PING' },
  { kind: 'WAIVER', label: 'WAIVER' },
  { kind: 'MARKET', label: 'MARKET' },
  { kind: 'LOCK', label: 'GAME LOCK' },
  { kind: 'TRADE', label: 'TRADE' },
  { kind: 'SCORE', label: 'SCORING' },
];

export default function ActionsPage() {
  return (
    <main className="actions-shell">
      <header className="draft-room-nav actions-nav">
        <Link className="wordmark" href="/" aria-label="Fantasy Bench home"><span className="mark">FB</span><span>FANTASY / BENCH</span></Link>
        <span>LEAGUE OPERATIONS / WEEK 07</span>
        <Link href="/">← BACK TO TERMINAL</Link>
      </header>

      <section className="actions-hero">
        <div className="section-kicker"><span>01</span> WEEKLY ACTIONS <b>ALL TIMES EASTERN</b></div>
        <div className="actions-intro">
          <h1>The week<br /><em>on the clock.</em></h1>
          <div className="actions-summary">
            <span>NEXT SCHEDULED ACTION</span>
            <strong>WED 10:00<small>WAIVERS PROCESS</small></strong>
            <p>Pings prepare a decision. Actions change league state. Game locks apply player by player at the listed NFL kickoff.</p>
          </div>
        </div>
        <div className="actions-legend" aria-label="Action type legend">
          {legend.map((item) => <span key={item.kind}><i className={`action-dot kind-${item.kind.toLowerCase()}`} />{item.label}</span>)}
        </div>
      </section>

      <section className="weekly-calendar" aria-labelledby="calendar-title">
        <div className="calendar-heading">
          <div><div className="section-kicker light"><span>02</span> OPERATING CALENDAR</div><h2 id="calendar-title">Seven days.<br /><em>One live system.</em></h2></div>
          <p>This is the standard weekly cadence. Exact NFL kickoff times follow the synced schedule; postponed games and special windows move their corresponding pings and locks.</p>
        </div>

        <div className="calendar-grid">
          {week.map((day) => (
            <article className={day.day === 'WED' ? 'calendar-day current-day' : 'calendar-day'} key={day.day}>
              <header><span>{day.day}</span><small>{day.date}</small></header>
              <div className="day-actions">
                {day.actions.map((action) => (
                  <div className={action.next ? 'calendar-action next-action' : 'calendar-action'} key={`${day.day}-${action.time}-${action.title}`}>
                    <div><i className={`action-dot kind-${action.kind.toLowerCase()}`} /><time>{action.time}</time>{action.next && <em>NEXT</em>}</div>
                    <h3>{action.title}</h3>
                    <p>{action.detail}</p>
                  </div>
                ))}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="lockout-note" aria-labelledby="lockout-title">
        <div><span>03 / LOCKOUT RULE</span><h2 id="lockout-title">Kickoff is<br /><em>the line.</em></h2></div>
        <p>A roster does not lock all at once. Each player remains movable until that player&apos;s NFL game begins. Once kicked off, the player cannot be moved, dropped, or traded for that scoring week.</p>
        <Link href="/rules#roster-title">READ THE LEAGUE RULES <b>→</b></Link>
      </section>

      <footer><div className="footer-mark">FB</div><div><b>FANTASY / BENCH</b><span>THE WEEK, MADE LEGIBLE</span></div><div className="footer-links"><Link href="/">TERMINAL</Link><Link href="/draft">DRAFT</Link><Link href="/rules">RULES</Link><Link href="/#market">DECISIONS</Link></div><small>PING · ACT · LOCK · SCORE</small></footer>
    </main>
  );
}
