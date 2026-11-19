import { useEffect, useRef } from "preact/hooks";

export const reducedMotion = (): boolean => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const SURFACES = ".panel, .selection";

export function installPointerLight(): void {
  let frame = 0;
  let last: PointerEvent | null = null;
  window.addEventListener(
    "pointermove",
    (event) => {
      if (event.pointerType !== "mouse") return;
      last = event;
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (last === null || !(last.target instanceof Element)) return;
        const surface = last.target.closest<HTMLElement>(SURFACES);
        if (surface === null) return;
        const box = surface.getBoundingClientRect();
        surface.style.setProperty("--mx", `${String(last.clientX - box.left)}px`);
        surface.style.setProperty("--my", `${String(last.clientY - box.top)}px`);
      });
    },
    { passive: true },
  );
}

export function useTilt<T extends HTMLElement>(baseX: number, baseY: number, range = 10) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const element = ref.current;
    if (element === null || reducedMotion()) return;
    const s = { x: baseX, y: baseY, vx: 0, vy: 0, tx: baseX, ty: baseY, frame: 0, last: performance.now() };
    const move = (event: PointerEvent) => {
      s.tx = baseX + (event.clientY / window.innerHeight - 0.5) * -range;
      s.ty = baseY + (event.clientX / window.innerWidth - 0.5) * range * 1.4;
    };
    const step = (now: number) => {
      const dt = Math.min(0.032, (now - s.last) / 1000);
      s.last = now;
      s.vx += ((s.tx - s.x) * 60 - s.vx * 9) * dt;
      s.vy += ((s.ty - s.y) * 60 - s.vy * 9) * dt;
      s.x += s.vx * dt;
      s.y += s.vy * dt;
      element.style.setProperty("--tilt-x", `${s.x.toFixed(2)}deg`);
      element.style.setProperty("--tilt-y", `${s.y.toFixed(2)}deg`);
      s.frame = requestAnimationFrame(step);
    };
    window.addEventListener("pointermove", move, { passive: true });
    s.frame = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(s.frame);
      window.removeEventListener("pointermove", move);
    };
  }, [baseX, baseY, range]);
  return ref;
}
