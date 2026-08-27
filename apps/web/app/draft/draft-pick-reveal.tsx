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

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.querySelectorAll<HTMLElement>('.draft-pick').forEach((pick) => {
          const step = Number(pick.style.getPropertyValue('--reveal-step')) || 0;
          revealTimers.push(window.setTimeout(() => pick.classList.add('is-revealed'), step * 85));
        });
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.12 });

    rounds.forEach((round) => observer.observe(round));
    return () => {
      observer.disconnect();
      revealTimers.forEach(window.clearTimeout);
    };
  }, []);

  return <div className="draft-board-grid" ref={boardRef}>{children}</div>;
}
