import type { Metadata } from 'next';
import LeagueTerminal from '../league-terminal';

export const metadata: Metadata = { title: 'Actions Calendar — Fantasy Bench' };
export default function ActionsPage() {
  return <LeagueTerminal view="actions" />;
}
