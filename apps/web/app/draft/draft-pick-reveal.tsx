'use client';

import { useEffect, useRef, type ReactNode } from 'react';

export default function DraftPickReveal({ children }: { children: ReactNode }) {
  const boardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const board = boardRef.current;
    if (!board) return;

    const rounds = Array.from(board.querySelectorAll<HTMLElement>('.draft-round'));
    const picks = Array.from(board.querySelectorAll<HTMLElement>('.draft-pick'));
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (reducedMotion || !('IntersectionObserver' in window)) {
      picks.forEach((pick) => pick.classList.add('is-revealed'));
      return;
    }

    board.classList.add('reveal-ready');
    const revealTimers: number[] = [];
    const queuedRounds = new Set<HTMLElement>();
    const visibleRounds = new Set<HTMLElement>();
    const revealQueue: HTMLElement[] = [];
    let revealing = false;
    let stopped = false;

    const revealNextRound = () => {
      if (stopped || revealing) return;
      let round = revealQueue.shift();
      while (round && !visibleRounds.has(round)) {
        queuedRounds.delete(round);
        round = revealQueue.shift();
      }
      if (!round) return;

      revealing = true;
      observer.unobserve(round);
      const roundPicks = Array.from(round.querySelectorAll<HTMLElement>('.draft-pick'));
      const lastStep = Math.max(...roundPicks.map((pick) => Number(pick.style.getPropertyValue('--reveal-step')) || 0));

      roundPicks.forEach((pick) => {
        const step = Number(pick.style.getPropertyValue('--reveal-step')) || 0;
        revealTimers.push(window.setTimeout(() => pick.classList.add('is-revealed'), step * 85));
      });

      revealTimers.push(window.setTimeout(() => {
        revealing = false;
        revealNextRound();
      }, lastStep * 85 + 540));
    };

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const round = entry.target as HTMLElement;
        if (!entry.isIntersecting) {
          visibleRounds.delete(round);
          return;
        }

        visibleRounds.add(round);
        if (!queuedRounds.has(round)) {
          queuedRounds.add(round);
          revealQueue.push(round);
          revealQueue.sort((a, b) => rounds.indexOf(a) - rounds.indexOf(b));
        }
      });
      revealNextRound();
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.12 });

    rounds.forEach((round) => observer.observe(round));
    return () => {
      stopped = true;
      observer.disconnect();
      revealTimers.forEach(window.clearTimeout);
    };
  }, []);

  return <div className="draft-board-grid" ref={boardRef}>{children}</div>;
}
