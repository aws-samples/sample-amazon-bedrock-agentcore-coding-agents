import { useRef, useEffect, useState, useCallback } from 'react';

/**
 * Chat auto-scroll: keep the newest message in view WITHOUT yanking the reader
 * down when they scroll up to re-read. The message bar stays pinned by layout
 * (it is a shrink-0 sibling of the scroll region); this hook only manages the
 * transcript's scroll position.
 *
 * Behavior (the foxl / shadcn-chatbot-kit pattern):
 *  - New content (or a streaming token) scrolls to the bottom ONLY while the
 *    reader is already at/near the bottom.
 *  - Scrolling up pauses auto-scroll; scrolling back to the bottom resumes it.
 *  - `isAtBottom` drives a "jump to latest" button so the reader can get back.
 *
 * Pass the changing values (message count, streamed length) as `deps` so the
 * effect fires as the transcript grows and as tokens stream in.
 */
export function useAutoScroll(deps: unknown[] = [], threshold = 120) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  // Whether to follow new content. Tracked in a ref so the scroll effect never
  // has to depend on it (which would re-run and fight the user's scroll).
  const followRef = useRef(true);

  const atBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
  }, [threshold]);

  const scrollToBottom = useCallback((smooth = true) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    followRef.current = true;
    setIsAtBottom(true);
  }, []);

  // A scroll gesture decides whether we keep following. Reading up pauses it;
  // returning to the bottom resumes it.
  const onScroll = useCallback(() => {
    const bottom = atBottom();
    followRef.current = bottom;
    setIsAtBottom(bottom);
  }, [atBottom]);

  // Follow new content only while pinned to the bottom. rAF so the DOM has laid
  // out the new node/token before we measure and scroll.
  useEffect(() => {
    if (!followRef.current) return;
    const id = requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
    });
    return () => cancelAnimationFrame(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { scrollRef, isAtBottom, scrollToBottom, onScroll };
}
