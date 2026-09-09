import type { Metadata } from 'next';
import LeagueTerminal from '../league-terminal';

export const metadata: Metadata = {
  title: '2026 Draft Room — Fantasy Bench',
  description: 'Live snake draft picks and public manager decisions from the eight-team PPR league.',
};
export default function DraftPage() {
  return <LeagueTerminal view="draft" />;
}
