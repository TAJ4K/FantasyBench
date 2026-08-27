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

    const revealTimers: number[] = [];
    const roundStates = rounds.map(() => 'pending' as 'pending' | 'queued' | 'revealing' | 'revealed');
    const revealQueue: number[] = [];
    let revealing = false;
    let stopped = false;

    const revealNextRound = () => {
      if (stopped || revealing) return;
      const roundIndex = revealQueue.shift();
      if (roundIndex === undefined) return;

      const round = rounds[roundIndex];
      const roundRect = round.getBoundingClientRect();
      const isVisible = roundRect.bottom > 0 && roundRect.top < window.innerHeight;
      const roundPicks = Array.from(round.querySelectorAll<HTMLElement>('.draft-pick'));

      if (!isVisible) {
        roundPicks.forEach((pick) => pick.classList.add('is-revealed'));
        roundStates[roundIndex] = 'revealed';
        revealTimers.push(window.setTimeout(revealNextRound, 0));
        return;
      }

      roundStates[roundIndex] = 'revealing';
      revealing = true;
      const lastStep = Math.max(...roundPicks.map((pick) => Number(pick.style.getPropertyValue('--reveal-step')) || 0));

      roundPicks.forEach((pick) => {
        const step = Number(pick.style.getPropertyValue('--reveal-step')) || 0;
        revealTimers.push(window.setTimeout(() => pick.classList.add('is-revealed'), step * 65));
      });

      revealTimers.push(window.setTimeout(() => {
        roundStates[roundIndex] = 'revealed';
        revealing = false;
        revealNextRound();
      }, lastStep * 65 + 360));
    };

    const queueThroughRound = (targetIndex: number) => {
      rounds.slice(0, targetIndex + 1).forEach((round, roundIndex) => {
        if (roundStates[roundIndex] !== 'pending') return;
        roundStates[roundIndex] = 'queued';
        revealQueue.push(roundIndex);
        observer.unobserve(round);
      });
      revealNextRound();
    };

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        queueThroughRound(rounds.indexOf(entry.target as HTMLElement));
      });
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.12 });

    rounds.forEach((round) => observer.observe(round));
    return () => {
      stopped = true;
      observer.disconnect();
      revealTimers.forEach(window.clearTimeout);
    };
  }, []);

  return <div className="draft-board-grid reveal-ready" ref={boardRef}>{children}</div>;
}
